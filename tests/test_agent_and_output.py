"""Agent loop, Slack brief and resolution report, driven by a fake Anthropic client."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from incidentpilot.agent import TOOLS, DiagnosisAgent, ToolContext, dispatch, heuristic_diagnosis
from incidentpilot.agent.prompts import SYSTEM_PROMPT, build_incident_prompt
from incidentpilot.impact import estimate_impact
from incidentpilot.models import (
    Alert,
    CommitCandidate,
    Diagnosis,
    ImpactEstimate,
    IncidentResult,
    RunbookChunk,
    make_incident_id,
)
from incidentpilot.report import render_markdown
from incidentpilot.slack import build_blocks, build_payload, deliver

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = json.loads((ROOT / "data" / "topology.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------- fake client


class _Block(SimpleNamespace):
    pass


def tool_use(name: str, payload: dict, block_id: str = "tu_1") -> _Block:
    return _Block(type="tool_use", name=name, input=payload, id=block_id)


def text_block(text: str) -> _Block:
    return _Block(type="text", text=text)


class _Stream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _Messages:
    def __init__(self, script: list, calls: list):
        self._script = script
        self._calls = calls

    def stream(self, **kwargs):
        # The loop mutates `messages` in place across turns, so snapshot it per call.
        self._calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if not self._script:
            raise AssertionError("agent made more requests than the script provides")
        return _Stream(self._script.pop(0))


class _BetaMessages:
    """Mimics an SDK that does not know the server-side fallbacks beta."""

    def stream(self, **kwargs):
        raise TypeError("stream() got an unexpected keyword argument 'fallbacks'")


class FakeClient:
    def __init__(self, script: list):
        self.calls: list[dict] = []
        self.messages = _Messages(script, self.calls)
        self.beta = SimpleNamespace(messages=_BetaMessages())


def message(content: list, stop_reason: str = "tool_use", **usage) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(input_tokens=usage.get("input_tokens", 1200),
                              output_tokens=usage.get("output_tokens", 300),
                              cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
                              cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0)),
    )


# ---------------------------------------------------------------------- fixtures


@pytest.fixture
def alert() -> Alert:
    return Alert(
        fingerprint="INC-TEST", source="generic", service="checkout-api", severity="sev1",
        title="checkout-api 5xx rate above 8%",
        description="QueuePool limit reached, database CPU flat",
        started_at=datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
        metrics={"error_rate_before": 0.002, "error_rate_after": 0.084, "duration_minutes": 22},
    )


@pytest.fixture
def candidates() -> list[CommitCandidate]:
    return [
        CommitCandidate(
            sha="a" * 40, author="Decoy Dev",
            authored_at=datetime(2026, 8, 10, 8, 24, tzinfo=timezone.utc),
            subject="fix: retry payment authorization on transient errors",
            files=["services/checkout-api/handlers.py"], insertions=12, deletions=1,
            services=["checkout-api"], score=0.71, rationale=["landed 36m before onset"],
        ),
        CommitCandidate(
            sha="b" * 40, author="Real Cause",
            authored_at=datetime(2026, 8, 10, 7, 30, tzinfo=timezone.utc),
            subject="perf: right-size checkout db pool after connection audit",
            files=["services/checkout-api/config.py"], insertions=1, deletions=1,
            services=["checkout-api"], score=0.64, rationale=["landed 1.5h before onset"],
        ),
    ]


@pytest.fixture
def runbooks() -> list[RunbookChunk]:
    return [RunbookChunk(chunk_id="db#1", runbook_id="db-connection-pool-exhaustion",
                         title="Pool exhaustion", heading="Mitigation",
                         text="Restore the previous pool size and redeploy.", score=0.03)]


def make_ctx(alert, candidates) -> ToolContext:
    return ToolContext(alert=alert, repo=None, index=None, topology=TOPOLOGY,
                       candidates=candidates, lookback_hours=72, keywords=["pool", "checkout"])


VERDICT = {
    "root_cause": "Connection pool lowered from 50 to 5, starving checkout of connections.",
    "offending_sha": "b" * 40,
    "confidence": 0.91,
    "reasoning": "The diff lowers DB_POOL_SIZE 50 -> 5. Database CPU is flat, which rules out "
                 "a slow query and points at starvation on the client side.",
    "evidence": ["DB_POOL_SIZE 50 -> 5 in services/checkout-api/config.py"],
    "ruled_out": ["aaaaaaaaaa: adds a retry flag, cannot produce pool timeouts"],
    "runbook_ids": ["db-connection-pool-exhaustion"],
    "remediation": ["Revert and redeploy", "Confirm error rate recovers"],
    "rollback_command": "git revert --no-edit bbbbbbbbbb",
    "needs_human": False,
}


# ------------------------------------------------------------------- tool schema


def test_tool_schemas_are_well_formed():
    names = [t["name"] for t in TOOLS]
    assert "submit_diagnosis" in names
    assert len(names) == len(set(names))
    for tool in TOOLS:
        assert tool["description"].strip()
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        for required in schema.get("required", []):
            assert required in schema["properties"], f"{tool['name']}: {required} not in properties"

    submit = next(t for t in TOOLS if t["name"] == "submit_diagnosis")
    assert submit["strict"] is True
    assert submit["input_schema"]["additionalProperties"] is False
    assert set(submit["input_schema"]["required"]) == set(submit["input_schema"]["properties"])


def test_system_prompt_has_no_volatile_content():
    """It sits in front of the cache breakpoint, so it must be byte-stable."""
    assert "2026" not in SYSTEM_PROMPT
    assert "{" not in SYSTEM_PROMPT


def test_incident_prompt_includes_candidates_and_impact(alert, candidates, runbooks):
    impact = estimate_impact(alert, TOPOLOGY).to_dict()
    prompt = build_incident_prompt(alert, candidates, runbooks, impact,
                                   {"depends_on": ["cart-service"], "called_by": ["web-frontend"],
                                    "owner": "#team-checkout", "tier": 1})
    assert "aaaaaaaaaa" in prompt
    assert "bbbbbbbbbb" in prompt
    assert "checkout-api 5xx rate above 8%" in prompt
    assert "db-connection-pool-exhaustion" in prompt
    assert "users affected" in prompt


# -------------------------------------------------------------------- tool calls


def test_dispatch_topology_reports_callers(alert, candidates):
    out = dispatch("get_service_topology", {"service": "payments-worker"}, make_ctx(alert, candidates))
    payload = json.loads(out)
    assert "checkout-api" in payload["called_by"]
    assert payload["owner"] == "#team-payments"


def test_dispatch_unknown_service_lists_known_ones(alert, candidates):
    out = dispatch("get_service_topology", {"service": "nope"}, make_ctx(alert, candidates))
    assert "Unknown service" in out and "checkout-api" in out


def test_dispatch_recomputes_impact(alert, candidates):
    out = dispatch("estimate_user_impact",
                   {"service": "checkout-api", "error_rate_after": 0.5, "duration_minutes": 90},
                   make_ctx(alert, candidates))
    payload = json.loads(out)
    assert payload["duration_minutes"] == 90
    assert payload["affected_users_point"] > 0


def test_dispatch_never_raises(alert, candidates):
    out = dispatch("get_commit_diff", {"sha": "deadbeef"}, make_ctx(alert, candidates))
    assert out.startswith("tool error") or out.startswith("git error")


def test_dispatch_unknown_tool_is_reported(alert, candidates):
    assert "Unknown tool" in dispatch("nope", {}, make_ctx(alert, candidates))


# -------------------------------------------------------------------- agent loop


def test_agent_returns_verdict_and_falls_back_off_the_beta(alert, candidates, runbooks):
    client = FakeClient([
        message([text_block("Checking the topology first."),
                 tool_use("get_service_topology", {"service": "checkout-api"})]),
        message([tool_use("submit_diagnosis", VERDICT, "tu_2")], stop_reason="tool_use"),
    ])
    agent = DiagnosisAgent(model="claude-opus-5", client=client)
    diagnosis, transcript, usage = agent.run(
        make_ctx(alert, candidates), runbooks, estimate_impact(alert, TOPOLOGY).to_dict(),
        {"depends_on": [], "called_by": [], "owner": "#team-checkout", "tier": 1},
    )

    assert diagnosis.offending_sha == "b" * 40
    assert diagnosis.confidence == pytest.approx(0.91)
    assert diagnosis.source == "agent"
    assert [c.name for c in transcript] == ["get_service_topology", "submit_diagnosis"]
    assert usage["input_tokens"] == 2400

    # The fake beta namespace rejects `fallbacks`, so the loop must have retried without it.
    assert len(client.calls) == 2
    first = client.calls[0]
    assert first["model"] == "claude-opus-5"
    assert first["thinking"] == {"type": "adaptive"}
    assert first["output_config"] == {"effort": "high"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "fallbacks" not in first


def test_agent_nudges_when_no_tool_is_called(alert, candidates, runbooks):
    client = FakeClient([
        message([text_block("I think it is probably the pool change.")], stop_reason="end_turn"),
        message([tool_use("submit_diagnosis", VERDICT, "tu_2")]),
    ])
    agent = DiagnosisAgent(model="claude-opus-5", client=client)
    diagnosis, _, _ = agent.run(
        make_ctx(alert, candidates), runbooks, estimate_impact(alert, TOPOLOGY).to_dict(), {},
    )
    assert diagnosis.offending_sha == "b" * 40
    nudge = client.calls[1]["messages"][-1]
    assert nudge["role"] == "user"
    assert "submit_diagnosis" in nudge["content"]


def test_agent_falls_back_to_heuristic_when_turns_run_out(alert, candidates, runbooks):
    script = [message([tool_use("get_service_topology", {"service": "checkout-api"}, f"tu_{i}")])
              for i in range(3)]
    agent = DiagnosisAgent(model="claude-opus-5", max_turns=3, client=FakeClient(script))
    diagnosis, transcript, _ = agent.run(
        make_ctx(alert, candidates), runbooks, estimate_impact(alert, TOPOLOGY).to_dict(), {},
    )
    assert diagnosis.source == "heuristic"
    assert diagnosis.needs_human is True
    assert "did not converge" in diagnosis.reasoning
    assert len(transcript) == 3


def test_agent_handles_a_refusal(alert, candidates, runbooks):
    refused = message([], stop_reason="refusal")
    refused.stop_details = SimpleNamespace(type="refusal", category="cyber",
                                           explanation="declined by classifier")
    agent = DiagnosisAgent(model="claude-opus-5", client=FakeClient([refused]))
    diagnosis, _, _ = agent.run(
        make_ctx(alert, candidates), runbooks, estimate_impact(alert, TOPOLOGY).to_dict(), {},
    )
    assert diagnosis.needs_human is True
    assert diagnosis.confidence == 0.0
    assert "declined" in diagnosis.reasoning


# --------------------------------------------------------------------- heuristic


def test_heuristic_picks_the_top_candidate(alert, candidates, runbooks):
    diagnosis = heuristic_diagnosis(alert, candidates, runbooks)
    assert diagnosis.offending_sha == "a" * 40
    assert diagnosis.source == "heuristic"
    assert diagnosis.rollback_command.startswith("git revert")


def test_heuristic_with_no_candidates_asks_for_a_human(alert, runbooks):
    diagnosis = heuristic_diagnosis(alert, [], runbooks)
    assert diagnosis.offending_sha is None
    assert diagnosis.needs_human is True


def test_heuristic_flags_a_close_call(alert, runbooks):
    tied = [
        CommitCandidate(sha="c" * 40, author="a", authored_at=datetime.now(timezone.utc),
                        subject="one", score=0.50),
        CommitCandidate(sha="d" * 40, author="a", authored_at=datetime.now(timezone.utc),
                        subject="two", score=0.49),
    ]
    assert heuristic_diagnosis(alert, tied, runbooks).needs_human is True


# ----------------------------------------------------------------- slack/report


@pytest.fixture
def result(alert, candidates, runbooks) -> IncidentResult:
    return IncidentResult(
        incident_id=make_incident_id(alert), alert=alert, candidates=candidates,
        runbooks=runbooks, diagnosis=Diagnosis(**{**VERDICT, "source": "agent"}),
        impact=estimate_impact(alert, TOPOLOGY),
        finished_at=datetime(2026, 8, 10, 9, 2, tzinfo=timezone.utc),
        usage={"input_tokens": 2400, "output_tokens": 600},
    )


def test_slack_blocks_are_valid_block_kit(result):
    blocks = build_blocks(result, repo_url="https://github.com/acme/checkout-platform")
    assert blocks[0]["type"] == "header"
    assert len(blocks[0]["text"]["text"]) <= 150
    for block in blocks:
        assert block["type"] in {"header", "section", "divider", "context", "actions"}
        if block["type"] == "section" and "fields" in block:
            assert len(block["fields"]) <= 10
    flat = json.dumps(blocks)
    assert "bbbbbbbbbb" in flat
    assert "db-connection-pool-exhaustion" in flat


def test_slack_payload_has_a_notification_fallback(result):
    payload = build_payload(result, "#incidents")
    assert payload["channel"] == "#incidents"
    assert result.incident_id in payload["text"]


def test_slack_delivery_is_dry_run_by_default(result, tmp_path):
    outcome = deliver(result, tmp_path, "#incidents", post=False)
    assert outcome["posted"] is False
    assert "disabled" in outcome["reason"]
    written = json.loads(Path(outcome["payload_path"]).read_text(encoding="utf-8"))
    assert written["channel"] == "#incidents"


def test_slack_post_without_a_token_does_not_post(result, tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    outcome = deliver(result, tmp_path, "#incidents", post=True)
    assert outcome["posted"] is False
    assert "SLACK_BOT_TOKEN" in outcome["reason"]


def test_report_contains_every_section(result):
    md = render_markdown(result, repo_url="https://github.com/acme/checkout-platform")
    for heading in ("## Summary", "## Impact", "## Root-cause analysis",
                    "## Correlated commits", "## Remediation", "## Runbooks consulted",
                    "## Investigation trace"):
        assert heading in md, f"missing {heading}"
    assert "bbbbbbbbbb" in md
    assert "DIAGNOSED" in md


def test_report_marks_low_confidence_incidents_for_review(result):
    result.diagnosis.needs_human = True
    assert "NEEDS HUMAN REVIEW" in render_markdown(result)


def test_impact_estimate_serializes(result):
    payload = result.to_dict()
    assert payload["impact"]["affected_users_point"] > 0
    assert isinstance(ImpactEstimate(**payload["impact"]), ImpactEstimate)
    assert payload["alert"]["started_at"].endswith("Z")
