"""Normalize alerts from different monitoring providers into a single Alert shape.

Supported inbound shapes:
  * Prometheus Alertmanager webhook (`{"alerts": [...]}`)
  * Datadog webhook payload (`{"alert_id": ..., "body": ...}`)
  * IncidentPilot's own generic shape (already close to `Alert`)
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .models import Alert, parse_ts, utcnow

_SEV_ALIASES = {
    "critical": "sev1", "page": "sev1", "p1": "sev1", "sev1": "sev1", "fatal": "sev1",
    "error": "sev2", "high": "sev2", "p2": "sev2", "sev2": "sev2", "major": "sev2",
    "warning": "sev3", "warn": "sev3", "p3": "sev3", "sev3": "sev3", "minor": "sev3",
    "info": "sev3", "low": "sev3",
}

_NUMERIC_LABELS = (
    "error_rate", "error_rate_before", "error_rate_after", "latency_p99_ms",
    "latency_p50_ms", "rps", "saturation", "queue_depth", "restart_count",
    "duration_minutes", "threshold",
)


def normalize_severity(raw: str | None) -> str:
    if not raw:
        return "sev2"
    return _SEV_ALIASES.get(str(raw).strip().lower(), "sev2")


def _fingerprint(*parts: str) -> str:
    return hashlib.sha1("|".join(p for p in parts if p).encode()).hexdigest()[:16]


def _coerce_metrics(source: dict[str, Any]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for key in _NUMERIC_LABELS:
        if key in source:
            try:
                metrics[key] = float(source[key])
            except (TypeError, ValueError):
                continue
    return metrics


def _detect_source(payload: dict[str, Any]) -> str:
    if "alerts" in payload and isinstance(payload.get("alerts"), list):
        return "alertmanager"
    if "alert_id" in payload or "event_type" in payload:
        return "datadog"
    return "generic"


def from_alertmanager(payload: dict[str, Any]) -> list[Alert]:
    alerts: list[Alert] = []
    for item in payload.get("alerts", []):
        labels = {str(k): str(v) for k, v in (item.get("labels") or {}).items()}
        annotations = {str(k): str(v) for k, v in (item.get("annotations") or {}).items()}
        service = labels.get("service") or labels.get("job") or labels.get("namespace") or "unknown"
        started = item.get("startsAt") or payload.get("startsAt") or utcnow()
        alert = Alert(
            fingerprint=item.get("fingerprint") or _fingerprint(service, labels.get("alertname", "")),
            source="alertmanager",
            service=service,
            severity=normalize_severity(labels.get("severity")),
            title=annotations.get("summary") or labels.get("alertname") or "Unnamed alert",
            description=annotations.get("description", ""),
            started_at=parse_ts(started),
            labels=labels,
            metrics=_coerce_metrics({**labels, **annotations}),
            runbook_hints=[annotations["runbook_url"]] if "runbook_url" in annotations else [],
        )
        alerts.append(alert)
    return alerts


def from_datadog(payload: dict[str, Any]) -> list[Alert]:
    tags = payload.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    labels = {}
    for tag in tags:
        key, _, value = str(tag).partition(":")
        if value:
            labels[key] = value
    service = labels.get("service") or payload.get("scope") or "unknown"
    alert = Alert(
        fingerprint=str(payload.get("alert_id") or _fingerprint(service, str(payload.get("title", "")))),
        source="datadog",
        service=service,
        severity=normalize_severity(payload.get("priority") or payload.get("alert_type")),
        title=str(payload.get("title") or "Datadog monitor triggered"),
        description=str(payload.get("body") or ""),
        started_at=parse_ts(payload.get("date") or payload.get("last_updated") or utcnow()),
        labels=labels,
        metrics=_coerce_metrics({**labels, **payload}),
    )
    return [alert]


def from_generic(payload: dict[str, Any]) -> list[Alert]:
    service = str(payload.get("service") or "unknown")
    alert = Alert(
        fingerprint=str(payload.get("fingerprint") or _fingerprint(service, str(payload.get("title", "")))),
        source=str(payload.get("source") or "generic"),
        service=service,
        severity=normalize_severity(payload.get("severity")),
        title=str(payload.get("title") or "Alert"),
        description=str(payload.get("description") or ""),
        started_at=parse_ts(payload.get("started_at") or utcnow()),
        labels={str(k): str(v) for k, v in (payload.get("labels") or {}).items()},
        metrics={k: float(v) for k, v in (payload.get("metrics") or {}).items()},
        runbook_hints=list(payload.get("runbook_hints") or []),
    )
    return [alert]


def normalize(payload: dict[str, Any]) -> list[Alert]:
    """Turn any supported webhook body into a list of Alerts."""
    source = _detect_source(payload)
    if source == "alertmanager":
        return [a for a in from_alertmanager(payload) if (payload.get("status") != "resolved")]
    if source == "datadog":
        return from_datadog(payload)
    return from_generic(payload)


_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def alert_keywords(alert: Alert) -> list[str]:
    """Lowercased tokens from the alert, used for commit/runbook matching."""
    blob = " ".join(
        [alert.service, alert.title, alert.description, *alert.labels.keys(), *alert.labels.values()]
    ).lower()
    stop = {
        "the", "and", "for", "with", "this", "that", "has", "have", "from", "are", "was",
        "alert", "service", "error", "errors", "high", "is", "in", "on", "of", "to", "a",
    }
    seen: dict[str, None] = {}
    for token in _TOKEN_RE.findall(blob):
        if len(token) > 2 and token not in stop:
            seen[token] = None
    return list(seen)
