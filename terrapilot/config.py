"""Configuration loading for TerraPilot.

Precedence (highest first): environment variables -> config.yaml -> defaults.
Environment variables are prefixed ``TERRAPILOT_`` and use upper snake case,
e.g. ``TERRAPILOT_MOCK_MODE=false`` or ``TERRAPILOT_REPO_PATH=/path/to/repo``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class GitHubConfig:
    enabled: bool = True
    repo: str = ""
    base_branch: str = "main"
    reviewers: list[str] = field(default_factory=list)
    dry_run: bool = True


@dataclass
class SlackConfig:
    enabled: bool = True
    webhook_url: str = ""
    channel: str = "#infra-approvals"
    dry_run: bool = True


@dataclass
class Config:
    repo_path: str = str(PROJECT_ROOT)
    aws_profile: str = "default"
    terraform_binary: str = "terraform"
    terraform_version: str = "1.14.3"
    state_dir: str = ".terrapilot"
    mock_mode: bool = True
    actor: str = "terrapilot-bot"
    policy_file: str = "policy.yaml"
    github: GitHubConfig = field(default_factory=GitHubConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)

    @property
    def repo(self) -> Path:
        return Path(self.repo_path).expanduser().resolve()

    @property
    def state_path(self) -> Path:
        p = Path(self.state_dir).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def policy_path(self) -> Path:
        p = Path(self.policy_file).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p


def _default_config_file() -> Path:
    override = os.environ.get("TERRAPILOT_CONFIG")
    if override:
        return Path(override).expanduser()
    return PROJECT_ROOT / "config.yaml"


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load configuration, layering file values then environment overrides."""
    cfg = Config()

    cfg_file = Path(path) if path else _default_config_file()
    if cfg_file.exists():
        data = yaml.safe_load(cfg_file.read_text()) or {}
        gh = data.pop("github", None) or {}
        sl = data.pop("slack", None) or {}
        cfg = replace(cfg, **{k: v for k, v in data.items() if hasattr(cfg, k)})
        cfg.github = GitHubConfig(**{k: v for k, v in gh.items() if hasattr(GitHubConfig(), k)})
        cfg.slack = SlackConfig(**{k: v for k, v in sl.items() if hasattr(SlackConfig(), k)})

    # Environment overrides (flat scalars only — nested via dedicated names).
    env = os.environ
    if "TERRAPILOT_REPO_PATH" in env:
        cfg.repo_path = env["TERRAPILOT_REPO_PATH"]
    if "TERRAPILOT_AWS_PROFILE" in env:
        cfg.aws_profile = env["TERRAPILOT_AWS_PROFILE"]
    if "TERRAPILOT_TERRAFORM_BINARY" in env:
        cfg.terraform_binary = env["TERRAPILOT_TERRAFORM_BINARY"]
    if "TERRAPILOT_STATE_DIR" in env:
        cfg.state_dir = env["TERRAPILOT_STATE_DIR"]
    if "TERRAPILOT_MOCK_MODE" in env:
        cfg.mock_mode = _as_bool(env["TERRAPILOT_MOCK_MODE"])
    if "TERRAPILOT_ACTOR" in env:
        cfg.actor = env["TERRAPILOT_ACTOR"]
    if "TERRAPILOT_GITHUB_DRY_RUN" in env:
        cfg.github.dry_run = _as_bool(env["TERRAPILOT_GITHUB_DRY_RUN"])
    if "TERRAPILOT_SLACK_DRY_RUN" in env:
        cfg.slack.dry_run = _as_bool(env["TERRAPILOT_SLACK_DRY_RUN"])
    if "TERRAPILOT_SLACK_WEBHOOK_URL" in env:
        cfg.slack.webhook_url = env["TERRAPILOT_SLACK_WEBHOOK_URL"]

    return cfg
