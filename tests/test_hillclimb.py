"""The hill-climb's decision rule, split and variant knobs -- all without an API key."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import hillclimb  # noqa: E402
import variants  # noqa: E402
from incidentpilot.agent import DiagnosisAgent, READ_FILE_TOOL, TOOLS, tools_for  # noqa: E402
from incidentpilot.agent.prompts import build_incident_prompt  # noqa: E402
from incidentpilot.config import load_config  # noqa: E402
from incidentpilot.models import Alert, CommitCandidate, Diagnosis  # noqa: E402

needs_fixture = pytest.mark.skipif(
    not (ROOT / "eval" / "fixtures" / "repo" / ".git").exists(),
    reason="fixture repo not built",
)


def rep(dev_top1=11, test_top1=7, wu=0, fc=0, cost=1.8, secs=900.0, dev_n=12, test_n=8):
    """One repeat's metrics, split evenly-ish across dev and test."""
    frac = dev_n / (dev_n + test_n)
    return {
        "dev": {"n": dev_n, "top1": dev_top1, "unflagged_wrong": wu, "flagged_correct": fc,
                "cost": cost * frac, "seconds_total": secs * frac},
        "test": {"n": test_n, "top1": test_top1, "unflagged_wrong": 0, "flagged_correct": 0,
                 "cost": cost * (1 - frac), "seconds_total": secs * (1 - frac)},
    }


BASE = [rep(), rep(dev_top1=10)]


# ---------------------------------------------------------------- config (bug fix)


def test_config_reads_the_environment_at_construction_not_import(monkeypatch):
    """Defaults used to be evaluated once at import, so changing the environment later did
    nothing -- every variant in one process would silently have run the same config."""
    monkeypatch.setenv("INCIDENTPILOT_EFFORT", "low")
    assert load_config().effort == "low"
    monkeypatch.setenv("INCIDENTPILOT_EFFORT", "max")
    assert load_config().effort == "max"


def test_overrides_beat_the_environment(monkeypatch):
    monkeypatch.setenv("INCIDENTPILOT_EFFORT", "low")
    assert load_config(effort="medium").effort == "medium"


def test_every_variant_override_is_a_real_config_field():
    for variant in variants.VARIANTS.values():
        load_config(**variant.overrides)  # raises TypeError on a typo'd field


# ------------------------------------------------------------------ decision rule


def test_safety_ratchet_rejects_more_wrong_and_unflagged_even_if_cheaper():
    cheaper_but_riskier = [rep(wu=1, cost=0.9), rep(wu=1, cost=0.9)]
    decision = hillclimb.decide(BASE, cheaper_but_riskier, "cost")
    assert not decision.keep
    assert not decision.checks[0][0]


def test_cost_win_is_kept_when_accuracy_holds():
    decision = hillclimb.decide(BASE, [rep(cost=1.2), rep(cost=1.25)], "cost")
    assert decision.keep, decision.checks


def test_cost_win_smaller_than_ten_percent_is_rejected():
    decision = hillclimb.decide(BASE, [rep(cost=1.72), rep(cost=1.74)], "cost")
    assert not decision.keep


def test_cost_win_that_costs_test_accuracy_is_rejected():
    decision = hillclimb.decide(BASE, [rep(cost=1.0, test_top1=4), rep(cost=1.0, test_top1=4)], "cost")
    assert not decision.keep


def test_latency_win_is_kept():
    decision = hillclimb.decide(BASE, [rep(secs=500), rep(secs=520)], "latency")
    assert decision.keep, decision.checks


def test_accuracy_gain_inside_noise_is_rejected():
    """Baseline wobbles 10-11 on dev; landing on 11.5 is not evidence of anything."""
    decision = hillclimb.decide(BASE, [rep(dev_top1=11), rep(dev_top1=12)], "accuracy")
    assert not decision.keep


def test_accuracy_gain_beyond_noise_is_kept():
    """Baseline mean 10.5 with a 1-incident band, so the bar is 11.5; 12.0 clears it."""
    decision = hillclimb.decide(BASE, [rep(dev_top1=12), rep(dev_top1=12)], "accuracy")
    assert decision.keep, decision.checks


