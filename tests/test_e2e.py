"""End-to-end tests driving the real MCP tools via FastMCP's in-memory client.

These run against a bundled synthetic Terraform repo (tests/fixtures/repo) in
mock mode — so they exercise discovery, the policy engine, the approval gate
and the integrations without invoking terraform or touching AWS. Set
TERRAPILOT_REPO_PATH to point the same flow at a real repo.
"""

from __future__ import annotations

import os
import tempfile

# Configure BEFORE importing the server (engine reads config on first use).
# The bundled fixture repo makes these tests self-contained — no AWS, no
# external repo required.
_FIXTURE_REPO = os.path.join(os.path.dirname(__file__), "fixtures", "repo")
os.environ["TERRAPILOT_REPO_PATH"] = _FIXTURE_REPO
os.environ["TERRAPILOT_MOCK_MODE"] = "true"
os.environ["TERRAPILOT_STATE_DIR"] = tempfile.mkdtemp(prefix="terrapilot-test-")
os.environ["TERRAPILOT_GITHUB_DRY_RUN"] = "true"
os.environ["TERRAPILOT_SLACK_DRY_RUN"] = "true"

from fastmcp import Client  # noqa: E402

from terrapilot.server import mcp  # noqa: E402

DEV_STACK = "terraform/aws/dev/us-east-1/dev/dynamodb/example-table"
PROD_STACK = "terraform/aws/prod/us-east-1/prod/s3/example-logs"


async def call(name: str, args: dict | None = None) -> dict:
    async with Client(mcp) as client:
        res = await client.call_tool(name, args or {})
        return res.data


async def test_discovery_finds_dev_and_prod():
    out = await call("list_stacks", {"limit": 500})
    assert out["total"] >= 4, "expected the fixture stacks to be discovered"
    envs = {s["env"] for s in out["stacks"]}
    assert "dev" in envs and "prod" in envs


async def test_describe_classifies_env_and_policy():
    out = await call("describe_stack", {"stack": DEV_STACK})
    assert out["env"] == "dev"
    assert out["policy"]["auto_apply"] is True

    out = await call("describe_stack", {"stack": PROD_STACK})
    assert out["env"] == "prod"
    assert out["policy"]["auto_apply"] is False
    assert out["policy"]["require_approval"] is True


async def test_validate_offline_ok():
    out = await call("validate_stack", {"stack": DEV_STACK})
    assert out["ok"] is True
    assert out["mocked"] is True


async def test_dev_change_auto_applies():
    os.environ.pop("TERRAPILOT_MOCK_PLAN", None)
    out = await call("propose_change", {"stack": DEV_STACK, "requested_by": "alice"})
    assert out["decision"]["action"] == "auto_apply"
    assert out["decision"]["env"] == "dev"
    assert out["status"] == "applied"
    assert out["applied"]["ok"] is True


async def test_prod_change_requires_signoff_then_applies():
    os.environ.pop("TERRAPILOT_MOCK_PLAN", None)
    out = await call("propose_change", {"stack": PROD_STACK, "requested_by": "alice"})
    assert out["decision"]["action"] == "require_approval"
    assert out["status"] == "pending"
    # PR + Slack drafted
    assert out["pr"]["dry_run"] is True
    assert out["slack"]["dry_run"] is True
    rid = out["request_id"]
    token = out["approval"]["token"]

    # Apply before approval must be refused.
    refused = await call("apply_change", {"request_id": rid, "applied_by": "alice"})
    assert refused.get("error") == "engine_error"

    # Wrong token rejected.
    bad = await call("approve_change", {"request_id": rid, "token": "wrong", "approver": "lead"})
    assert bad["ok"] is False

    # Correct token approves, then apply succeeds.
    ok = await call("approve_change", {"request_id": rid, "token": token, "approver": "lead@platform"})
    assert ok["ok"] is True and ok["status"] == "approved"

    applied = await call("apply_change", {"request_id": rid, "applied_by": "lead@platform"})
    assert applied["status"] == "applied"


async def test_guardrail_blocks_protected_destroy():
    os.environ["TERRAPILOT_MOCK_PLAN"] = '{"add":0,"change":0,"destroy":1,"types":["aws_db_instance"]}'
    try:
        out = await call("propose_change", {"stack": DEV_STACK, "requested_by": "alice"})
        assert out["decision"]["action"] == "blocked"
        assert out["status"] == "blocked"
        assert any("protected" in r for r in out["decision"]["reasons"])
    finally:
        os.environ.pop("TERRAPILOT_MOCK_PLAN", None)


async def test_guardrail_escalates_dev_destroy_to_approval():
    # A destroy of a non-protected type in dev must escalate to manual approval.
    os.environ["TERRAPILOT_MOCK_PLAN"] = '{"add":0,"change":0,"destroy":1,"types":["aws_iam_role_policy"]}'
    try:
        out = await call("propose_change", {"stack": DEV_STACK, "requested_by": "alice"})
        assert out["decision"]["action"] == "require_approval"
        assert out["status"] == "pending"
    finally:
        os.environ.pop("TERRAPILOT_MOCK_PLAN", None)
