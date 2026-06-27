"""Terraform runner.

Wraps the ``terraform`` binary and parses machine-readable output. Two design
points worth calling out:

* **Plan/apply separation.** ``plan()`` always writes a binary plan file and
  records its SHA-256. ``apply()`` only ever applies a saved plan file. This
  closes the TOCTOU gap where the world changes between "what you approved"
  and "what you applied".
* **Mock mode.** When enabled (and the default for demos/CI), no terraform
  process is spawned and AWS is never touched. Plans are synthesised so the
  policy engine, approval flow and integrations can be exercised end-to-end
  offline. The scenario is controlled via ``TERRAPILOT_MOCK_PLAN`` (JSON).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# terraform plan action verbs -> our coarse buckets
_CREATE = {"create"}
_UPDATE = {"update"}
_DELETE = {"delete"}


@dataclass
class CommandResult:
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


@dataclass
class ResourceChange:
    address: str
    type: str
    actions: list[str]

    @property
    def is_destroy(self) -> bool:
        return "delete" in self.actions

    @property
    def is_replace(self) -> bool:
        return "delete" in self.actions and "create" in self.actions


@dataclass
class PlanResult:
    stack: str
    ok: bool
    add: int = 0
    change: int = 0
    destroy: int = 0
    resource_changes: list[ResourceChange] = field(default_factory=list)
    plan_file: str = ""
    plan_hash: str = ""
    summary: str = ""
    stdout: str = ""
    stderr: str = ""
    mocked: bool = False

    @property
    def has_changes(self) -> bool:
        return bool(self.add or self.change or self.destroy)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _count(changes: list[ResourceChange]) -> tuple[int, int, int]:
    add = change = destroy = 0
    for c in changes:
        acts = set(c.actions)
        if acts == _CREATE:
            add += 1
        elif acts == _UPDATE:
            change += 1
        elif acts == _DELETE:
            destroy += 1
        elif acts == {"create", "delete"} or acts == {"delete", "create"}:
            # replace: counts as both a destroy and a create
            add += 1
            destroy += 1
    return add, change, destroy


class TerraformRunner:
    def __init__(
        self,
        repo: Path,
        binary: str = "terraform",
        aws_profile: str = "default",
        mock: bool = True,
        plan_dir: Path | None = None,
    ) -> None:
        self.repo = repo
        self.binary = binary
        self.aws_profile = aws_profile
        self.mock = mock
        self.plan_dir = plan_dir or (repo / ".terrapilot" / "plans")
        self.plan_dir.mkdir(parents=True, exist_ok=True)

    # ----- low level ---------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("AWS_PROFILE", self.aws_profile)
        env["TF_IN_AUTOMATION"] = "1"
        env["TF_INPUT"] = "0"
        return env

    def _run(self, args: list[str], cwd: Path) -> CommandResult:
        proc = subprocess.run(
            [self.binary, *args],
            cwd=str(cwd),
            env=self._env(),
            capture_output=True,
            text=True,
        )
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def version(self) -> str:
        if self.mock:
            return "mock"
        res = self._run(["version", "-json"], self.repo)
        try:
            return json.loads(res.stdout).get("terraform_version", "unknown")
        except json.JSONDecodeError:
            return "unknown"

    # ----- validate ----------------------------------------------------
    def fmt_check(self, stack_dir: Path) -> CommandResult:
        if self.mock:
            return CommandResult(0, "mock: fmt ok", "")
        return self._run(["fmt", "-check", "-recursive"], stack_dir)

    def validate(self, stack_dir: Path) -> CommandResult:
        """fmt + a backend-less init + validate. Safe offline (no AWS)."""
        if self.mock:
            return CommandResult(0, "mock: validate ok", "")
        init = self._run(["init", "-backend=false", "-input=false", "-no-color"], stack_dir)
        if not init.ok:
            return init
        return self._run(["validate", "-no-color"], stack_dir)

    # ----- plan --------------------------------------------------------
    def _mock_plan(self, stack: str) -> PlanResult:
        spec: dict[str, Any] = {"add": 1, "change": 1, "destroy": 0, "types": []}
        raw = os.environ.get("TERRAPILOT_MOCK_PLAN")
        if raw:
            try:
                spec.update(json.loads(raw))
            except json.JSONDecodeError:
                pass

        changes: list[ResourceChange] = []
        types: list[str] = list(spec.get("types", []))
        for i in range(int(spec.get("add", 0))):
            t = types[i] if i < len(types) else "aws_iam_role_policy"
            changes.append(ResourceChange(f"{t}.created_{i}", t, ["create"]))
        for i in range(int(spec.get("change", 0))):
            changes.append(ResourceChange(f"aws_security_group.changed_{i}", "aws_security_group", ["update"]))
        for i in range(int(spec.get("destroy", 0))):
            t = types[i] if i < len(types) else "aws_iam_role_policy"
            changes.append(ResourceChange(f"{t}.destroyed_{i}", t, ["delete"]))

        add, change, destroy = _count(changes)
        plan_file = self.plan_dir / f"{stack.replace('/', '__')}.mock.json"
        plan_file.write_text(json.dumps({"stack": stack, "add": add, "change": change, "destroy": destroy}, sort_keys=True))
        return PlanResult(
            stack=stack,
            ok=True,
            add=add,
            change=change,
            destroy=destroy,
            resource_changes=changes,
            plan_file=str(plan_file),
            plan_hash=_sha256(plan_file),
            summary=f"Plan: {add} to add, {change} to change, {destroy} to destroy. (mock)",
            mocked=True,
        )

    def plan(self, stack: str, stack_dir: Path) -> PlanResult:
        if self.mock:
            return self._mock_plan(stack)

        init = self._run(["init", "-input=false", "-no-color"], stack_dir)
        if not init.ok:
            return PlanResult(stack=stack, ok=False, summary="terraform init failed",
                              stdout=init.stdout, stderr=init.stderr)

        plan_file = self.plan_dir / f"{stack.replace('/', '__')}.tfplan"
        res = self._run(
            ["plan", "-input=false", "-no-color", "-detailed-exitcode", f"-out={plan_file}"],
            stack_dir,
        )
        # detailed-exitcode: 0=no changes, 2=changes, 1=error
        if res.rc == 1:
            return PlanResult(stack=stack, ok=False, summary="terraform plan failed",
                              stdout=res.stdout, stderr=res.stderr)

        show = self._run(["show", "-json", str(plan_file)], stack_dir)
        changes: list[ResourceChange] = []
        if show.ok:
            try:
                data = json.loads(show.stdout)
                for rc in data.get("resource_changes", []) or []:
                    actions = rc.get("change", {}).get("actions", []) or []
                    if actions == ["no-op"]:
                        continue
                    changes.append(ResourceChange(rc.get("address", "?"), rc.get("type", "?"), actions))
            except json.JSONDecodeError:
                pass

        add, change, destroy = _count(changes)
        return PlanResult(
            stack=stack,
            ok=True,
            add=add,
            change=change,
            destroy=destroy,
            resource_changes=changes,
            plan_file=str(plan_file),
            plan_hash=_sha256(plan_file),
            summary=f"Plan: {add} to add, {change} to change, {destroy} to destroy.",
            stdout=res.stdout,
            stderr=res.stderr,
        )

    # ----- apply -------------------------------------------------------
    def apply(self, stack: str, stack_dir: Path, plan_file: str, expected_hash: str) -> CommandResult:
        """Apply a previously-saved plan file, verifying its hash first."""
        pf = Path(plan_file)
        if not self.mock:
            if not pf.exists():
                return CommandResult(1, "", f"plan file missing: {plan_file}")
            if _sha256(pf) != expected_hash:
                return CommandResult(1, "", "plan hash mismatch — refusing to apply a changed plan")
            return self._run(["apply", "-input=false", "-no-color", "-auto-approve", str(pf)], stack_dir)

        # mock apply
        if not pf.exists() or _sha256(pf) != expected_hash:
            return CommandResult(1, "", "mock: plan hash mismatch")
        return CommandResult(0, f"mock: applied {stack} from {pf.name}", "")
