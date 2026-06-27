"""TerraPilot MCP server (FastMCP, stdio).

Exposes the governed Terraform pipeline as MCP tools so an AI assistant
(Claude, etc.) can drive infrastructure changes safely:

    Prompt -> AI -> Terraform -> Plan -> Approval -> Apply

Run with:  python -m terrapilot.server      (or `terrapilot serve`)
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .engine import EngineError, StackNotFound, TerraPilotEngine

mcp: FastMCP = FastMCP(
    name="TerraPilot",
    instructions=(
        "TerraPilot is a policy-governed Terraform automation server (an in-house Atlantis). "
        "Typical flow: list_stacks -> describe_stack -> propose_change. "
        "propose_change runs validate + plan + the policy engine. In DEV it auto-applies. "
        "In PROD it opens a GitHub PR and a Slack approval request and returns a request_id; "
        "a lead must approve (approve, or merge the PR) before apply_change will run. "
        "Never bypass the approval gate for prod. Use plan_stack for a read-only preview."
    ),
)

_engine: TerraPilotEngine | None = None


def get_engine() -> TerraPilotEngine:
    global _engine
    if _engine is None:
        _engine = TerraPilotEngine()
    return _engine


def _safe(fn) -> dict[str, Any]:
    try:
        return fn()
    except StackNotFound as exc:
        return {"error": "stack_not_found", "message": str(exc)}
    except EngineError as exc:
        return {"error": "engine_error", "message": str(exc)}


@mcp.tool
def list_stacks(env: str = "", query: str = "", limit: int = 50) -> dict:
    """List discoverable Terraform stacks. Filter by env ('dev'/'prod') or a path/name substring."""
    return get_engine().list_stacks(env=env, query=query, limit=limit)


@mcp.tool
def describe_stack(stack: str) -> dict:
    """Show a stack's environment, backend, and the policy that governs it."""
    return _safe(lambda: get_engine().describe_stack(stack))


@mcp.tool
def validate_stack(stack: str) -> dict:
    """Run terraform fmt + validate on a stack (offline-safe, no AWS calls)."""
    return _safe(lambda: get_engine().validate_stack(stack))


@mcp.tool
def plan_stack(stack: str) -> dict:
    """Run a read-only terraform plan and return the change summary + policy decision preview."""
    return _safe(lambda: get_engine().plan_stack(stack))


@mcp.tool
def propose_change(stack: str, requested_by: str = "", change_summary: str = "") -> dict:
    """Propose a change to a stack: validate -> plan -> policy.

    DEV stacks are auto-applied. PROD stacks open a GitHub PR + Slack approval
    request and return a request_id that must be approved before apply_change.
    Plans that destroy protected resources are BLOCKED.
    """
    return _safe(lambda: get_engine().propose_change(stack, requested_by=requested_by, change_summary=change_summary))


@mcp.tool
def approve_change(request_id: str, token: str, approver: str) -> dict:
    """Approve a pending prod change request (simulates a lead's sign-off / PR merge)."""
    return get_engine().approve(request_id, token, approver)


@mcp.tool
def reject_change(request_id: str, approver: str) -> dict:
    """Reject a pending change request."""
    return get_engine().reject(request_id, approver)


@mcp.tool
def apply_change(request_id: str, applied_by: str = "") -> dict:
    """Apply an APPROVED change request. Refuses pending/blocked requests and verifies the plan hash."""
    return _safe(lambda: get_engine().apply_change(request_id, applied_by=applied_by))


@mcp.tool
def list_requests(status: str = "") -> dict:
    """List change requests, optionally filtered by status (pending/approved/applied/rejected/blocked)."""
    return get_engine().list_requests(status=status)


@mcp.tool
def audit_log(limit: int = 20) -> dict:
    """Return the most recent entries from TerraPilot's audit trail."""
    return get_engine().audit_tail(limit=limit)


def main() -> None:
    mcp.run()  # stdio transport by default


if __name__ == "__main__":
    main()
