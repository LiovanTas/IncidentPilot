"""Commit correlation and runbook retrieval, against the built fixture repo."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from incidentpilot.gitctx import (
    GitRepo,
    _risk_signal,
    collect_candidates,
    rank_candidates,
    services_for_files,
)
from incidentpilot.ingest import alert_keywords, normalize
from incidentpilot.models import Alert, CommitCandidate
from incidentpilot.rag import HashedEmbedder, RunbookIndex, chunk_markdown, cosine

ROOT = Path(__file__).resolve().parents[1]
REPO_PATH = ROOT / "eval" / "fixtures" / "repo"
INCIDENTS = ROOT / "eval" / "incidents.json"
TOPOLOGY = json.loads((ROOT / "data" / "topology.json").read_text(encoding="utf-8"))

needs_fixture = pytest.mark.skipif(
    not (REPO_PATH / ".git").exists(),
    reason="fixture repo not built (run eval/build_fixture_repo.py)",
)


@pytest.fixture(scope="module")
def repo() -> GitRepo:
    return GitRepo(REPO_PATH)


@pytest.fixture(scope="module")
def incidents() -> list[dict]:
    return json.loads(INCIDENTS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def index(tmp_path_factory) -> RunbookIndex:
    idx = RunbookIndex(tmp_path_factory.mktemp("rag") / "runbooks.db", embedder=HashedEmbedder())
    idx.build(ROOT / "runbooks")
    return idx


# ------------------------------------------------------------------------ gitctx


def test_services_for_files_maps_paths_to_owners():
    owners = services_for_files(
        ["services/checkout-api/config.py", "libs/http_client.py"], TOPOLOGY
    )
    assert "checkout-api" in owners


def test_commits_after_onset_score_zero():
    alert = Alert(
        fingerprint="t", source="generic", service="checkout-api", severity="sev1",
        title="errors", started_at=datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
    )
    future = CommitCandidate(
        sha="f" * 40, author="a", authored_at=alert.started_at + timedelta(hours=3),
        subject="chore: something risky with a migration",
        files=["services/checkout-api/config.py"], insertions=10, deletions=2,
    )
    ranked = rank_candidates(alert, [future], TOPOLOGY, ["errors"])
    assert ranked[0].score == 0.0
    assert "after the alert fired" in " ".join(ranked[0].rationale)


def test_risk_signal_flags_migrations_above_plain_refactors():
    migration = CommitCandidate(sha="a" * 40, author="a", authored_at=datetime.now(timezone.utc),
                                subject="db: drop legacy column", files=["db/migrations/x.sql"])
    refactor = CommitCandidate(sha="b" * 40, author="a", authored_at=datetime.now(timezone.utc),
                               subject="refactor: rename a helper", files=["services/x/util.py"])
    assert _risk_signal(migration)[0] > _risk_signal(refactor)[0]


@needs_fixture
def test_window_excludes_commits_outside_lookback(repo, incidents):
    alert = normalize(incidents[10]["alert"])[0]
    narrow = repo.commits_in_window(alert, lookback_hours=2)
    wide = repo.commits_in_window(alert, lookback_hours=72)
    assert len(narrow) < len(wide)
    for commit in wide:
        assert commit.authored_at <= alert.started_at + timedelta(minutes=31)


@needs_fixture
def test_ground_truth_is_always_retrieved(repo, incidents):
    """The retrieval stage must never lose the true cause -- it is the agent's ceiling."""
    misses = []
    for record in incidents:
        alert = normalize(record["alert"])[0]
        candidates = collect_candidates(
            repo, alert, TOPOLOGY, alert_keywords(alert), lookback_hours=72, limit=12
        )
        if record["ground_truth_sha"] not in [c.sha for c in candidates]:
            misses.append(record["incident_id"])
    assert misses == [], f"ground truth missing from candidates for {misses}"


@needs_fixture
def test_candidates_are_sorted_by_descending_score(repo, incidents):
    alert = normalize(incidents[0]["alert"])[0]
    candidates = collect_candidates(
        repo, alert, TOPOLOGY, alert_keywords(alert), lookback_hours=72, limit=12
    )
    scores = [c.score for c in candidates]
    assert scores == sorted(scores, reverse=True)
    assert all(c.score > 0 for c in candidates)


@needs_fixture
def test_show_truncates_long_diffs(repo, incidents):
    sha = incidents[0]["ground_truth_sha"]
    diff = repo.show(sha, max_chars=200)
    assert len(diff) < 400
    assert "truncated" in diff


# --------------------------------------------------------------------------- rag


def test_chunking_splits_on_headings():
    chunks = chunk_markdown(ROOT / "runbooks" / "db-connection-pool-exhaustion.md")
    headings = {c["heading"] for c in chunks}
    assert "Symptoms" in headings
    assert "Mitigation" in headings
    assert all(c["runbook_id"] == "db-connection-pool-exhaustion" for c in chunks)


def test_hashed_embedder_is_deterministic_and_normalized():
    embedder = HashedEmbedder()
    a = embedder.embed_one("connection pool exhaustion on checkout")
    b = embedder.embed_one("connection pool exhaustion on checkout")
    assert a == b
    assert cosine(a, b) == pytest.approx(1.0, abs=1e-6)
    c = embedder.embed_one("duplicate charges in the ledger reconciliation")
    assert cosine(a, c) < 0.9


def test_retrieval_finds_the_right_runbook(index):
    cases = {
        "QueuePool limit reached, connections timing out on checkout":
            "db-connection-pool-exhaustion",
        "customers charged twice, ledger shows two payments for one order":
            "payment-duplicate-charges",
        "consumer lag growing, messages arriving late":
            "queue-lag-and-worker-saturation",
        "pods OOMKilled and restarting in a loop":
            "oom-and-restart-loops",
    }
    for query, expected in cases.items():
        hits = index.search(query, top_k=3)
        assert expected in [h.runbook_id for h in hits], f"{query!r} -> {[h.runbook_id for h in hits]}"


def test_search_returns_distinct_runbooks_before_repeating(index):
    hits = index.search("cache hit ratio collapsed after deploy", top_k=4)
    ids = [h.runbook_id for h in hits]
    assert len(set(ids[:3])) == 3, f"expected breadth before depth, got {ids}"


def test_empty_query_does_not_crash(index):
    assert isinstance(index.search("", top_k=3), list)
