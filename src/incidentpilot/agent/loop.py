"""The agent loop.

A hand-written tool-use loop rather than the SDK's tool runner, for two reasons: the
resolution report needs the full tool transcript (what was inspected, in what order,
and how long each call took), and the eval needs a hard turn ceiling per incident so
one pathological run cannot spend the whole budget.

Model defaults: claude-opus-5 with adaptive thinking, effort from config, streaming so
long investigations do not hit the HTTP timeout, and a prompt-cache breakpoint on the
static system prompt.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..models import Alert, CommitCandidate, Diagnosis, RunbookChunk, ToolCall
from .prompts import SYSTEM_PROMPT, build_incident_prompt
from .tools import TERMINAL_TOOL, TOOLS, ToolContext, dispatch

log = logging.getLogger("incidentpilot.agent")

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AgentUnavailable(RuntimeError):
    """Raised when the Anthropic SDK or credentials are missing."""


def _accumulate(usage: dict[str, int], response: Any) -> None:
    src = getattr(response, "usage", None)
    if not src:
        return
    for field in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens"):
        value = getattr(src, field, None)
        if isinstance(value, int):
            usage[field] = usage.get(field, 0) + value


def _diagnosis_from_tool_input(payload: dict[str, Any]) -> Diagnosis:
    sha = str(payload.get("offending_sha") or "").strip()
    return Diagnosis(
        root_cause=str(payload.get("root_cause", "")).strip(),
        offending_sha=sha or None,
        confidence=float(payload.get("confidence", 0.0)),
        reasoning=str(payload.get("reasoning", "")).strip(),
        evidence=[str(x) for x in payload.get("evidence", [])],
        ruled_out=[str(x) for x in payload.get("ruled_out", [])],
        runbook_ids=[str(x) for x in payload.get("runbook_ids", [])],
        remediation=[str(x) for x in payload.get("remediation", [])],
        rollback_command=str(payload.get("rollback_command", "")).strip(),
        needs_human=bool(payload.get("needs_human", False)),
        source="agent",
    )


# The ranker's measured top-1 precision on the replay corpus (eval/run_eval.py). It is a
# candidate generator, not a diagnostician: 100% top-3 recall, ~35% top-1. Re-derive this
# if the ranker's signals or weights change.
RANKER_BASE_PRECISION = 0.35

# It reads commit metadata and never a diff, so it is capped well below any threshold at
# which a verdict would be acted on unreviewed.
HEURISTIC_CONFIDENCE_CEILING = 0.45


def _ranker_confidence(top_score: float, margin: float, n_candidates: int) -> float:
    """Confidence for a metadata-only verdict, anchored to measured precision.

    Two earlier formulas keyed off the absolute score and off the margin. Both were
    uncalibrated, and the margin version was measurably worse. The reason is in the data:
    across the replay corpus the leader-to-runner-up margin is 0.061 when the ranker is
    right and 0.053 when it is wrong -- the distributions overlap almost entirely, so no
    monotone function of (score, margin) can separate the two.

    That is not a tuning failure, it is a statement about the inputs. Deciding between two
    plausible commits requires knowing what their diffs *do*, which is information the
    ranker never sees. So it reports a confidence anchored near its measured precision and
    defers; discriminating is the agent's job.
    """
    if n_candidates == 0:
        return 0.0
    nudge = min(margin, 0.15) if n_candidates > 1 else 0.0
    return round(min(HEURISTIC_CONFIDENCE_CEILING, RANKER_BASE_PRECISION + nudge), 2)


def heuristic_diagnosis(alert: Alert, candidates: list[CommitCandidate],
                        runbooks: list[RunbookChunk]) -> Diagnosis:
    """Deterministic verdict from the ranker alone.

    Used when no Anthropic credentials are configured, when the agent exhausts its turn
    budget, and as the control arm in the eval so the LLM's contribution is measurable.
    """
    if not candidates:
        return Diagnosis(
            root_cause=f"No code change correlates with the {alert.service} alert in the blast window.",
            offending_sha=None, confidence=0.1,
            reasoning="No commit landed in the correlation window, so this is likely infrastructure, "
                      "traffic, or a dependency outside the repository.",
            evidence=[], ruled_out=[], runbook_ids=[rb.runbook_id for rb in runbooks[:2]],
            remediation=["Check infrastructure and third-party status dashboards.",
                         "Page the service owner if the error rate is still climbing."],
            needs_human=True, source="heuristic",
        )

    top = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None
    margin = top.score - runner_up.score if runner_up else top.score
    confidence = _ranker_confidence(top.score, margin, len(candidates))

    return Diagnosis(
        root_cause=f"Likely caused by {top.short_sha}: {top.subject}",
        offending_sha=top.sha,
        confidence=confidence,
        reasoning="Ranked first by correlation heuristics: " + "; ".join(top.rationale),
        evidence=[f"{top.short_sha} touches {len(top.files)} files ({top.churn} lines) "
                  f"in {', '.join(top.services) or 'unmapped paths'}"],
        ruled_out=[f"{c.short_sha} ({c.subject[:60]}) scored {c.score}" for c in candidates[1:4]],
        runbook_ids=[rb.runbook_id for rb in runbooks[:2]],
        remediation=[f"Revert {top.short_sha} and redeploy {alert.service}.",
                     "Confirm the error rate returns to baseline within 10 minutes.",
                     "If it does not, widen the correlation window and re-run."],
        rollback_command=f"git revert --no-edit {top.short_sha}",
        # Always. A verdict from metadata alone is a lead to check, not a conclusion.
        needs_human=True,
        source="heuristic",
    )


class DiagnosisAgent:
    """Runs one incident to a verdict."""

    def __init__(self, model: str, effort: str = "high", max_tokens: int = 16000,
                 max_turns: int = 14, client: Any = None):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.max_turns = max_turns
        self._use_fallbacks = True
        if client is not None:
            self.client = client
        else:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on install
                raise AgentUnavailable("the `anthropic` package is not installed") from exc
            self.client = anthropic.Anthropic()

    # ------------------------------------------------------------------ requests

    def _request_kwargs(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
            "system": [{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            "tools": TOOLS,
            "messages": messages,
        }

    def _create(self, messages: list[dict[str, Any]]) -> Any:
        """One streamed turn. Server-side refusal fallbacks are on by default; if the
        installed SDK or account does not know that beta, drop it and carry on."""
        kwargs = self._request_kwargs(messages)
        if self._use_fallbacks:
            try:
                with self.client.beta.messages.stream(
                    **kwargs, betas=[FALLBACK_BETA], fallbacks="default",
                ) as stream:
                    return stream.get_final_message()
            except Exception as exc:
                if not _is_unsupported_feature(exc):
                    raise
                log.warning("server-side fallbacks unavailable (%s); continuing without", exc)
                self._use_fallbacks = False
        with self.client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    # ---------------------------------------------------------------------- loop

    def run(self, ctx: ToolContext, runbooks: list[RunbookChunk], impact: dict,
            topology_summary: dict) -> tuple[Diagnosis, list[ToolCall], dict[str, int]]:
        prompt = build_incident_prompt(ctx.alert, ctx.candidates, runbooks, impact, topology_summary)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        transcript: list[ToolCall] = []
        usage: dict[str, int] = {}

        for turn in range(self.max_turns):
            response = self._create(messages)
            _accumulate(usage, response)

            if getattr(response, "stop_reason", None) == "refusal":
                details = getattr(response, "stop_details", None)
                reason = getattr(details, "explanation", None) or "request declined by safety classifier"
                log.warning("model refused on turn %d: %s", turn, reason)
                return _refusal_diagnosis(reason), transcript, usage

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})

            if not tool_uses:
                # No tool call and no verdict: nudge once, then give up to the heuristic.
                if turn < self.max_turns - 1:
                    messages.append({
                        "role": "user",
                        "content": "You have not called submit_diagnosis yet. Call it now with your "
                                   "best verdict, using needs_human if the evidence is inconclusive.",
                    })
                    continue
                break

            results: list[dict[str, Any]] = []
            verdict: Diagnosis | None = None
            for block in tool_uses:
                args = dict(block.input) if isinstance(block.input, dict) else json.loads(block.input)
                if block.name == TERMINAL_TOOL:
                    verdict = _diagnosis_from_tool_input(args)
                    transcript.append(ToolCall(
                        name=block.name, arguments=args,
                        result_preview="verdict recorded", duration_ms=0,
                    ))
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": "Diagnosis recorded. Investigation closed."})
                    continue

                started = time.perf_counter()
                output = dispatch(block.name, args, ctx)
                elapsed = int((time.perf_counter() - started) * 1000)
                transcript.append(ToolCall(
                    name=block.name, arguments=args,
                    result_preview=output[:280].replace("\n", " "), duration_ms=elapsed,
                ))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

            if verdict is not None:
                return verdict, transcript, usage

            messages.append({"role": "user", "content": results})

        log.warning("agent exhausted %d turns without a verdict; using heuristic", self.max_turns)
        fallback = heuristic_diagnosis(ctx.alert, ctx.candidates, runbooks)
        fallback.needs_human = True
        fallback.reasoning = (
            "Agent did not converge within its turn budget. Heuristic verdict: " + fallback.reasoning
        )
        return fallback, transcript, usage


def _refusal_diagnosis(reason: str) -> Diagnosis:
    return Diagnosis(
        root_cause="Diagnosis not produced: the model declined the request.",
        offending_sha=None, confidence=0.0, reasoning=reason,
        evidence=[], ruled_out=[], runbook_ids=[],
        remediation=["Escalate to the on-call engineer; automated diagnosis is unavailable for this alert."],
        needs_human=True, source="agent",
    )


def _is_unsupported_feature(exc: Exception) -> bool:
    """True when the SDK/account does not support the fallbacks beta, as opposed to a
    real API failure we should surface."""
    if isinstance(exc, TypeError):
        return True
    text = str(exc).lower()
    markers = ("fallbacks", "unsupported beta", "unknown beta", "server-side-fallback",
               "unexpected keyword")
    return any(m in text for m in markers) and "rate" not in text
