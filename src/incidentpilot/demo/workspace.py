"""The repositories and pipelines the demo runs against.

The demo gets its own copy of the synthetic monorepo with the fresh bug planted in it, so
nothing it does can disturb the benchmark repo the evals read. The planted commits are
dated after every benchmark incident, and replays are pinned to each alert's onset, so
the benchmark incidents see exactly the history they always did.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from ..config import ROOT, load_config
from ..pipeline import IncidentPilot

log = logging.getLogger("incidentpilot.demo")

FIXTURE_REPO = ROOT / "eval" / "fixtures" / "repo"
DEMO_REPO = ROOT / "data" / "demo-repo"
REALWORLD_REPOS = ROOT / "eval" / "realworld" / "repos"
OUT_DIR = ROOT / "data" / "demo-out"


def _eval_module(name: str):
    sys.path.insert(0, str(ROOT / "eval"))
    try:
        return __import__(name)
    finally:
        sys.path.remove(str(ROOT / "eval"))


def ensure_demo_repo() -> Path:
    """Build the demo repo on first use: the benchmark monorepo plus the planted bug."""
    if (DEMO_REPO / ".git").exists():
        return DEMO_REPO
    if not (FIXTURE_REPO / ".git").exists():
        log.info("building the synthetic monorepo first")
        _eval_module("build_fixture_repo").build_repo()
    DEMO_REPO.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(FIXTURE_REPO), str(DEMO_REPO)], check=True)
    _eval_module("demo_new_bug").plant(DEMO_REPO)
    log.info("demo repo ready at %s", DEMO_REPO)
    return DEMO_REPO


def repo_path(repo_key: str) -> Path:
    return DEMO_REPO if repo_key == "demo" else REALWORLD_REPOS / repo_key


def repo_available(repo_key: str) -> bool:
    return (repo_path(repo_key) / ".git").exists()


class Pilots:
    """One IncidentPilot per repository, built on first use."""

    def __init__(self) -> None:
        self._pilots: dict[str, IncidentPilot] = {}

    def get(self, repo_key: str) -> IncidentPilot:
        if repo_key not in self._pilots:
            overrides = {"repo_path": repo_path(repo_key), "show_reasoning": True,
                         "out_dir": OUT_DIR}
            if repo_key != "demo":
                # The real repositories have no runbooks of ours worth injecting.
                overrides["top_k"] = 0
            self._pilots[repo_key] = IncidentPilot(load_config(**overrides))
        return self._pilots[repo_key]
