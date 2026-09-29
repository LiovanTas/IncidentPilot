"""Tool definitions and dispatch for the diagnosis agent.

The tool list is a module-level constant so its JSON serialization is byte-stable
across incidents -- tools are rendered before `system` in the cache prefix, so any
churn here invalidates every downstream cache hit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from ..gitctx import GitError, GitRepo
from ..impact import blast_radius, dependents_of, estimate_impact
from ..models import Alert, CommitCandidate
from ..rag import RunbookIndex

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_runbooks",
        "description": (
            "Semantic + keyword search over the team's runbook corpus. Use it to find the "
            "documented diagnostic procedure for a symptom, and to check whether this "
            "failure mode has a known cause. Query with symptoms and mechanisms "
            "('connection pool exhaustion checkout', 'p99 latency after index change'), "
            "not with commit SHAs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language symptom or mechanism."},
                "top_k": {"type": "integer", "description": "Number of chunks to return (1-8). Default 4."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_commit_diff",
        "description": (
            "Full unified diff and stat for one commit, truncated at ~6k characters. This is "
            "your primary evidence: read it before accepting or rejecting a candidate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sha": {"type": "string", "description": "Commit SHA (short form is fine)."},
            },
            "required": ["sha"],
        },
    },
    {
        "name": "list_commit_candidates",
        "description": (
            "Re-run commit correlation with different parameters -- a wider lookback window, or "
            "the perspective of a different service. Use it when the initial candidate list "
            "looks like it is missing the real change (for example when the symptom points "
            "upstream of the alerting service)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service to correlate against. Defaults to the alerting service."},
                "lookback_hours": {"type": "number", "description": "Window size in hours (1-336). Default is the incident's configured window."},
                "limit": {"type": "integer", "description": "Max commits to return. Default 10."},
            },
            "required": [],
        },
    },
    {
        "name": "get_file_history",
        "description": (
            "Recent commits touching one file or directory, newest first. Use it to see whether "
            "a suspicious file was churned repeatedly, or to find an earlier change that set up "
            "the failure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative path or directory."},
                "limit": {"type": "integer", "description": "Number of commits. Default 8."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_service_topology",
        "description": (
            "Dependency graph, ownership, traffic volume and blast radius for a service. Use it "
            "to decide whether a change in another service could reach the one that is alerting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name."},
            },
            "required": ["service"],
        },
    },
    {
        "name": "estimate_user_impact",
        "description": (
            "Recompute the user-impact estimate with corrected parameters, for when the alert's "
            "own metrics understate or overstate the failure (for example when only one "
            "endpoint is affected, or when the outage ran longer than the alert window)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service the failure is measured on."},
                "error_rate_after": {"type": "number", "description": "Observed failure fraction, 0-1."},
                "duration_minutes": {"type": "number", "description": "Minutes the failure has been live."},
            },
            "required": ["service", "error_rate_after", "duration_minutes"],
        },
    },
    {
        "name": "submit_diagnosis",
        "description": (
            "Record the final verdict and end the investigation. Call this exactly once, after "
            "you have read the evidence. If you cannot single out one commit, still call it -- "
            "with needs_human set to true and a low confidence."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "root_cause": {
                    "type": "string",
                    "description": "One sentence naming the mechanism, not the symptom.",
                },
                "offending_sha": {
                    "type": "string",
                    "description": "SHA of the commit that caused this, or the empty string if none was identified.",
                },
                "confidence": {
                    "type": "number",
                    "description": "0.0-1.0. Below 0.5 means you are guessing.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Two to four sentences: the mechanism, and the evidence in the diff that proves it.",
                },
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concrete observations, e.g. 'pool_size lowered 50->5 in db.py:31'.",
                },
                "ruled_out": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Candidates considered and rejected, each with the reason.",
                },
                "runbook_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Runbook ids whose guidance you applied.",
                },
                "remediation": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered actions for the on-call engineer, most urgent first.",
                },
                "rollback_command": {
                    "type": "string",
                    "description": "Exact command to revert, or the empty string if a revert is not the right move.",
                },
                "needs_human": {
                    "type": "boolean",
                    "description": "True when the evidence does not single out one cause.",
                },
            },
            "required": [
                "root_cause", "offending_sha", "confidence", "reasoning", "evidence",
                "ruled_out", "runbook_ids", "remediation", "rollback_command", "needs_human",
            ],
            "additionalProperties": False,
        },
    },
]

TERMINAL_TOOL = "submit_diagnosis"

# Offered only when the `enable_read_file` variant is on, so the default tool list -- and
# therefore the default cache prefix -- is unchanged.
READ_FILE_TOOL: dict[str, Any] = {
    "name": "read_file_at_commit",
    "description": (
        "Read an entire file as it existed at a given commit. A diff shows which lines "
        "changed; this shows what they mean -- what a changed constant controls, which "
        "code paths call a modified function, what a flag's other branch does. Use it when "
        "a hunk alone does not tell you whether a change can produce the symptom."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sha": {"type": "string", "description": "Commit SHA; the file is read as of this commit."},
            "path": {"type": "string", "description": "Repo-relative file path."},
        },
        "required": ["sha", "path"],
    },
}


def tools_for(enable_read_file: bool = False) -> list[dict[str, Any]]:
    """The tool list for one agent configuration. Built once per agent so its serialized
    form is byte-stable across every request that agent makes."""
    if not enable_read_file:
        return TOOLS
    # Inserted before the terminal tool so submit_diagnosis stays last.
    return [*TOOLS[:-1], READ_FILE_TOOL, TOOLS[-1]]


@dataclass
class ToolContext:
    """Everything the tools need to answer. One per incident."""

    alert: Alert
    repo: GitRepo
    index: RunbookIndex
    topology: dict
    candidates: list[CommitCandidate]
    lookback_hours: float
    keywords: list[str]


def _fmt_candidates(candidates: list[CommitCandidate]) -> str:
    if not candidates:
        return "No commits matched that window."
    rows = []
    for c in candidates:
        rows.append(
            f"{c.short_sha}  score={c.score}  {c.authored_at.isoformat()}  {c.author}\n"
            f"  {c.subject}\n"
            f"  services={', '.join(c.services) or 'unmapped'}  files={len(c.files)}  churn={c.churn}\n"
            f"  signals={json.dumps(c.signals)}\n"
            f"  why={'; '.join(c.rationale) or 'n/a'}"
        )
    return "\n".join(rows)


def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> str:
    """Execute one tool call and return its result as text. Never raises."""
    try:
        return _dispatch(name, args, ctx)
    except GitError as exc:
        return f"git error: {exc}"
    except Exception as exc:  # a tool crash must not kill the incident response
        return f"tool error ({type(exc).__name__}): {exc}"


def _dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> str:
    if name == "search_runbooks":
        top_k = max(1, min(int(args.get("top_k", 4)), 8))
        hits = ctx.index.search(str(args["query"]), top_k=top_k)
        if not hits:
            return "No runbook matched that query."
        return "\n\n".join(
            f"### {h.runbook_id} :: {h.heading}  (score {h.score})\n{h.text}" for h in hits
        )

    if name == "get_commit_diff":
        sha = str(args["sha"]).strip()
        return ctx.repo.show(sha)

    if name == "list_commit_candidates":
        from ..gitctx import collect_candidates

        service = str(args.get("service") or ctx.alert.service)
        lookback = float(args.get("lookback_hours") or ctx.lookback_hours)
        lookback = max(1.0, min(lookback, 336.0))
        limit = max(1, min(int(args.get("limit", 10)), 25))
        probe = replace(ctx.alert, service=service)
        found = collect_candidates(ctx.repo, probe, ctx.topology, ctx.keywords, lookback, limit)
        header = f"{len(found)} commit(s) in a {lookback:.0f}h window correlated against {service}:\n"
        return header + _fmt_candidates(found)

    if name == "read_file_at_commit":
        return ctx.repo.file_at(str(args["sha"]).strip(), str(args["path"]).strip())

    if name == "get_file_history":
        limit = max(1, min(int(args.get("limit", 8)), 30))
        out = ctx.repo.file_history(str(args["path"]), limit)
        return out or "No commits touch that path in this repository."

    if name == "get_service_topology":
        service = str(args["service"])
        spec = ctx.topology.get("services", {}).get(service)
        if not spec:
            known = ", ".join(sorted(ctx.topology.get("services", {})))
            return f"Unknown service '{service}'. Known services: {known}"
        payload = {
            "service": service,
            "owner": spec.get("owner"),
            "tier": spec.get("tier"),
            "user_facing": spec.get("user_facing"),
            "rps": spec.get("rps"),
            "unique_users_per_hour": spec.get("unique_users_per_hour"),
            "code_paths": spec.get("paths", []),
            "depends_on": spec.get("depends_on", []),
            "called_by": dependents_of(service, ctx.topology),
            "blast_radius": blast_radius(service, ctx.topology),
        }
        return json.dumps(payload, indent=2)

    if name == "estimate_user_impact":
        service = str(args["service"])
        probe = Alert(
            fingerprint=ctx.alert.fingerprint, source=ctx.alert.source, service=service,
            severity=ctx.alert.severity, title=ctx.alert.title, description=ctx.alert.description,
            started_at=ctx.alert.started_at, labels=dict(ctx.alert.labels),
            metrics={
                **ctx.alert.metrics,
                "error_rate_after": float(args["error_rate_after"]),
                "duration_minutes": float(args["duration_minutes"]),
            },
        )
        estimate = estimate_impact(probe, ctx.topology)
        return json.dumps(estimate.to_dict(), indent=2)

    return f"Unknown tool: {name}"
