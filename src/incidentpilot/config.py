"""Runtime configuration, resolved from the environment with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # --- Claude ---
    model: str = os.environ.get("INCIDENTPILOT_MODEL", "claude-opus-5")
    effort: str = os.environ.get("INCIDENTPILOT_EFFORT", "high")
    max_tokens: int = int(os.environ.get("INCIDENTPILOT_MAX_TOKENS", "16000"))
    max_agent_turns: int = int(os.environ.get("INCIDENTPILOT_MAX_TURNS", "14"))

    # --- Repo under investigation ---
    repo_path: Path = Path(os.environ.get("INCIDENTPILOT_REPO", str(ROOT / "eval/fixtures/repo")))

    # --- Correlation window ---
    lookback_hours: float = float(os.environ.get("INCIDENTPILOT_LOOKBACK_HOURS", "72"))
    max_candidates: int = int(os.environ.get("INCIDENTPILOT_MAX_CANDIDATES", "12"))

    # --- RAG ---
    runbook_dir: Path = Path(os.environ.get("INCIDENTPILOT_RUNBOOKS", str(ROOT / "runbooks")))
    index_path: Path = Path(os.environ.get("INCIDENTPILOT_INDEX", str(ROOT / "data/runbooks.db")))
    embedding_backend: str = os.environ.get("INCIDENTPILOT_EMBEDDINGS", "auto")  # auto|voyage|hashed
    top_k: int = int(os.environ.get("INCIDENTPILOT_TOP_K", "5"))

    # --- Topology / impact ---
    topology_path: Path = Path(os.environ.get("INCIDENTPILOT_TOPOLOGY", str(ROOT / "data/topology.json")))

    # --- Slack ---
    slack_channel: str = os.environ.get("SLACK_CHANNEL", "#incidents")
    slack_post: bool = _bool("INCIDENTPILOT_SLACK_POST", False)

    # --- Output ---
    out_dir: Path = Path(os.environ.get("INCIDENTPILOT_OUT", str(ROOT / "out")))

    # --- Webhook ---
    webhook_token: str = os.environ.get("INCIDENTPILOT_WEBHOOK_TOKEN", "")

    @property
    def has_anthropic_key(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def load_config() -> Config:
    return Config()
