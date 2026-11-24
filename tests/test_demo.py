"""The demo: event streaming from the agent and pipeline, replays, and the HTTP API."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from incidentpilot.agent import DiagnosisAgent, ToolContext
from incidentpilot.demo import runs as runs_mod
from incidentpilot.demo import scenarios as scenario_mod
from incidentpilot.demo.runs import Run, RunManager, answer_event
from incidentpilot.ingest import normalize
from incidentpilot.models import Alert

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "eval" / "fixtures" / "repo"
needs_fixture = pytest.mark.skipif(not (FIXTURE / ".git").exists(), reason="fixture repo not built")


# ------------------------------------------------------------------ fake client


def _msg(content, stop="tool_use"):
    return SimpleNamespace(content=content, stop_reason=stop, stop_details=None,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=50,
                                                 cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0))


class _Stream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = SimpleNamespace(stream=self._stream)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return _Stream(self.script.pop(0))


def _block(**kw):
    return SimpleNamespace(**kw)


VERDICT = {
    "root_cause": "r", "offending_sha": "", "confidence": 0.9, "reasoning": "x",
    "evidence": [], "ruled_out": [], "runbook_ids": [], "remediation": [],
    "rollback_command": "", "needs_human": True,
}


def _ctx():
    alert = Alert(fingerprint="t", source="generic", service="checkout-api", severity="sev1",
                  title="t")
    return ToolContext(alert=alert, repo=None, index=None, topology={"services": {}},
                       candidates=[], lookback_hours=72, keywords=[])


# --------------------------------------------------------------- agent events


def test_agent_emits_reasoning_notes_and_each_tool_call():
    client = FakeClient([
        _msg([_block(type="thinking", thinking="The breaker cannot cancel at 300ms."),
              _block(type="text", text="Checking topology."),
              _block(type="tool_use", id="a", name="get_service_topology",
                     input={"service": "checkout-api"})]),
        _msg([_block(type="tool_use", id="b", name="submit_diagnosis", input=VERDICT)]),
    ])
    events = []
    agent = DiagnosisAgent(model="claude-sonnet-5", client=client, show_reasoning=True)
    agent.run(_ctx(), [], {}, {}, on_event=events.append)

    kinds = [e["type"] for e in events]
    assert kinds == ["thinking_started", "reasoning", "note", "tool_start", "tool_result",
                     "thinking_started"]
    assert events[1]["text"].startswith("The breaker")
    assert events[3]["name"] == "get_service_topology" and events[4]["step"] == 1
    assert client.calls[0]["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_reasoning_display_is_off_unless_asked():
    agent = DiagnosisAgent(model="claude-sonnet-5", client=FakeClient([]))
    assert agent._request_kwargs([])["thinking"] == {"type": "adaptive"}


@needs_fixture
def test_pipeline_emits_every_stage(tmp_path):
    from incidentpilot.config import load_config
    from incidentpilot.pipeline import IncidentPilot

    record = next(r for r in json.loads((ROOT / "eval" / "incidents.json").read_text("utf-8"))
                  if r["incident_id"] == "INC-06")
    pilot = IncidentPilot(load_config(repo_path=FIXTURE, out_dir=tmp_path))
    events = []
    pilot.handle(normalize(record["alert"])[0], use_agent=False, on_event=events.append)
    kinds = [e["type"] for e in events]
    assert kinds == ["alert", "candidates", "runbooks", "impact", "verdict"]
    shortlist = events[1]
    assert shortlist["ranker_pick"] == shortlist["candidates"][0]["sha"]
    assert record["ground_truth_sha"] in {c["sha"] for c in shortlist["candidates"]}


# -------------------------------------------------------------------- answers


def _scenario(suite="synthetic", sha="abcdef1234567890"):
    return scenario_mod.Scenario(id="S-1", title="t", hook="h", suite=suite, source="src",
                                 repo="demo", alert={}, answer_sha=sha, answer_subject="s")


def test_answer_matches_on_sha_prefix():
    assert answer_event(_scenario(), "abcdef1234")["matched"] is True
    assert answer_event(_scenario(), "0000000000")["matched"] is False
    assert answer_event(_scenario(), None)["matched"] is False


def test_naming_nothing_is_right_on_a_negative_control():
    negative = _scenario(suite="negative", sha=None)
    assert answer_event(negative, None)["matched"] is True
    assert answer_event(negative, "abcdef1234")["matched"] is False


# ------------------------------------------------------------- runs and replay


def test_stream_delivers_everything_then_stops():
    run = Run(_scenario(), "replay")
    run.push({"type": "start"})
    run.push({"type": "note", "text": "x"})
    run.push({"type": "end"})
    frames = list(run.stream(heartbeat=0.01))
    events = [json.loads(f[len("data: "):]) for f in frames if f.startswith("data: ")]
    assert [e["type"] for e in events] == ["start", "note", "end"]
    assert [e["seq"] for e in events] == [0, 1, 2]


def test_replay_plays_a_recording_and_computes_the_answer_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_mod, "RECORDINGS", tmp_path)
    scenario = _scenario()
    (tmp_path / "S-1.json").write_text(json.dumps({
        "schema": 1, "scenario": "S-1", "recorded_at": "2026-11-23T10:00:00+00:00",
        "model": "claude-sonnet-5",
        "events": [
            {"type": "start", "t": 0},
            {"type": "alert", "t": 0.01},
            {"type": "verdict", "t": 0.02, "diagnosis": {"offending_sha": "abcdef1234"}},
            {"type": "done", "t": 0.03, "cost_usd": 0.05},
            # A stale answer baked into an old recording must never reach the page.
            {"type": "answer", "t": 0.04, "matched": False},
            {"type": "end", "t": 0.05},
        ],
    }), encoding="utf-8")
    run = RunManager().start(scenario, "replay", speed=8)
    events = [json.loads(f[6:]) for f in run.stream(heartbeat=0.05) if f.startswith("data: ")]
    kinds = [e["type"] for e in events]
    assert kinds == ["start", "alert", "verdict", "done", "answer", "end"]
    assert events[0]["recorded_at"].startswith("2026-11-23")
    assert events[4]["matched"] is True
    assert events[3]["t"] == 0.03  # the recorded clock, not replay time


def test_instant_replay_skips_the_recorded_pauses(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(runs_mod, "RECORDINGS", tmp_path)
    (tmp_path / "S-1.json").write_text(json.dumps({
        "schema": 1, "scenario": "S-1", "recorded_at": "2026-11-23T10:00:00+00:00",
        "model": "m", "events": [{"type": "alert", "t": 30.0}, {"type": "done", "t": 60.0}],
    }), encoding="utf-8")
    started = time.perf_counter()
    run = RunManager().start(_scenario(), "replay", speed=0)
    events = [json.loads(f[6:]) for f in run.stream(heartbeat=0.05) if f.startswith("data: ")]
    assert time.perf_counter() - started < 2
    assert [e["type"] for e in events] == ["start", "alert", "done", "answer", "end"]
    assert events[2]["t"] == 60.0  # still shows how long the real run took


def test_a_stand_in_replay_never_claims_a_match(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_mod, "RECORDINGS", tmp_path)
    (tmp_path / "S-1.json").write_text(json.dumps({
        "schema": 1, "scenario": "S-1", "recorded_at": "2026-11-24T12:00:00+00:00",
        "model": "claude-sonnet-5 (SCRIPTED STAND-IN)", "stand_in": True,
        "events": [{"type": "verdict", "t": 0.1, "diagnosis": {"offending_sha": "abcdef1234"}}],
    }), encoding="utf-8")
    run = RunManager().start(_scenario(), "replay", speed=0)
    events = [json.loads(f[6:]) for f in run.stream(heartbeat=0.05) if f.startswith("data: ")]
    answer = next(e for e in events if e["type"] == "answer")
    assert answer["scripted"] is True


def test_every_demo_incident_ships_with_a_replay():
    for s in scenario_mod.load(None):
        recording = runs_mod.load_recording(s.id)
        assert recording is not None, f"{s.id} has no replay"
        verdicts = [e for e in recording["events"] if e["type"] == "verdict"]
        assert len(verdicts) == 1


def test_replay_without_a_recording_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(runs_mod, "RECORDINGS", tmp_path)
    run = RunManager().start(_scenario(), "replay")
    events = [json.loads(f[6:]) for f in run.stream(heartbeat=0.05) if f.startswith("data: ")]
    assert events[-1]["type"] == "error" and "demo-record" in events[-1]["message"]


def test_live_without_a_key_explains_instead_of_failing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(runs_mod, "repo_available", lambda key: True)
    pilot = SimpleNamespace(config=SimpleNamespace(has_anthropic_key=False))
    manager = RunManager(pilots=SimpleNamespace(get=lambda key: pilot))
    run = manager.start(_scenario(), "live")
    events = [json.loads(f[6:]) for f in run.stream(heartbeat=0.05) if f.startswith("data: ")]
    assert events[-1]["type"] == "error" and "ANTHROPIC_API_KEY" in events[-1]["message"]


# ------------------------------------------------------------------- scenarios


def test_every_curated_scenario_resolves_to_a_real_incident():
    loaded = scenario_mod.load(None)
    assert [s.id for s in loaded] == [c[1] for c in scenario_mod.CURATED]
    for s in loaded:
        assert s.alert.get("title") and s.hook
        if s.suite == "negative":
            assert s.answer_sha is None
        elif s.suite != "fresh":
            assert s.answer_sha and s.answer_subject


# ------------------------------------------------------------------------ HTTP


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from incidentpilot.server import app

    monkeypatch.setattr(runs_mod, "RECORDINGS", tmp_path)
    return TestClient(app)  # no `with`: the webhook lifespan is not needed here


FIRST = scenario_mod.CURATED[0][1]


def test_the_demo_runs_on_real_open_source_regressions():
    assert {entry[0] for entry in scenario_mod.CURATED} == {"realworld"}
    assert not scenario_mod.needs_demo_repo()
    repos = {s.repo for s in scenario_mod.load(None)}
    assert {"moby", "go", "grafana", "prometheus"} <= repos


def test_hooks_never_give_the_answer_away():
    for s in scenario_mod.load(None):
        assert s.answer_subject.lower() not in s.hook.lower()
        assert (s.answer_sha or "")[:7] not in s.hook


def test_scenarios_endpoint_reports_what_can_run(client):
    body = client.get("/api/demo/scenarios").json()
    assert body["scenarios"][0]["id"] == FIRST
    assert {"recorded_at", "live_ready", "hook"} <= set(body["scenarios"][0])


def test_run_endpoint_streams_events(client):
    run_id = client.post("/api/demo/runs", json={"scenario": FIRST}).json()["run_id"]
    with client.stream("GET", f"/api/demo/runs/{run_id}/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        text = "".join(response.iter_text())
    assert '"type": "start"' in text and "No recording" in text


def test_run_endpoint_rejects_unknown_input(client):
    assert client.post("/api/demo/runs", json={"scenario": "nope"}).status_code == 404
    assert client.post("/api/demo/runs",
                       json={"scenario": FIRST, "mode": "turbo"}).status_code == 400


def test_demo_page_and_root_redirect(client):
    assert client.get("/demo").status_code == 200
    assert client.get("/", follow_redirects=False).headers["location"] == "/demo"
