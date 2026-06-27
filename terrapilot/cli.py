"""TerraPilot CLI.

A thin wrapper over the engine for humans and for the demo. The MCP server is
the primary interface for AI; this CLI is for operators, CI, and `terrapilot
demo` (the scripted end-to-end showcase used in the README/LinkedIn post).
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import EngineError, StackNotFound, TerraPilotEngine


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _rule(title: str) -> None:
    print(f"\n\033[1m{'─' * 3} {title} {'─' * (60 - len(title))}\033[0m")


def cmd_serve(_args) -> int:
    from .server import main as serve_main
    serve_main()
    return 0


def cmd_stacks(args) -> int:
    _print(TerraPilotEngine().list_stacks(env=args.env, query=args.query, limit=args.limit))
    return 0


def cmd_describe(args) -> int:
    _print(TerraPilotEngine().describe_stack(args.stack))
    return 0


def cmd_validate(args) -> int:
    _print(TerraPilotEngine().validate_stack(args.stack))
    return 0


def cmd_plan(args) -> int:
    _print(TerraPilotEngine().plan_stack(args.stack))
    return 0


def cmd_propose(args) -> int:
    _print(TerraPilotEngine().propose_change(args.stack, requested_by=args.by, change_summary=args.summary))
    return 0


def cmd_approve(args) -> int:
    _print(TerraPilotEngine().approve(args.request_id, args.token, args.by))
    return 0


def cmd_reject(args) -> int:
    _print(TerraPilotEngine().reject(args.request_id, args.by))
    return 0


def cmd_apply(args) -> int:
    _print(TerraPilotEngine().apply_change(args.request_id, applied_by=args.by))
    return 0


def cmd_requests(args) -> int:
    _print(TerraPilotEngine().list_requests(status=args.status))
    return 0


def cmd_audit(args) -> int:
    _print(TerraPilotEngine().audit_tail(limit=args.limit))
    return 0


def _auto_pick(eng: TerraPilotEngine, env: str) -> str:
    stacks = eng.list_stacks(env=env, limit=1)["stacks"]
    if not stacks:
        raise SystemExit(f"no '{env}' stacks found in the configured repo")
    return stacks[0]["stack"]


def cmd_demo(args) -> int:
    """Scripted end-to-end showcase against the configured repo (mock mode)."""
    eng = TerraPilotEngine()
    dev = args.dev_stack or _auto_pick(eng, "dev")
    prod = args.prod_stack or _auto_pick(eng, "prod")

    _rule("1. Discover stacks")
    stacks = eng.list_stacks(limit=5)
    print(f"Discovered {stacks['total']} stacks (showing 5):")
    for s in stacks["stacks"]:
        print(f"  [{s['env']:>4}] {s['stack']}")

    _rule(f"2. DEV change → auto-apply :  {dev}")
    dev_res = eng.propose_change(dev, requested_by="alice@dev", change_summary="bump dev config")
    print(f"  decision : {dev_res['decision']['action']}  ({dev_res['decision']['env']})")
    print(f"  plan     : {dev_res['plan']['summary']}")
    print(f"  status   : {dev_res['status']}  → {dev_res.get('message')}")

    _rule(f"3. PROD change → approval gate :  {prod}")
    prod_res = eng.propose_change(prod, requested_by="alice@dev", change_summary="prod network change")
    rid = prod_res["request_id"]
    print(f"  decision : {prod_res['decision']['action']}  ({prod_res['decision']['env']})")
    print(f"  plan     : {prod_res['plan']['summary']}")
    print(f"  request  : {rid}  (status={prod_res['status']})")
    if "pr" in prod_res:
        print(f"  PR       : {prod_res['pr']['url']}  (dry_run={prod_res['pr']['dry_run']})")
    if "slack" in prod_res:
        print(f"  slack    : {prod_res['slack']['ref']}  (dry_run={prod_res['slack']['dry_run']})")

    _rule("4. Apply BEFORE approval is refused")
    try:
        eng.apply_change(rid, applied_by="alice@dev")
        print("  ERROR: apply should have been refused!")
    except EngineError as exc:
        print(f"  ✋ refused as expected: {exc}")

    _rule("5. Lead signs off, then apply succeeds")
    token = prod_res["approval"]["token"]
    appr = eng.approve(rid, token, approver="lead@platform")
    print(f"  approve  : {appr['message']} (status={appr['status']})")
    applied = eng.apply_change(rid, applied_by="lead@platform")
    print(f"  apply    : status={applied['status']}  ✅")

    _rule("6. Audit trail")
    for e in eng.audit_tail(limit=8)["entries"]:
        print(f"  {e['ts']}  {e['actor']:>14}  {e['action']:<20} {e['stack']}")

    print("\n\033[1m✓ End-to-end pipeline demonstrated.\033[0m\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="terrapilot", description="AI-native, policy-governed Terraform automation.")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the MCP server (stdio)").set_defaults(func=cmd_serve)

    s = sub.add_parser("stacks", help="list stacks")
    s.add_argument("--env", default="")
    s.add_argument("--query", default="")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_stacks)

    for name, fn, help_ in [("describe", cmd_describe, "describe a stack"),
                            ("validate", cmd_validate, "fmt + validate a stack"),
                            ("plan", cmd_plan, "plan a stack")]:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("stack")
        sp.set_defaults(func=fn)

    pr = sub.add_parser("propose", help="propose a change (validate+plan+policy)")
    pr.add_argument("stack")
    pr.add_argument("--by", default="")
    pr.add_argument("--summary", default="")
    pr.set_defaults(func=cmd_propose)

    ap = sub.add_parser("approve", help="approve a pending request")
    ap.add_argument("request_id")
    ap.add_argument("--token", required=True)
    ap.add_argument("--by", required=True)
    ap.set_defaults(func=cmd_approve)

    rj = sub.add_parser("reject", help="reject a request")
    rj.add_argument("request_id")
    rj.add_argument("--by", required=True)
    rj.set_defaults(func=cmd_reject)

    apl = sub.add_parser("apply", help="apply an approved request")
    apl.add_argument("request_id")
    apl.add_argument("--by", default="")
    apl.set_defaults(func=cmd_apply)

    rq = sub.add_parser("requests", help="list change requests")
    rq.add_argument("--status", default="")
    rq.set_defaults(func=cmd_requests)

    au = sub.add_parser("audit", help="show audit log")
    au.add_argument("--limit", type=int, default=20)
    au.set_defaults(func=cmd_audit)

    dm = sub.add_parser("demo", help="run the scripted end-to-end showcase")
    dm.add_argument("--dev-stack", default="", help="dev stack (auto-picked if omitted)")
    dm.add_argument("--prod-stack", default="", help="prod stack (auto-picked if omitted)")
    dm.set_defaults(func=cmd_demo)

    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except (EngineError, StackNotFound) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
