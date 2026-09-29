"""Runtime configuration, resolved from the environment with sane defaults.

Every field reads the environment when a Config is *constructed*, not when this module
is imported. The earlier version used plain `os.environ.get(...)` defaults, which Python
evaluates once at class definition -- so changing the environment after import had no
effect, and anything comparing configurations in one process (the hill-climb harness)
would have silently run every variant with the first one's settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> Any:
    return field(default_factory=lambda: os.environ.get(name, default))


def _env_int(name: str, default: int) -> Any:
    return field(default_factory=lambda: int(os.environ.get(name, str(default))))


def _env_float(name: str, default: float) -> Any:
    return field(default_factory=lambda: float(os.environ.get(name, str(default))))


def _env_bool(name: str, default: bool = False) -> Any:
    def read() -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return field(default_factory=read)


def _env_path(name: str, default: Path) -> Any:
    return field(default_factory=lambda: Path(os.environ.get(name, str(default))))


@dataclass(frozen=True)
class Config:
    # --- Claude ---
    model: str = _env("INCIDENTPILOT_MODEL", "claude-opus-5")
    effort: str = _env("INCIDENTPILOT_EFFORT", "high")
    max_tokens: int = _env_int("INCIDENTPILOT_MAX_TOKENS", 16000)
    max_agent_turns: int = _env_int("INCIDENTPILOT_MAX_TURNS", 14)

    # --- Agent behaviour (the knobs the hill-climb harness varies) ---
    # Inline the diffs of the top N candidates in the first prompt, saving the tool
    # round-trips the agent would otherwise spend fetching them one by one.
    prefetch_diffs: int = _env_int("INCIDENTPILOT_PREFETCH_DIFFS", 0)
    # Offer a tool that reads a whole file as it stood at a given commit, so the agent can
    # see what a changed constant actually controls rather than inferring it from a hunk.
    enable_read_file: bool = _env_bool("INCIDENTPILOT_ENABLE_READ_FILE", False)
    # Force human review whenever the agent's own confidence is below this. 0 disables.
    escalate_below: float = _env_float("INCIDENTPILOT_ESCALATE_BELOW", 0.0)

    # --- Repo under investigation ---
    repo_path: Path = _env_path("INCIDENTPILOT_REPO", ROOT / "eval/fixtures/repo")

    # --- Correlation window ---
    lookback_hours: float = _env_float("INCIDENTPILOT_LOOKBACK_HOURS", 72.0)
    max_candidates: int = _env_int("INCIDENTPILOT_MAX_CANDIDATES", 12)

    # --- RAG ---
    runbook_dir: Path = _env_path("INCIDENTPILOT_RUNBOOKS", ROOT / "runbooks")
    index_path: Path = _env_path("INCIDENTPILOT_INDEX", ROOT / "data/runbooks.db")
    embedding_backend: str = _env("INCIDENTPILOT_EMBEDDINGS", "auto")  # auto|voyage|hashed
    top_k: int = _env_int("INCIDENTPILOT_TOP_K", 5)

    # --- Topology / impact ---
    topology_path: Path = _env_path("INCIDENTPILOT_TOPOLOGY", ROOT / "data/topology.json")

    # --- Slack ---
    slack_channel: str = _env("SLACK_CHANNEL", "#incidents")
    slack_post: bool = _env_bool("INCIDENTPILOT_SLACK_POST", False)

    # --- Output ---
    out_dir: Path = _env_path("INCIDENTPILOT_OUT", ROOT / "out")

    # --- Webhook ---
    webhook_token: str = _env("INCIDENTPILOT_WEBHOOK_TOKEN", "")

    @property
    def has_anthropic_key(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def load_config(**overrides: Any) -> Config:
    """Resolve config from the environment now, then apply explicit overrides."""
    config = Config()
    return replace(config, **overrides) if overrides else config
