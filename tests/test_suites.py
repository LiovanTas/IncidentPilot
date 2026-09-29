"""Negative controls, the real-world builder's leak scrubbing, and per-suite scoring."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(ROOT / "eval" / "realworld"))

import build as realworld  # noqa: E402
import make_negatives  # noqa: E402
import run_eval  # noqa: E402
from incidentpilot.models import CommitCandidate  # noqa: E402

TOPOLOGY = json.loads((ROOT / "data" / "topology.json").read_text(encoding="utf-8"))


class FakePilot:
    """Just enough of IncidentPilot for score_one."""

    def __init__(self, blamed: str | None, needs_human: bool, candidates=()):
        self.topology = TOPOLOGY
        self._result = SimpleNamespace(
            candidates=list(candidates), runbooks=[], tool_calls=[], usage={},
            diagnosis=SimpleNamespace(offending_sha=blamed, confidence=0.7,
                                      needs_human=needs_human, source="agent",
                                      runbook_ids=[], root_cause="r"),
        )

    def handle(self, alert, use_agent=True):
        return self._result


NEGATIVE = {
    "incident_id": "NEG-X", "ground_truth_sha": None, "cause": "Provider outage.",
    "ground_truth_subject": "No commit -- Provider outage.",
    "alert": {"source": "generic", "service": "payments-worker", "severity": "sev1",
              "title": "t", "started_at": "2026-08-12T18:00:00Z"},
}


# --------------------------------------------------------------- negative controls


def test_naming_no_commit_is_correct_on_a_negative():
    row = run_eval.score_one(NEGATIVE, FakePilot(blamed=None, needs_human=True), use_agent=True)
    assert row["negative"] and row["correct"]
    assert row["truth_sha"] is None and row["truth_rank"] is None


def test_blaming_any_commit_is_wrong_on_a_negative():
    row = run_eval.score_one(NEGATIVE, FakePilot(blamed="a" * 40, needs_human=False), use_agent=True)
    assert not row["correct"]


def test_negative_aggregate_separates_flagged_from_confident_blame():
    rows = [
        run_eval.score_one(NEGATIVE, FakePilot(None, True), True),
        run_eval.score_one(NEGATIVE, FakePilot("a" * 40, True), True),
        run_eval.score_one(NEGATIVE, FakePilot("b" * 40, False), True),
    ]
    agg = run_eval.aggregate(rows, "claude-sonnet-5")
    assert agg["top1"] == 1 and agg["negatives"] == 3
    assert agg["negatives_named_nothing"] == 1
    assert agg["negatives_blamed_but_flagged"] == 1
    assert agg["negatives_blamed_confidently"] == 1
    # Retrieval has no meaning without a guilty commit to retrieve.
    assert agg["positives"] == 0 and agg["recall"] is None and agg["mrr"] is None
    # A confident wrong blame is exactly what calibration's danger count exists for.
    assert run_eval.calibration(rows)["unflagged_and_wrong"] == 1


def test_every_negative_label_is_clean():
    """Nothing the alerting service could depend on changed inside its window."""
    records = json.loads((ROOT / "eval" / "negatives.json").read_text(encoding="utf-8"))
    commits = make_negatives.all_commits()
    assert len(records) == len(make_negatives.NEGATIVES)
    for record in records:
        onset = datetime.fromisoformat(record["alert"]["started_at"].replace("Z", "+00:00"))
        window = make_negatives._window(commits, onset)
        reach = make_negatives._reach(record["alert"]["service"])
        assert len(window) >= make_negatives.MIN_TEMPTATIONS
        touching = [c["message"].splitlines()[0] for c in window
                    for p in make_negatives._paths(c) for r in reach if p.startswith(r)]
        assert not touching, f"{record['incident_id']} window has a plausible cause: {touching}"


def test_negative_descriptions_carry_no_clock_times_that_could_contradict_onset():
    import re
    for neg in make_negatives.NEGATIVES:
        assert not re.search(r"\b\d{1,2}:\d{2}\b", neg["description"]), neg["id"]


# ------------------------------------------------------------ real-world scrubbing


def test_clean_reason_strips_everything_that_identifies_the_change():
    body = (
        'Revert "pkg/tsdb: speed up head compaction (#15432)"\n\n'
        "This reverts commit 3f9a1c2b7e4d5a6f8b9c0d1e2f3a4b5c6d7e8f90.\n\n"
        "Compaction started panicking with a nil pointer in head.go on arm64,\n"
        "see https://github.com/prometheus/prometheus/issues/15500 and @someone's report.\n"
        "Broke in 9f8e7d6c after the change in #15432.\n\n"
        "Signed-off-by: A Person <a@example.com>\n"
    )
    reason = realworld.clean_reason(body)
    assert "panicking with a nil pointer" in reason
    for leak in ("Revert", "3f9a1c2b", "9f8e7d6c", "15432", "15500", "@someone", "https://",
                 "Signed-off-by", "speed up head compaction"):
        assert leak not in reason, leak


def test_leak_check_catches_a_reason_that_restates_the_subject():
    assert realworld.leaks_answer("the change 'speed up head compaction' broke arm64",
                                  "tsdb: speed up head compaction (#15432)") is False
    assert realworld.leaks_answer("reverting tsdb speed up head compaction, it broke arm64",
                                  "tsdb: speed up head compaction (#15432)") is True
    # Two-word subjects are too generic to count as a leak by substring alone.
    assert realworld.leaks_answer("fix typo in the docs", "fix typo") is False


# ----------------------------------------------------------------- suite plumbing


def test_real_world_incidents_are_never_called_cross_service():
    """There is no service map for a real repo, so 'outside the alerting service' would
    be true of every incident and mean nothing."""
    truth = CommitCandidate(sha="c" * 40, author="a",
                            authored_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                            subject="s", files=["pkg/x.go"])
    record = {**NEGATIVE, "incident_id": "RW-1", "ground_truth_sha": "c" * 40,
              "ground_truth_subject": "s", "repo": "prometheus",
              "alert": {**NEGATIVE["alert"], "service": "prometheus"}}
    row = run_eval.score_one(record, FakePilot("c" * 40, False, [truth]), True)
    assert row["correct"] and not row["cross_service"]


def test_every_suite_has_a_builder_and_an_intro():
    assert set(run_eval.SUITES) == set(run_eval.SUITE_BUILDERS) == set(run_eval.SUITE_INTRO)


# Regressions found by reading the first real-world build's alerts.

def test_moby_dco_signoffs_are_not_a_reason():
    body = ("Revert \"foo\"\n\nThis reverts commit abcdef1.\n\n"
            "Docker-DCO-1.1-Signed-off-by: Michael Crosby <m@example.com> (github: crosbymichael)")
    assert realworld.clean_reason(body) == ""


def test_bulleted_revert_titles_are_removed():
    body = ('Revert "x" (#99)\n\n* Revert "Update dependency @types/core to v1.4.6"\n\n'
            "This reverts commit abcdef1.\n\n* The new type definitions break the e2e build.")
    reason = realworld.clean_reason(body)
    assert "Update dependency" not in reason
    assert "break the e2e build" in reason


def test_lines_describing_the_revert_itself_are_removed():
    body = ("Alerting: Revert adding typesVersions to /alerting for backward compat\n\n"
            "Consumers on the old bundler cannot resolve the subpath exports any more.")
    reason = realworld.clean_reason(body)
    assert "typesVersions" not in reason
    assert "cannot resolve the subpath exports" in reason


def test_leak_check_scrubs_the_subject_the_same_way_as_the_alert():
    """Stripping '@types' from the alert but not the subject hid an exact restatement."""
    assert realworld.leaks_answer("Update dependency /core to v1.4.6 broke the build",
                                  "Update dependency @types/core to v1.4.6")


def test_gerrit_reason_for_revert_is_kept_despite_mentioning_revert():
    body = ('Revert "cmd/go: foo"\n\nThis reverts CL 512345.\n\n'
            "Reason for revert: breaks the linux-386 builder\n\n"
            "Change-Id: I1234\nReviewed-on: https://go-review.googlesource.com/c/go/+/5\n")
    assert realworld.clean_reason(body) == "breaks the linux-386 builder"


def test_commit_subject_lines_are_dropped_even_when_they_mention_a_failure():
    """'...due to bugs in ~1.4.5' went through once, as an alert naming its own answer."""
    body = ("* chore(renovate): ignore swc/core for time being due to bugs in versions ~1.4.5\n"
            "* fix wording of generated code verification error\n"
            "* Fix ValueMappingEditor test TS issue")
    assert realworld.clean_reason(body) == ""


def test_gerrit_change_numbers_are_scrubbed():
    """'Likely due to CL 564197' matches the guilty commit's own Reviewed-on line."""
    reason = realworld.clean_reason(
        "Reason for revert: cockroachdb benchmark failing. Likely due to CL 564197.")
    assert "564197" not in reason and "cockroachdb benchmark failing" in reason


def test_alerts_naming_the_author_are_rejected():
    assert realworld.names_author("Keith's change broke arm64", "Keith Randall", "go")
    assert not realworld.names_author("broke arm64", "Keith Randall", "go")
    # A bot's name is not identifying, and neither is the project's own name.
    assert not realworld.names_author("grafana bot builds fail", "Grafana Bot", "grafana")
