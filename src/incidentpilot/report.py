"""Resolution report generation.

The report is the artifact that replaces the manual write-up: it is what a responder
would otherwise assemble by hand from Grafana, git log and the runbook wiki.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import IncidentResult


def _fmt_confidence(value: float) -> str:
    label = "high" if value >= 0.75 else "medium" if value >= 0.5 else "low"
    return f"{value:.0%} ({label})"


def render_markdown(result: IncidentResult, repo_url: str = "") -> str:
    alert = result.alert
    diag = result.diagnosis
    impact = result.impact
    lines: list[str] = []

    lines.append(f"# {result.incident_id} — {alert.service}: {alert.title}")
    lines.append("")
    status = "NEEDS HUMAN REVIEW" if diag.needs_human else "DIAGNOSED"
    lines.append(
        f"**{alert.severity.upper()}** · **{status}** · fired "
        f"{alert.started_at.isoformat()} · diagnosed in {result.duration_seconds:.1f}s "
        f"({len(result.tool_calls)} tool calls)"
    )
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(diag.root_cause or "_No root cause identified._")
    lines.append("")
    if diag.offending_sha:
        short = diag.offending_sha[:10]
        link = f"[`{short}`]({repo_url}/commit/{diag.offending_sha})" if repo_url else f"`{short}`"
        lines.append(f"**Offending commit:** {link} · **Confidence:** {_fmt_confidence(diag.confidence)}")
    else:
        lines.append(f"**Offending commit:** not identified · **Confidence:** {_fmt_confidence(diag.confidence)}")
    lines.append("")

    lines.append("## Impact")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"| --- | --- |")
    lines.append(f"| Users affected | ~{impact.affected_users_point:,} "
                 f"({impact.affected_users_low:,}–{impact.affected_users_high:,}) |")
    lines.append(f"| Failed requests | {impact.failed_requests:,} |")
    lines.append(f"| Duration | {impact.duration_minutes:.0f} min |")
    lines.append(f"| Error budget burned | {impact.error_budget_burn_pct}% of the 30-day budget |")
    lines.append(f"| Blast radius | {', '.join(impact.blast_radius)} |")
    lines.append(f"| Estimate confidence | {impact.confidence} |")
    lines.append("")
    lines.append(f"_Method: {impact.method}_")
    lines.append("")

    lines.append("## Root-cause analysis")
    lines.append("")
    lines.append(diag.reasoning or "_none recorded_")
    lines.append("")
    if diag.evidence:
        lines.append("### Evidence")
        lines.append("")
        for item in diag.evidence:
            lines.append(f"- {item}")
        lines.append("")
    if diag.ruled_out:
        lines.append("### Candidates ruled out")
        lines.append("")
        for item in diag.ruled_out:
            lines.append(f"- {item}")
        lines.append("")

    lines.append("## Correlated commits")
    lines.append("")
    if result.candidates:
        lines.append("| # | Commit | Authored | Author | Score | Subject |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for i, c in enumerate(result.candidates[:10], 1):
            marker = " ⬅︎" if diag.offending_sha and c.sha.startswith(diag.offending_sha[:10]) else ""
            subject = c.subject.replace("|", "\\|")[:70]
            lines.append(
                f"| {i} | `{c.short_sha}`{marker} | {c.authored_at.strftime('%Y-%m-%d %H:%M')} | "
                f"{c.author} | {c.score} | {subject} |"
            )
    else:
        lines.append("_No commits landed in the correlation window._")
    lines.append("")

    lines.append("## Remediation")
    lines.append("")
    if diag.remediation:
        for i, step in enumerate(diag.remediation, 1):
            lines.append(f"{i}. {step}")
    else:
        lines.append("_none recorded_")
    lines.append("")
    if diag.rollback_command:
        lines.append("```bash")
        lines.append(diag.rollback_command)
        lines.append("```")
        lines.append("")

    if result.runbooks:
        lines.append("## Runbooks consulted")
        lines.append("")
        for rb in result.runbooks:
            cited = " **(applied)**" if rb.runbook_id in diag.runbook_ids else ""
            lines.append(f"- `{rb.runbook_id}` — {rb.heading} (retrieval score {rb.score}){cited}")
        lines.append("")

    lines.append("## Investigation trace")
    lines.append("")
    if result.tool_calls:
        lines.append("| Step | Tool | Arguments | Result |")
        lines.append("| --- | --- | --- | --- |")
        for i, call in enumerate(result.tool_calls, 1):
            args = json.dumps(call.arguments)[:80].replace("|", "\\|")
            preview = call.result_preview[:80].replace("|", "\\|")
            lines.append(f"| {i} | `{call.name}` ({call.duration_ms}ms) | `{args}` | {preview} |")
    else:
        lines.append("_No tool calls (heuristic path)._")
    lines.append("")

    if result.usage:
        lines.append("## Cost")
        lines.append("")
        parts = [f"{k.replace('_', ' ')}: {v:,}" for k, v in sorted(result.usage.items())]
        lines.append(" · ".join(parts))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"_Generated by IncidentPilot · diagnosis source: {diag.source}_")
    return "\n".join(lines)


def write_report(result: IncidentResult, out_dir: Path, repo_url: str = "") -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result.incident_id}.md"
    path.write_text(render_markdown(result, repo_url), encoding="utf-8")
    return path


def write_json(result: IncidentResult, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result.incident_id}.json"
    path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    return path
