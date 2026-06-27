"""Policy engine — env-aware governance, enforced in code.

Given a stack path and a plan, it decides one of three actions:

* ``auto_apply``      — safe to apply without a human (dev, no guardrail trip)
* ``require_approval``— must get lead sign-off (prod, or an escalated dev change)
* ``blocked``         — refused outright (destroying a protected resource type)

Guardrails always run and can only ever make a decision *more* restrictive,
never less. Prod can never be downgraded to auto_apply.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .runner import PlanResult

AUTO_APPLY = "auto_apply"
REQUIRE_APPROVAL = "require_approval"
BLOCKED = "blocked"


@dataclass
class EnvPolicy:
    name: str
    match_paths: list[str]
    auto_apply: bool
    require_approval: bool
    open_pr: bool
    notify_slack: bool
    approvers: list[str]
    approval_ttl_minutes: int


@dataclass
class Guardrails:
    max_destroy_auto: int
    protected_paths: list[str]
    protected_resource_types: list[str]
    block_on_protected_destroy: bool


@dataclass
class Policy:
    environments: list[EnvPolicy]
    default_environment: str
    guardrails: Guardrails


@dataclass
class Decision:
    env: str
    action: str
    reasons: list[str] = field(default_factory=list)
    require_pr: bool = False
    require_slack: bool = False
    approvers: list[str] = field(default_factory=list)
    approval_ttl_minutes: int = 60

    def to_dict(self) -> dict:
        return {
            "env": self.env,
            "action": self.action,
            "reasons": self.reasons,
            "require_pr": self.require_pr,
            "require_slack": self.require_slack,
            "approvers": self.approvers,
            "approval_ttl_minutes": self.approval_ttl_minutes,
        }


def load_policy(path: Path) -> Policy:
    data = yaml.safe_load(path.read_text()) or {}
    envs = [
        EnvPolicy(
            name=e["name"],
            match_paths=e.get("match_paths", []),
            auto_apply=bool(e.get("auto_apply", False)),
            require_approval=bool(e.get("require_approval", True)),
            open_pr=bool(e.get("open_pr", False)),
            notify_slack=bool(e.get("notify_slack", False)),
            approvers=e.get("approvers", []),
            approval_ttl_minutes=int(e.get("approval_ttl_minutes", 60)),
        )
        for e in data.get("environments", [])
    ]
    g = data.get("guardrails", {}) or {}
    guardrails = Guardrails(
        max_destroy_auto=int(g.get("max_destroy_auto", 0)),
        protected_paths=g.get("protected_paths", []),
        protected_resource_types=g.get("protected_resource_types", []),
        block_on_protected_destroy=bool(g.get("block_on_protected_destroy", True)),
    )
    return Policy(
        environments=envs,
        default_environment=data.get("default_environment", "prod"),
        guardrails=guardrails,
    )


def classify_env(policy: Policy, stack_path: str) -> EnvPolicy:
    norm = f"/{stack_path.strip('/')}/"
    for env in policy.environments:
        for pat in env.match_paths:
            if pat in stack_path or pat in norm:
                return env
    # fall back to the named default (most restrictive)
    for env in policy.environments:
        if env.name == policy.default_environment:
            return env
    return policy.environments[-1]


def _matches_protected_path(stack_path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(stack_path, pat) for pat in patterns)


def evaluate(policy: Policy, stack_path: str, plan: PlanResult) -> Decision:
    env = classify_env(policy, stack_path)
    g = policy.guardrails
    reasons: list[str] = []

    # Baseline from environment policy.
    if env.auto_apply and not env.require_approval:
        action = AUTO_APPLY
        reasons.append(f"{env.name}: environment policy permits auto-apply")
    else:
        action = REQUIRE_APPROVAL
        reasons.append(f"{env.name}: environment policy requires sign-off")

    # --- Guardrails (can only escalate) ---
    protected_destroys = [
        c for c in plan.resource_changes
        if (c.is_destroy or c.is_replace) and c.type in g.protected_resource_types
    ]
    if protected_destroys and g.block_on_protected_destroy:
        action = BLOCKED
        names = ", ".join(sorted({c.type for c in protected_destroys}))
        reasons.append(f"BLOCKED: plan destroys/replaces protected resource type(s): {names}")

    if action != BLOCKED:
        if _matches_protected_path(stack_path, g.protected_paths):
            if action == AUTO_APPLY:
                action = REQUIRE_APPROVAL
            reasons.append("escalated: stack matches a protected path — sign-off required")

        if action == AUTO_APPLY and plan.destroy > g.max_destroy_auto:
            action = REQUIRE_APPROVAL
            reasons.append(
                f"escalated: plan destroys {plan.destroy} resource(s) "
                f"(> max_destroy_auto={g.max_destroy_auto}) — sign-off required"
            )

    if not plan.has_changes:
        reasons.append("note: plan is a no-op (no changes)")

    return Decision(
        env=env.name,
        action=action,
        reasons=reasons,
        require_pr=env.open_pr and action == REQUIRE_APPROVAL,
        require_slack=env.notify_slack and action == REQUIRE_APPROVAL,
        approvers=env.approvers,
        approval_ttl_minutes=env.approval_ttl_minutes,
    )
