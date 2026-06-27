"""TerraPilot engine — the orchestration core.

This is provider-agnostic of the transport: the MCP server and the CLI both
call these methods. It implements the governed pipeline:

    propose_change -> (policy) -> auto-apply (dev)  |  request approval (prod)
    approve        -> mark a pending request approved
    apply_change   -> apply a saved, hash-pinned plan for an approved request
"""

from __future__ import annotations

import re
from pathlib import Path

from . import github as gh
from . import policy as pol
from . import slack as sl
from .audit import AuditLog
from .config import Config, load_config
from .discovery import discover_stacks, resolve_stack
from .runner import PlanResult, TerraformRunner
from .store import APPROVED, ChangeRequest, RequestStore


class StackNotFound(Exception):
    pass


class EngineError(Exception):
    pass


def _backend_info(stack_dir: Path) -> dict[str, str]:
    info: dict[str, str] = {}
    backend = stack_dir / "_backend.tf"
    if not backend.exists():
        backend = stack_dir / "backend.tf"
    if backend.exists():
        text = backend.read_text(errors="ignore")
        for field_ in ("bucket", "key", "region"):
            m = re.search(rf'{field_}\s*=\s*"([^"]+)"', text)
            if m:
                info[field_] = m.group(1)
    return info


class TerraPilotEngine:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or load_config()
        self.policy = pol.load_policy(self.cfg.policy_path)
        self.runner = TerraformRunner(
            repo=self.cfg.repo,
            binary=self.cfg.terraform_binary,
            aws_profile=self.cfg.aws_profile,
            mock=self.cfg.mock_mode,
            plan_dir=self.cfg.state_path / "plans",
        )
        self.store = RequestStore(self.cfg.state_path / "requests.json")
        self.audit = AuditLog(self.cfg.state_path / "audit.jsonl")

    # ---------- discovery ----------
    def _resolve(self, identifier: str):
        stack = resolve_stack(self.cfg.repo, identifier)
        if not stack:
            raise StackNotFound(
                f"could not resolve stack '{identifier}'. Use list_stacks to see options."
            )
        return stack

    def _env_of(self, stack_path: str) -> str:
        return pol.classify_env(self.policy, stack_path).name

    def list_stacks(self, env: str = "", query: str = "", limit: int = 50) -> dict:
        stacks = discover_stacks(self.cfg.repo)
        out = []
        for s in stacks:
            e = self._env_of(s.path)
            if env and e != env:
                continue
            if query and query not in s.path and query not in s.name:
                continue
            out.append({"stack": s.path, "name": s.name, "env": e, "source": s.source})
        total = len(out)
        return {"total": total, "shown": min(total, limit), "stacks": out[:limit]}

    def describe_stack(self, identifier: str) -> dict:
        s = self._resolve(identifier)
        stack_dir = self.cfg.repo / s.path
        env = pol.classify_env(self.policy, s.path)
        return {
            "stack": s.path,
            "name": s.name,
            "env": env.name,
            "exists": stack_dir.is_dir(),
            "backend": _backend_info(stack_dir),
            "policy": {
                "auto_apply": env.auto_apply,
                "require_approval": env.require_approval,
                "open_pr": env.open_pr,
                "notify_slack": env.notify_slack,
                "approvers": env.approvers,
            },
        }

    # ---------- terraform ----------
    def validate_stack(self, identifier: str) -> dict:
        s = self._resolve(identifier)
        stack_dir = self.cfg.repo / s.path
        fmt = self.runner.fmt_check(stack_dir)
        val = self.runner.validate(stack_dir)
        ok = fmt.ok and val.ok
        self.audit.record("validate", actor=self.cfg.actor, stack=s.path, ok=ok)
        return {
            "stack": s.path,
            "ok": ok,
            "fmt_ok": fmt.ok,
            "validate_ok": val.ok,
            "output": (fmt.stdout + val.stdout).strip(),
            "errors": (fmt.stderr + val.stderr).strip(),
            "mocked": self.cfg.mock_mode,
        }

    def plan_stack(self, identifier: str) -> dict:
        s = self._resolve(identifier)
        stack_dir = self.cfg.repo / s.path
        plan = self.runner.plan(s.path, stack_dir)
        decision = pol.evaluate(self.policy, s.path, plan)
        self.audit.record(
            "plan", actor=self.cfg.actor, stack=s.path,
            ok=plan.ok, add=plan.add, change=plan.change, destroy=plan.destroy,
            decision=decision.action,
        )
        return {**self._plan_dict(plan), "decision": decision.to_dict()}

    @staticmethod
    def _plan_dict(plan: PlanResult) -> dict:
        return {
            "stack": plan.stack,
            "ok": plan.ok,
            "summary": plan.summary,
            "add": plan.add,
            "change": plan.change,
            "destroy": plan.destroy,
            "has_changes": plan.has_changes,
            "resource_changes": [
                {"address": c.address, "type": c.type, "actions": c.actions}
                for c in plan.resource_changes
            ],
            "plan_hash": plan.plan_hash,
            "mocked": plan.mocked,
            "errors": plan.stderr.strip(),
        }

    # ---------- pipeline ----------
    def propose_change(self, identifier: str, requested_by: str = "", change_summary: str = "") -> dict:
        s = self._resolve(identifier)
        stack_dir = self.cfg.repo / s.path
        actor = requested_by or self.cfg.actor

        # Gate 1: validate
        val = self.runner.validate(stack_dir)
        if not val.ok:
            self.audit.record("propose.rejected", actor=actor, stack=s.path, reason="validate failed")
            raise EngineError(f"validation failed for {s.path}: {val.stderr.strip() or val.stdout.strip()}")

        # Gate 2: plan
        plan = self.runner.plan(s.path, stack_dir)
        if not plan.ok:
            self.audit.record("propose.rejected", actor=actor, stack=s.path, reason="plan failed")
            raise EngineError(f"plan failed for {s.path}: {plan.stderr.strip() or plan.summary}")

        # Gate 3: policy
        decision = pol.evaluate(self.policy, s.path, plan)

        blocked = decision.action == pol.BLOCKED
        auto = decision.action == pol.AUTO_APPLY
        cr = self.store.create(
            stack=s.path,
            env=decision.env,
            action=decision.action,
            plan_hash=plan.plan_hash,
            plan_file=plan.plan_file,
            summary=plan.summary,
            reasons=decision.reasons,
            requested_by=actor,
            approvers=decision.approvers,
            ttl_minutes=decision.approval_ttl_minutes,
            auto_approved=auto,
            blocked=blocked,
        )
        self.audit.record(
            "propose", actor=actor, stack=s.path, request_id=cr.id,
            decision=decision.action, summary=change_summary,
        )

        result: dict = {
            "request_id": cr.id,
            "stack": s.path,
            "env": decision.env,
            "decision": decision.to_dict(),
            "plan": self._plan_dict(plan),
            "status": cr.status,
        }

        if blocked:
            result["message"] = "BLOCKED by policy — see decision.reasons. No PR opened, no apply."
            return result

        if auto:
            # dev: do everything and deploy
            applied = self._apply(cr, applied_by=actor)
            result["applied"] = applied
            result["status"] = cr.status
            result["message"] = "dev policy: auto-approved and applied."
            return result

        # prod: request human sign-off via PR + Slack
        plan_excerpt = "\n".join(
            f"{'~' if 'update' in c.actions else '-' if 'delete' in c.actions else '+'} {c.address}"
            for c in plan.resource_changes
        )
        if decision.require_pr:
            branch = f"terrapilot/{cr.id}"
            title = f"[TerraPilot] {s.path}: {plan.summary}"
            body = gh.render_pr_body(s.path, decision.env, plan.summary, decision.reasons, plan_excerpt)
            pr = gh.open_pr(self.cfg.github, branch=branch, title=title, body=body, request_id=cr.id)
            cr.pr_url = pr.url
            result["pr"] = {"url": pr.url, "created": pr.created, "dry_run": pr.dry_run, "detail": pr.detail}

        if decision.require_slack:
            msg = sl.render_message(
                stack=s.path, env=decision.env, request_id=cr.id, summary=plan.summary,
                approvers=decision.approvers, pr_url=cr.pr_url, token=cr.token,
            )
            res = sl.notify(self.cfg.slack, message=msg, request_id=cr.id)
            cr.slack_ref = res.ref
            result["slack"] = {"sent": res.sent, "dry_run": res.dry_run, "ref": res.ref, "message": res.message}

        self.store.update(cr)
        self.audit.record(
            "approval_requested", actor=actor, stack=s.path, request_id=cr.id,
            pr_url=cr.pr_url, slack_ref=cr.slack_ref,
        )
        result["approval"] = {
            "request_id": cr.id,
            "token": cr.token,
            "approvers": cr.approvers,
            "expires_at": cr.expires_at,
            "how_to_approve": f"terrapilot approve {cr.id} --token <token>  (or merge the PR)",
        }
        result["message"] = "prod policy: approval required. PR opened and Slack notified."
        return result

    def approve(self, request_id: str, token: str, approver: str) -> dict:
        ok, msg, cr = self.store.approve(request_id, token, approver)
        self.audit.record(
            "approve", actor=approver, stack=cr.stack if cr else "",
            request_id=request_id, ok=ok, detail=msg,
        )
        return {"ok": ok, "message": msg, "status": cr.status if cr else None, "request_id": request_id}

    def reject(self, request_id: str, approver: str) -> dict:
        ok, msg, cr = self.store.reject(request_id, approver)
        self.audit.record("reject", actor=approver, stack=cr.stack if cr else "", request_id=request_id, ok=ok)
        return {"ok": ok, "message": msg, "status": cr.status if cr else None, "request_id": request_id}

    def apply_change(self, request_id: str, applied_by: str = "") -> dict:
        cr = self.store.get(request_id)
        if not cr:
            raise EngineError(f"change request '{request_id}' not found")
        if cr.status == "applied":
            return {"ok": True, "message": "already applied", "request_id": request_id, "status": cr.status}
        if cr.status != APPROVED:
            raise EngineError(
                f"refusing to apply: request '{request_id}' is '{cr.status}', not approved. "
                f"prod changes require sign-off."
            )
        return self._apply(cr, applied_by=applied_by or self.cfg.actor)

    def _apply(self, cr: ChangeRequest, applied_by: str) -> dict:
        stack_dir = self.cfg.repo / cr.stack
        res = self.runner.apply(cr.stack, stack_dir, cr.plan_file, cr.plan_hash)
        if not res.ok:
            self.audit.record("apply.failed", actor=applied_by, stack=cr.stack, request_id=cr.id, detail=res.stderr)
            raise EngineError(f"apply failed for {cr.stack}: {res.stderr.strip() or res.stdout.strip()}")
        self.store.mark_applied(cr)
        self.audit.record("apply", actor=applied_by, stack=cr.stack, request_id=cr.id, ok=True)
        return {
            "ok": True,
            "request_id": cr.id,
            "stack": cr.stack,
            "status": "applied",
            "output": res.stdout.strip(),
            "mocked": self.cfg.mock_mode,
        }

    # ---------- introspection ----------
    def list_requests(self, status: str = "") -> dict:
        items = self.store.list(status)
        return {
            "count": len(items),
            "requests": [
                {
                    "id": c.id, "stack": c.stack, "env": c.env, "status": c.status,
                    "action": c.action, "summary": c.summary, "requested_by": c.requested_by,
                    "approved_by": c.approved_by, "pr_url": c.pr_url,
                }
                for c in items
            ],
        }

    def audit_tail(self, limit: int = 20) -> dict:
        return {"entries": self.audit.tail(limit)}
