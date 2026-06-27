"""Change-request store.

A ChangeRequest is the unit the whole pipeline revolves around. It binds a
plan (by hash) to a policy decision and a lifecycle status, and is what
``apply`` consumes. Persisted as JSON so the flow survives a server restart;
in a hosted deployment this becomes a real database, same schema.

Status lifecycle:
    pending   -> approved -> applied
              -> rejected
    auto-approved (dev) -> applied
    blocked   (terminal)
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
APPLIED = "applied"
BLOCKED = "blocked"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ChangeRequest:
    id: str
    stack: str
    env: str
    action: str                 # policy action that produced this request
    status: str
    plan_hash: str
    plan_file: str
    summary: str
    reasons: list[str] = field(default_factory=list)
    requested_by: str = ""
    requested_at: str = ""
    token: str = ""             # single-use, bound to plan_hash
    expires_at: str = ""
    approvers: list[str] = field(default_factory=list)
    approved_by: str = ""
    decided_at: str = ""
    pr_url: str = ""
    slack_ref: str = ""

    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        return _now() > datetime.fromisoformat(self.expires_at)


class RequestStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        if not self.path.exists():
            self.path.write_text("{}")

    def _load(self) -> dict[str, dict]:
        return json.loads(self.path.read_text() or "{}")

    def _save(self, data: dict[str, dict]) -> None:
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def create(
        self,
        *,
        stack: str,
        env: str,
        action: str,
        plan_hash: str,
        plan_file: str,
        summary: str,
        reasons: list[str],
        requested_by: str,
        approvers: list[str],
        ttl_minutes: int,
        auto_approved: bool,
        blocked: bool = False,
    ) -> ChangeRequest:
        rid = "cr-" + secrets.token_hex(5)
        now = _now()
        status = BLOCKED if blocked else (APPROVED if auto_approved else PENDING)
        cr = ChangeRequest(
            id=rid,
            stack=stack,
            env=env,
            action=action,
            status=status,
            plan_hash=plan_hash,
            plan_file=plan_file,
            summary=summary,
            reasons=reasons,
            requested_by=requested_by,
            requested_at=now.isoformat(),
            token="" if (auto_approved or blocked) else secrets.token_urlsafe(16),
            expires_at="" if (auto_approved or blocked) else (now + timedelta(minutes=ttl_minutes)).isoformat(),
            approvers=approvers,
            approved_by="auto (dev policy)" if auto_approved else "",
            decided_at=now.isoformat() if auto_approved else "",
        )
        data = self._load()
        data[rid] = asdict(cr)
        self._save(data)
        return cr

    def get(self, rid: str) -> ChangeRequest | None:
        data = self._load()
        raw = data.get(rid)
        return ChangeRequest(**raw) if raw else None

    def update(self, cr: ChangeRequest) -> None:
        data = self._load()
        data[cr.id] = asdict(cr)
        self._save(data)

    def list(self, status: str = "") -> list[ChangeRequest]:
        data = self._load()
        items = [ChangeRequest(**v) for v in data.values()]
        if status:
            items = [c for c in items if c.status == status]
        return sorted(items, key=lambda c: c.requested_at, reverse=True)

    # --- transitions ---
    def approve(self, rid: str, token: str, approver: str) -> tuple[bool, str, ChangeRequest | None]:
        cr = self.get(rid)
        if not cr:
            return False, "change request not found", None
        if cr.status == BLOCKED:
            return False, "change request is blocked by policy and cannot be approved", cr
        if cr.status != PENDING:
            return False, f"change request is '{cr.status}', not pending", cr
        if cr.is_expired():
            cr.status = REJECTED
            cr.decided_at = _now().isoformat()
            self.update(cr)
            return False, "approval window expired", cr
        if not secrets.compare_digest(token, cr.token):
            return False, "invalid approval token", cr
        cr.status = APPROVED
        cr.approved_by = approver
        cr.decided_at = _now().isoformat()
        self.update(cr)
        return True, "approved", cr

    def reject(self, rid: str, approver: str) -> tuple[bool, str, ChangeRequest | None]:
        cr = self.get(rid)
        if not cr:
            return False, "change request not found", None
        if cr.status not in (PENDING, APPROVED):
            return False, f"cannot reject a '{cr.status}' request", cr
        cr.status = REJECTED
        cr.approved_by = approver
        cr.decided_at = _now().isoformat()
        self.update(cr)
        return True, "rejected", cr

    def mark_applied(self, cr: ChangeRequest) -> None:
        cr.status = APPLIED
        self.update(cr)
