"""Stack discovery.

A "stack" is a single Terraform root module (a directory you run
``terraform init/plan/apply`` in). We treat the repo's Atlantis project
files (``atlantis-dev.yaml`` / ``atlantis-prod.yaml``) as the source of
truth, since that is what the team already maintains. If those are absent we
fall back to scanning for directories that contain a backend file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ATLANTIS_FILES = ("atlantis-dev.yaml", "atlantis-prod.yaml", "atlantis.yaml")
BACKEND_MARKERS = ("_backend.tf", "backend.tf")


@dataclass(frozen=True)
class Stack:
    path: str          # repo-relative directory, e.g. terraform/aws/dev/.../datadog
    name: str          # human label (Atlantis name, or the path)
    source: str        # "atlantis" | "scan"

    @property
    def key(self) -> str:
        return self.path


def _from_atlantis(repo: Path) -> dict[str, Stack]:
    stacks: dict[str, Stack] = {}
    for fname in ATLANTIS_FILES:
        f = repo / fname
        if not f.exists():
            continue
        data = yaml.safe_load(f.read_text()) or {}
        for proj in data.get("projects", []) or []:
            if not isinstance(proj, dict):
                continue
            d = proj.get("dir")
            if not isinstance(d, str):
                continue
            d = d.strip().lstrip("./")
            # The anchor/default entry uses a non-path placeholder (contains "->").
            if not d.startswith("terraform/") or "->" in d:
                continue
            name = proj.get("name") or d
            stacks[d] = Stack(path=d, name=str(name), source="atlantis")
    return stacks


def _from_scan(repo: Path) -> dict[str, Stack]:
    stacks: dict[str, Stack] = {}
    tf_root = repo / "terraform"
    if not tf_root.exists():
        return stacks
    for marker in BACKEND_MARKERS:
        for backend in tf_root.rglob(marker):
            d = backend.parent.relative_to(repo).as_posix()
            if "/.terraform" in f"/{d}":
                continue
            stacks.setdefault(d, Stack(path=d, name=d, source="scan"))
    return stacks


def discover_stacks(repo: Path) -> list[Stack]:
    stacks = _from_atlantis(repo)
    if not stacks:
        stacks = _from_scan(repo)
    return sorted(stacks.values(), key=lambda s: s.path)


def resolve_stack(repo: Path, identifier: str) -> Stack | None:
    """Resolve a stack by exact path, suffix, or unique substring match."""
    ident = identifier.strip().lstrip("./").rstrip("/")
    stacks = discover_stacks(repo)
    by_path = {s.path: s for s in stacks}
    if ident in by_path:
        return by_path[ident]

    suffix = [s for s in stacks if s.path.endswith(ident)]
    if len(suffix) == 1:
        return suffix[0]

    contains = [s for s in stacks if ident in s.path or ident in s.name]
    if len(contains) == 1:
        return contains[0]
    return None
