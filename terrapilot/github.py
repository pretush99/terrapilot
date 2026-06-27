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
from pathlib import Path

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


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


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
    repo_path: Path,
    branch: str,
    title: str,
    body: str,
    request_id: str,
    paths: list[str],
    commit_message: str,
) -> PRResult:
    """Open a real PR for the working-tree changes under ``paths``.

    Full flow: branch off the base, commit the stack's changed files, push, and
    ``gh pr create``. On any failure (including "nothing to commit") it restores
    the original branch and deletes the throwaway branch, so the working tree is
    never left in a surprising state. Git/gh auth is ambient (the process env /
    credential helper) — TerraPilot never handles tokens itself.
    """
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
        return PRResult(False, False, "", branch, title, body,
                        detail="gh CLI unavailable or not authenticated; cannot open PR.")

    repo = Path(repo_path)
    orig = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or cfg.base_branch

    def _fail(detail: str) -> PRResult:
        # best-effort restore: back to original branch, drop the throwaway branch
        _git(repo, "checkout", orig)
        _git(repo, "branch", "-D", branch)
        return PRResult(False, False, "", branch, title, body, detail=detail)

    cb = _git(repo, "checkout", "-b", branch)
    if cb.returncode != 0:
        return PRResult(False, False, "", branch, title, body,
                        detail=f"could not create branch '{branch}': {cb.stderr.strip()}")

    add = _git(repo, "add", "--", *paths)
    if add.returncode != 0:
        return _fail(f"git add failed: {add.stderr.strip()}")

    commit = _git(repo, "commit", "-m", commit_message)
    if commit.returncode != 0:
        return _fail("nothing to commit — no working-tree changes under the stack path")

    push = _git(repo, "push", "-u", "origin", branch)
    if push.returncode != 0:
        return _fail(f"git push failed: {push.stderr.strip()}")

    pr = subprocess.run(
        ["gh", "pr", "create", "-R", cfg.repo, "--head", branch, "--base", cfg.base_branch,
         "--title", title, "--body", body]
        + sum([["--reviewer", r] for r in cfg.reviewers], []),
        cwd=str(repo), capture_output=True, text=True,
    )
    # Always return to the original branch so the working tree is clean.
    _git(repo, "checkout", orig)

    if pr.returncode != 0:
        return PRResult(False, False, "", branch, title, body, detail=pr.stderr.strip())
    return PRResult(True, False, pr.stdout.strip(), branch, title, body)
