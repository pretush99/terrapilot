"""Slack approval notifications.

For prod changes TerraPilot posts an approval request to Slack so leads see it
immediately. ``dry_run`` (or a missing webhook) renders the exact message
instead of sending it, so the flow is demonstrable without a live workspace.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import SlackConfig


@dataclass
class SlackResult:
    sent: bool
    dry_run: bool
    message: str
    ref: str = ""
    detail: str = ""


def render_message(
    *, stack: str, env: str, request_id: str, summary: str, approvers: list[str], pr_url: str, token: str
) -> str:
    approver_str = " ".join(approvers) if approvers else "leads"
    pr_line = f"\n• PR: {pr_url}" if pr_url else ""
    return (
        f":lock: *TerraPilot — prod approval required*\n"
        f"• Stack: `{stack}`  (env: `{env}`)\n"
        f"• Change: {summary}\n"
        f"• Request: `{request_id}`{pr_line}\n"
        f"• Approvers: {approver_str}\n\n"
        f"Approve with:  `terrapilot approve {request_id} --token {token}`\n"
        f"or merge the PR above."
    )


def notify(cfg: SlackConfig, *, message: str, request_id: str) -> SlackResult:
    if cfg.dry_run or not cfg.enabled or not cfg.webhook_url:
        return SlackResult(
            sent=False,
            dry_run=True,
            message=message,
            ref=f"slack-draft-{request_id}",
            detail="draft mode: message rendered, not posted (no webhook / dry_run).",
        )
    try:
        resp = httpx.post(cfg.webhook_url, json={"channel": cfg.channel, "text": message}, timeout=10)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return SlackResult(False, False, message, detail=f"slack post failed: {exc}")
    return SlackResult(sent=True, dry_run=False, message=message, ref=f"slack-{request_id}")
