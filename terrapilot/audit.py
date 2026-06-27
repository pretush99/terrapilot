"""Append-only audit log.

Every state-changing action TerraPilot takes is recorded as one JSON line.
This is the local audit trail; in a hosted deployment it would ship to a
central logging pipeline, but the contract (who, what, when, on which stack)
is identical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class AuditLog:
    path: Path

    def record(self, action: str, *, actor: str, stack: str = "", **detail: Any) -> dict:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "action": action,
            "stack": stack,
            "detail": detail,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    def tail(self, limit: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:] if line.strip()]