def test_calibration_needs_fewer_unflagged_errors_at_bounded_escalation_cost():
    base = [rep(wu=1), rep(wu=1)]
    good = [rep(wu=0, fc=1), rep(wu=0, fc=1)]
    too_cautious = [rep(wu=0, fc=4), rep(wu=0, fc=5)]
    assert hillclimb.decide(base, good, "calibration").keep
    assert not hillclimb.decide(base, too_cautious, "calibration").keep


def test_single_repeat_baseline_warns_that_noise_is_unmeasured():
    decision = hillclimb.decide([rep()], [rep(cost=1.0)], "cost")
    assert any("noise" in w for w in decision.warnings)


# -------------------------------------------------------------------------- split


@needs_fixture
def test_split_is_deterministic_disjoint_and_complete():
    import json
    records = json.loads((ROOT / "eval" / "incidents.json").read_text(encoding="utf-8"))
    a, b = hillclimb.make_split(records), hillclimb.make_split(records)
    assert a["dev"] == b["dev"] and a["test"] == b["test"]
    assert not set(a["dev"]) & set(a["test"])
    assert set(a["dev"]) | set(a["test"]) == {r["incident_id"] for r in records}
    assert 6 <= len(a["test"]) <= 10


@needs_fixture
def test_split_puts_hard_cross_service_cases_in_both_halves():
    import json
    records = json.loads((ROOT / "eval" / "incidents.json").read_text(encoding="utf-8"))
    split = hillclimb.make_split(records)
    cross = {r["incident_id"] for r in records if hillclimb._cause_is_cross_service(r)}
    assert cross & set(split["dev"]), "dev has no hard cases"
    assert cross & set(split["test"]), "test has no hard cases"


# ------------------------------------------------------------------ variant knobs


def test_read_file_tool_is_opt_in_and_keeps_submit_last():
    assert tools_for(False) is TOOLS
    enabled = tools_for(True)
    assert READ_FILE_TOOL in enabled
    assert enabled[-1]["name"] == "submit_diagnosis"
    assert len(enabled) == len(TOOLS) + 1


def _alert():
    return Alert(fingerprint="t", source="generic", service="checkout-api", severity="sev1",
                 title="x", started_at=datetime(2026, 8, 10, tzinfo=timezone.utc))


def test_prefetched_diffs_are_inlined_in_the_first_prompt():
    candidate = CommitCandidate(sha="a" * 40, author="a", authored_at=_alert().started_at, subject="s")
    prompt = build_incident_prompt(_alert(), [candidate], [], {}, {},
                                   prefetched={"a" * 40: "-DB_POOL_SIZE = 50\n+DB_POOL_SIZE = 5"})
    assert "+DB_POOL_SIZE = 5" in prompt
    assert "do not fetch them again" in prompt


def test_prompt_without_prefetch_is_unchanged():
    candidate = CommitCandidate(sha="a" * 40, author="a", authored_at=_alert().started_at, subject="s")
    assert "already fetched" not in build_incident_prompt(_alert(), [candidate], [], {}, {})


@pytest.mark.parametrize("confidence,flag_in,expected", [
    (0.60, False, True),    # below threshold -> escalated
    (0.90, False, False),   # above -> left alone
    (0.60, True, True),     # already flagged -> stays flagged
    (0.90, True, True),     # policy never lowers a flag the model raised
])
def test_escalation_policy(confidence, flag_in, expected):
    agent = DiagnosisAgent(model="claude-opus-5", client=SimpleNamespace(), escalate_below=0.75)
    verdict = Diagnosis(root_cause="r", offending_sha="a", confidence=confidence,
                        reasoning="because", needs_human=flag_in)
    assert agent._apply_escalation_policy(verdict).needs_human is expected


def test_escalation_policy_off_by_default():
    agent = DiagnosisAgent(model="claude-opus-5", client=SimpleNamespace())
    verdict = Diagnosis(root_cause="r", offending_sha="a", confidence=0.1, reasoning="x")
    assert agent._apply_escalation_policy(verdict).needs_human is False


def test_accuracy_variant_at_ceiling_is_reported_as_unmeasurable_not_refuted():
    """With dev at 12/12 there is no room to show a gain; the rule must say so."""
    at_ceiling = [rep(dev_top1=12), rep(dev_top1=12)]
    decision = hillclimb.decide(at_ceiling, at_ceiling, "accuracy")
    assert not decision.keep
    assert any("unmeasurable" in w for w in decision.warnings)
