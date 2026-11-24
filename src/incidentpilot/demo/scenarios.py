"""The incidents the demo offers, in the order a presenter would walk through them.

Each scenario points at a real incident from one of the eval suites (or the planted bug
that is in none of them), so the demo shows exactly what the benchmark measures and its
known answer can be revealed after the verdict.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config import ROOT

EVAL = ROOT / "eval"
SUITE_FILES = {
    "synthetic": EVAL / "incidents.json",
    "negative": EVAL / "negatives.json",
    "realworld": EVAL / "realworld" / "incidents.json",
}
FRESH_BUG_SUBJECT = "fix: fail closed when the cache backend is unreachable"


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str                 # what the alert says, shortened for the list
    hook: str                  # one sentence the presenter can say before running it
    suite: str                 # synthetic | fresh | negative | realworld
    source: str                # where the incident comes from, for the "known answer" line
    repo: str                  # "demo" (the synthetic monorepo) or a real-world clone's name
    alert: dict[str, Any] = field(hash=False)
    answer_sha: str | None = None
    answer_subject: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "hook": self.hook, "suite": self.suite,
            "source": self.source, "repo": self.repo, "alert": self.alert,
            "answer_subject": self.answer_subject,
        }


# (suite, incident id in that suite's file, short label for the list or "" to use the
# alert's title, hook). The demo shows real open-source regressions: each was reverted
# upstream, and the revert names the commit it undid, so the known answer comes from the
# maintainers rather than from us. Their alerts are the revert's own words, which make poor
# list labels, hence the short ones. Hooks never give the answer away. Synthetic, fresh and
# negative scenarios still work here if added back.
CURATED: list[tuple[str, str, str, str]] = [
    ("realworld", "RW-moby-03", "Breaks on case-insensitive filesystems",
     "Docker's engine broke on case-insensitive filesystems. 56 commits landed in the window."),
    ("realworld", "RW-grafana-01", "Won't compile on 32-bit systems",
     "Grafana stopped compiling on 32-bit systems. The report names a library, not a commit."),
    ("realworld", "RW-moby-01", "Breaks docker/cli formatting",
     "An engine change caused problems in another repository: the docker/cli formatting code."),
    ("realworld", "RW-prometheus-01", "Build fails on macOS",
     "Prometheus history from 2013: the build broke on macOS, and the report is a pasted build log."),
    ("realworld", "RW-go-05", "Fails on big-endian and riscv64",
     "Go started failing on big-endian platforms and riscv64. 27 commits to sort through."),
    ("realworld", "RW-go-04", "Toolchain bootstrap panics",
     "Go's toolchain bootstrap broke. The report carries the compiler's own panic message."),
    ("realworld", "RW-go-03", "CockroachDB benchmark fails",
     "A CockroachDB benchmark regressed on Go. The report's link to the suspect change was lost."),
]


def needs_demo_repo() -> bool:
    """Only synthetic, fresh and negative scenarios run against the planted monorepo."""
    return any(entry[0] != "realworld" for entry in CURATED)


def _load_suite(suite: str) -> dict[str, dict]:
    path = SUITE_FILES[suite]
    if not path.exists():
        return {}
    return {r["incident_id"]: r for r in json.loads(path.read_text(encoding="utf-8"))}


def _fresh_bug(repo: Path | None) -> tuple[dict, str | None]:
    sys.path.insert(0, str(EVAL))
    try:
        import demo_new_bug  # noqa: PLC0415  (lives in eval/, imported on demand)
    finally:
        sys.path.remove(str(EVAL))
    sha = None
    if repo is not None and (repo / ".git").exists():
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%H", "--fixed-strings",
             f"--grep={FRESH_BUG_SUBJECT}"], capture_output=True, text=True,
        ).stdout.strip()
        sha = out or None
    return dict(demo_new_bug.ALERT_PAYLOAD), sha


def _short_title(title: str, limit: int = 64) -> str:
    title = " ".join(title.split())
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


@lru_cache(maxsize=4)
def load(demo_repo: Path | None = None) -> tuple[Scenario, ...]:
    suites = {name: _load_suite(name) for name in SUITE_FILES}
    out: list[Scenario] = []
    for suite, incident_id, label, hook in CURATED:
        if suite == "fresh":
            alert, sha = _fresh_bug(demo_repo)
            out.append(Scenario(
                id=incident_id, title=label or _short_title(alert["title"]), hook=hook, suite=suite,
                source="planted for the demo; in no benchmark", repo="demo", alert=alert,
                answer_sha=sha, answer_subject=FRESH_BUG_SUBJECT,
            ))
            continue
        record = suites[suite].get(incident_id)
        if record is None:
            continue
        source = {
            "synthetic": f"synthetic benchmark, {incident_id}",
            "negative": f"negative control, {incident_id}: {record.get('cause', '')}".strip(),
            "realworld": f"reverted upstream in {record.get('repo')}",
        }[suite]
        out.append(Scenario(
            id=incident_id, title=label or _short_title(record["alert"]["title"]), hook=hook,
            suite=suite, source=source,
            repo=record.get("repo", "demo") if suite == "realworld" else "demo",
            alert=record["alert"], answer_sha=record.get("ground_truth_sha"),
            answer_subject=record.get("ground_truth_subject", ""),
        ))
    return tuple(out)


def get(scenario_id: str, demo_repo: Path | None = None) -> Scenario | None:
    return next((s for s in load(demo_repo) if s.id == scenario_id), None)
