"""GitHub integration (via the ``gh`` CLI).

For prod changes TerraPilot opens a PR whose body contains the plan, so the
existing CODEOWNERS + branch-protection rules become the merge gate — the same
audit trail the team already trusts.

Safety: ``dry_run`` (the default) renders the branch/commit/PR it *would*
create and returns a simulated URL, without ever touching git history or
pushing. Flip it off only with a dedicated bot token and branch protection in
place. We never commit Terraform state or plan files.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from .config import GitHubConfig


@dataclass
class PRResult:
    created: bool
    dry_run: bool
    url: str
    branch: str
    title: str
    body: str
    detail: str = ""


def gh_available() -> bool:
    if not shutil.which("gh"):
        return False
    res = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    return res.returncode == 0


def render_pr_body(stack: str, env: str, summary: str, reasons: list[str], plan_excerpt: str) -> str:
    reason_lines = "\n".join(f"- {r}" for r in reasons)
    return (
        f"## TerraPilot change request\n\n"
        f"**Stack:** `{stack}`\n"
        f"**Environment:** `{env}`\n\n"
        f"### Plan summary\n```\n{summary}\n```\n\n"
        f"### Policy decision\n{reason_lines}\n\n"
        f"### Plan detail\n```\n{plan_excerpt or '(no detail captured)'}\n```\n\n"
        f"---\n"
        f"> Merging this PR (lead approval via CODEOWNERS) authorizes TerraPilot to apply the plan above.\n"
    )


def open_pr(
    cfg: GitHubConfig,
    *,
    branch: str,
    title: str,
    body: str,
    request_id: str,
) -> PRResult:
    if cfg.dry_run or not cfg.enabled:
        url = f"https://github.com/{cfg.repo}/pull/DRY-RUN-{request_id}"
        return PRResult(
            created=False,
            dry_run=True,
            url=url,
            branch=branch,
            title=title,
            body=body,
            detail="dry_run: PR not actually opened. Set github.dry_run=false to enable.",
        )

    if not gh_available():
        return PRResult(
            created=False,
            dry_run=False,
            url="",
            branch=branch,
            title=title,
            body=body,
            detail="gh CLI unavailable or not authenticated; cannot open PR.",
        )

    # Real path: create branch + push + open PR. The caller is responsible for
    # having committed the .tf changes onto `branch` first.
    res = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body, "--base", cfg.base_branch, "--head", branch]
        + sum([["--reviewer", r] for r in cfg.reviewers], []),
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return PRResult(False, False, "", branch, title, body, detail=res.stderr.strip())
    return PRResult(True, False, res.stdout.strip(), branch, title, body)
