"""Slack brief generation and delivery.

Posting is off by default. The brief is always written to disk as the exact Block Kit
payload that would be sent, so it can be reviewed (or pasted into Block Kit Builder)
without touching a workspace. Set INCIDENTPILOT_SLACK_POST=1 and SLACK_BOT_TOKEN to
actually deliver it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from .models import IncidentResult

log = logging.getLogger("incidentpilot.slack")

SEVERITY_EMOJI = {"sev1": ":rotating_light:", "sev2": ":warning:", "sev3": ":large_yellow_circle:"}


def _confidence_bar(confidence: float) -> str:
    filled = max(0, min(5, round(confidence * 5)))
    return "█" * filled + "░" * (5 - filled)


def _truncate(text: str, limit: int = 280) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_blocks(result: IncidentResult, repo_url: str = "") -> list[dict[str, Any]]:
    alert = result.alert
    diag = result.diagnosis
    impact = result.impact
    emoji = SEVERITY_EMOJI.get(alert.severity, ":warning:")

    sha_text = "not identified"
    if diag.offending_sha:
        short = diag.offending_sha[:10]
        sha_text = f"<{repo_url}/commit/{diag.offending_sha}|`{short}`>" if repo_url else f"`{short}`"

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} {result.incident_id} · {alert.service}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{alert.title}*\n{_truncate(alert.description, 300)}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity*\n{alert.severity.upper()}"},
                {"type": "mrkdwn", "text": f"*Started*\n<!date^{int(alert.started_at.timestamp())}^{{time_secs}}|{alert.started_at.isoformat()}>"},
                {"type": "mrkdwn", "text": f"*Users affected*\n~{impact.affected_users_point:,} ({impact.affected_users_low:,}–{impact.affected_users_high:,})"},
                {"type": "mrkdwn", "text": f"*Error budget*\n{impact.error_budget_burn_pct}% of 30-day budget"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Suspected root cause* — confidence {_confidence_bar(diag.confidence)} "
                    f"{diag.confidence:.0%}\n{_truncate(diag.root_cause, 300)}\n\n"
                    f"*Offending commit:* {sha_text}\n{_truncate(diag.reasoning, 600)}"
                ),
            },
        },
    ]

    if diag.evidence:
        evidence = "\n".join(f"• {_truncate(e, 160)}" for e in diag.evidence[:4])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Evidence*\n{evidence}"}})

    if diag.remediation:
        steps = "\n".join(f"{i}. {_truncate(s, 160)}" for i, s in enumerate(diag.remediation[:5], 1))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Recommended actions*\n{steps}"}})

    if diag.rollback_command:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Rollback*\n```{diag.rollback_command}```"},
        })

    if diag.runbook_ids:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Runbooks: " + ", ".join(f"`{r}`" for r in diag.runbook_ids)}],
        })

    footer = (
        f"{len(result.tool_calls)} tool calls · {result.duration_seconds:.1f}s to diagnosis · "
        f"{len(result.candidates)} commits correlated · source: {diag.source}"
    )
    if diag.needs_human:
        footer = ":warning: needs human review · " + footer
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})

    return blocks


def build_payload(result: IncidentResult, channel: str, repo_url: str = "") -> dict[str, Any]:
    return {
        "channel": channel,
        "text": f"{result.incident_id} {result.alert.summary_line}",  # notification fallback
        "blocks": build_blocks(result, repo_url),
    }


def write_brief(result: IncidentResult, out_dir: Path, channel: str, repo_url: str = "") -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result.incident_id}.slack.json"
    path.write_text(json.dumps(build_payload(result, channel, repo_url), indent=2), encoding="utf-8")
    return path


def post_brief(result: IncidentResult, channel: str, repo_url: str = "") -> dict[str, Any]:
    """Deliver to Slack. Caller is responsible for checking that posting is enabled."""
    import httpx

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is not set")
    response = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json=build_payload(result, channel, repo_url),
        timeout=15.0,
    )
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"slack rejected the brief: {body.get('error')}")
    log.info("posted %s to %s (ts=%s)", result.incident_id, channel, body.get("ts"))
    return body


def deliver(result: IncidentResult, out_dir: Path, channel: str, post: bool,
            repo_url: str = "") -> dict[str, Any]:
    """Always write the payload; post only when explicitly enabled and credentialed."""
    path = write_brief(result, out_dir, channel, repo_url)
    outcome: dict[str, Any] = {"payload_path": str(path), "posted": False}
    if not post:
        outcome["reason"] = "posting disabled (INCIDENTPILOT_SLACK_POST is not set)"
        return outcome
    if not os.environ.get("SLACK_BOT_TOKEN"):
        outcome["reason"] = "posting enabled but SLACK_BOT_TOKEN is missing"
        return outcome
    body = post_brief(result, channel, repo_url)
    outcome.update(posted=True, channel=channel, ts=body.get("ts"))
    return outcome
