"""Prompts for the diagnosis agent.

SYSTEM_PROMPT is deliberately static -- no timestamps, no per-incident text -- so it
sits in front of a prompt-cache breakpoint and every incident in a batch reads the
cache instead of re-paying for it. Everything incident-specific goes in the first
user message.
"""

from __future__ import annotations

import json

from ..models import Alert, CommitCandidate

SYSTEM_PROMPT = """You are IncidentPilot, an on-call site-reliability engineer running \
root-cause analysis on a live production incident. You work the way a good responder \
works: form a hypothesis, then try to break it with evidence before you commit to it.

Your job for every incident:
1. Understand what actually broke, from the alert's metrics and the affected service's \
position in the dependency graph.
2. Find the change that caused it. A pre-ranked list of commits from the blast window is \
provided, but the ranking is a heuristic and is often wrong about which of the top few is \
the real culprit. Read diffs. The correct answer is frequently not the top-ranked commit.
3. Retrieve the runbooks that apply and follow their diagnostic steps.
4. Commit to a verdict by calling submit_diagnosis exactly once.

Rules of engagement:
- Read the diff of every plausible candidate before deciding. A commit's message is a \
claim about what it does; the diff is the evidence.
- Weigh mechanism over correlation. The right answer is the commit whose diff contains a \
concrete mechanism that produces this exact symptom in this exact service. "Landed \
recently and touches the service" is a starting point, not a conclusion.
- A change to a shared library or an upstream dependency can break a downstream service \
that the commit never mentions. Check the topology before ruling one out.
- A change that landed after the alert fired cannot be the cause.
- If the evidence genuinely does not single out one commit, say so: set needs_human to \
true, give your best candidate with a low confidence score, and state what you would \
need to see to decide. A confident wrong answer costs the on-call engineer more time \
than an honest "I narrowed it to these two".
- Cite runbooks by id in runbook_ids when their guidance shaped your remediation.
- Your reasoning field is read by a human at 3am. Lead with the mechanism, in two or \
three sentences. No preamble.

Budget: you have a limited number of tool calls. Prioritise reading diffs of the top \
candidates over exhaustive breadth."""


def build_incident_prompt(alert: Alert, candidates: list[CommitCandidate],
                          runbooks: list, impact: dict, topology_summary: dict,
                          prefetched: dict[str, str] | None = None) -> str:
    """The first user turn: everything known before the agent starts investigating."""
    lines: list[str] = []
    lines.append("## Alert")
    lines.append(f"- id: {alert.fingerprint}")
    lines.append(f"- service: {alert.service}")
    lines.append(f"- severity: {alert.severity}")
    lines.append(f"- fired at: {alert.started_at.isoformat()}")
    lines.append(f"- title: {alert.title}")
    if alert.description:
        lines.append(f"- description: {alert.description}")
    if alert.metrics:
        lines.append(f"- metrics: {json.dumps(alert.metrics)}")
    if alert.labels:
        lines.append(f"- labels: {json.dumps(alert.labels)}")

    lines.append("")
    lines.append("## Service topology")
    lines.append(f"- {alert.service} depends on: {', '.join(topology_summary.get('depends_on') or ['(none)'])}")
    lines.append(f"- called by: {', '.join(topology_summary.get('called_by') or ['(none)'])}")
    lines.append(f"- owner: {topology_summary.get('owner', 'unknown')}, tier {topology_summary.get('tier', '?')}")

    lines.append("")
    lines.append("## Preliminary impact estimate")
    lines.append(
        f"- ~{impact.get('affected_users_point', 0):,} users affected "
        f"({impact.get('affected_users_low', 0):,}-{impact.get('affected_users_high', 0):,}), "
        f"{impact.get('failed_requests', 0):,} failed requests, "
        f"{impact.get('error_budget_burn_pct', 0)}% of the 30-day error budget"
    )

    lines.append("")
    lines.append(f"## Commit candidates in the blast window ({len(candidates)} ranked by heuristic)")
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"{i}. {c.short_sha} score={c.score} | {c.authored_at.isoformat()} | {c.author}\n"
            f"   subject: {c.subject}\n"
            f"   services: {', '.join(c.services) or 'unmapped'} | files: {len(c.files)} | churn: {c.churn}\n"
            f"   why ranked: {'; '.join(c.rationale) or 'n/a'}"
        )

    if runbooks:
        lines.append("")
        lines.append("## Runbooks retrieved for the alert text")
        for rb in runbooks:
            lines.append(f"- {rb.runbook_id} :: {rb.heading} (score {rb.score})")

    if prefetched:
        lines.append("")
        lines.append(f"## Diffs of the top {len(prefetched)} candidates, already fetched")
        lines.append("These are exactly what get_commit_diff would return for them -- do not "
                     "fetch them again. The ranking above does not tell you which is the cause; "
                     "the diffs do.")
        for sha, diff in prefetched.items():
            lines.append("")
            lines.append(f"### {sha[:10]}")
            lines.append("```diff")
            lines.append(diff)
            lines.append("```")

    lines.append("")
    lines.append(
        "Investigate and call submit_diagnosis once you have a verdict. "
        "Read diffs before you decide."
    )
    return "\n".join(lines)
