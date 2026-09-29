"""Model pricing, prompt caching and cache-health reporting."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import run_eval  # noqa: E402
from incidentpilot.agent import DiagnosisAgent  # noqa: E402
from incidentpilot.agent.loop import FALLBACK_MODELS  # noqa: E402
from incidentpilot.config import load_config  # noqa: E402
from incidentpilot.pricing import cache_hit_rate, cost_usd, price_for  # noqa: E402

USAGE = {"input_tokens": 100_000, "output_tokens": 20_000,
         "cache_read_input_tokens": 300_000, "cache_creation_input_tokens": 10_000}


def test_default_model_is_sonnet(monkeypatch):
    monkeypatch.delenv("INCIDENTPILOT_MODEL", raising=False)
    assert load_config().model == "claude-sonnet-5"


def test_sonnet_costs_forty_percent_of_opus_for_the_same_tokens():
    assert cost_usd("claude-sonnet-5", USAGE) == pytest.approx(cost_usd("claude-opus-5", USAGE) * 0.4)


def test_cache_reads_bill_at_a_tenth_of_input():
    price = price_for("claude-sonnet-5")
    assert price.cache_read == pytest.approx(price.input * 0.10)
    assert price.cache_write == pytest.approx(price.input * 1.25)


def test_unknown_model_has_no_cost_rather_than_a_wrong_one():
    assert cost_usd("claude-made-up-9", USAGE) is None


def test_cache_hit_rate():
    assert cache_hit_rate(USAGE) == pytest.approx(300_000 / 410_000)
    assert cache_hit_rate({}) is None


def test_every_request_carries_both_cache_breakpoints():
    agent = DiagnosisAgent(model="claude-sonnet-5", client=SimpleNamespace())
    kwargs = agent._request_kwargs([{"role": "user", "content": "x"}])
    assert kwargs["cache_control"] == {"type": "ephemeral"}            # conversation history
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}  # tools + system


def test_request_prefix_is_byte_stable_across_turns():
    """Caching is a prefix match: anything volatile before the breakpoint breaks it."""
    agent = DiagnosisAgent(model="claude-sonnet-5", client=SimpleNamespace())
    a = agent._request_kwargs([{"role": "user", "content": "turn 1"}])
    b = agent._request_kwargs([{"role": "user", "content": "turn 2"}])
    assert a["tools"] == b["tools"] and a["system"] == b["system"]


class _RecordingStreams:
    def __init__(self):
        self.plain, self.beta = [], []
        outer = self

        class _Plain:
            def stream(self, **kw):
                outer.plain.append(kw)
                raise RuntimeError("stop here")

        class _Beta:
            def stream(self, **kw):
                outer.beta.append(kw)
                raise RuntimeError("stop here")

        self.messages = _Plain()
        self.beta_ns = SimpleNamespace(messages=_Beta())


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-haiku-4-5"])
def test_models_without_documented_fallbacks_never_send_the_beta(model):
    streams = _RecordingStreams()
    client = SimpleNamespace(messages=streams.messages, beta=streams.beta_ns)
    agent = DiagnosisAgent(model=model, client=client)
    with pytest.raises(RuntimeError):
        agent._create([{"role": "user", "content": "x"}])
    assert streams.beta == [] and len(streams.plain) == 1
    assert "fallbacks" not in streams.plain[0]


def test_opus_still_requests_server_side_fallbacks():
    assert "claude-opus-5" in FALLBACK_MODELS
    streams = _RecordingStreams()
    agent = DiagnosisAgent(model="claude-opus-5",
                           client=SimpleNamespace(messages=streams.messages, beta=streams.beta_ns))
    with pytest.raises(RuntimeError):
        agent._create([{"role": "user", "content": "x"}])
    assert streams.beta and streams.beta[0]["fallbacks"] == "default"


def test_zero_cache_reads_across_a_run_is_reported():
    broken = {"n": 20, "total_usage": {"input_tokens": 500_000, "cache_read_input_tokens": 0}}
    working = {"n": 20, "total_usage": {"input_tokens": 50_000, "cache_read_input_tokens": 450_000}}
    assert "not working" in run_eval.caching_problem(broken)
    assert run_eval.caching_problem(working) is None


def _resp(read, created=0):
    return SimpleNamespace(usage=SimpleNamespace(
        input_tokens=10, output_tokens=100,
        cache_read_input_tokens=read, cache_creation_input_tokens=created))


def test_first_request_may_miss_but_followups_must_read():
    from incidentpilot.agent.loop import _accumulate
    usage: dict = {}
    _accumulate(usage, _resp(read=0, created=4000))    # cold cache: fine
    _accumulate(usage, _resp(read=4000, created=900))  # reads the prefix: fine
    assert usage["requests"] == 2 and usage.get("uncached_followups", 0) == 0
    _accumulate(usage, _resp(read=0, created=5000))    # follow-up read nothing: a miss
    assert usage["uncached_followups"] == 1


def test_partial_cache_misses_are_reported_even_when_the_total_looks_healthy():
    summary = {"n": 20, "total_usage": {"input_tokens": 50, "cache_read_input_tokens": 400_000,
                                        "requests": 90, "uncached_followups": 3}}
    assert "partially missed" in run_eval.caching_problem(summary)


def test_opus_5_5_cache_reads_bill_at_a_twentieth():
    price = price_for("claude-opus-5-5")
    assert price.input == 4.00 and price.cache_read == pytest.approx(price.input * 0.05)
