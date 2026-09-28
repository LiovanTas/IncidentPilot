"""User-impact estimation.

Turns an alert's metrics plus the service topology into a defensible number:
how many real people hit a failure, how many requests died, and how much of the
30-day error budget just burned. The agent quotes this in the Slack brief, so the
method has to be inspectable rather than a magic constant.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .models import Alert, ImpactEstimate

DEFAULT_BASELINE_ERROR_RATE = 0.002
UNCERTAINTY_BAND = 0.40


@lru_cache(maxsize=8)
def load_topology(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dependents_of(service: str, topology: dict) -> list[str]:
    """Reverse edges: services that call `service` and therefore inherit its failure."""
    return sorted(
        name for name, spec in topology.get("services", {}).items()
        if service in spec.get("depends_on", [])
    )


def blast_radius(service: str, topology: dict, depth: int = 2) -> list[str]:
    seen = {service}
    frontier = [service]
    for _ in range(depth):
        nxt: list[str] = []
        for node in frontier:
            for dependent in dependents_of(node, topology):
                if dependent not in seen:
                    seen.add(dependent)
                    nxt.append(dependent)
        frontier = nxt
        if not frontier:
            break
    return sorted(seen)


def _unique_users(uph: float, hours: float) -> float:
    """Unique users seen in a window shorter than an hour overlap heavily, so the
    count grows sublinearly below 1h and linearly above it."""
    if hours <= 0:
        return 0.0
    if hours < 1.0:
        return uph * (hours ** 0.85)
    return uph * hours


def _error_delta(alert: Alert) -> tuple[float, str]:
    m = alert.metrics
    after = m.get("error_rate_after", m.get("error_rate"))
    before = m.get("error_rate_before", DEFAULT_BASELINE_ERROR_RATE)
    if after is None:
        # No error-rate telemetry: fall back to a severity-implied failure fraction.
        implied = {"sev1": 0.35, "sev2": 0.10, "sev3": 0.02}.get(alert.severity, 0.10)
        return implied, f"no error-rate metric on the alert; assumed {implied:.0%} from {alert.severity}"
    delta = max(0.0, float(after) - float(before))
    return delta, f"error rate {float(before):.2%} -> {float(after):.2%} (delta {delta:.2%})"


def _duration_minutes(alert: Alert, now: datetime | None = None) -> float:
    if "duration_minutes" in alert.metrics:
        return max(1.0, float(alert.metrics["duration_minutes"]))
    now = now or datetime.now(timezone.utc)
    elapsed = (now - alert.started_at).total_seconds() / 60.0
    return max(1.0, min(elapsed, 720.0))


def estimate_impact(alert: Alert, topology: dict, now: datetime | None = None) -> ImpactEstimate:
    services = topology.get("services", {})
    spec = services.get(alert.service, {})
    rps = float(spec.get("rps", 100))
    uph = float(spec.get("unique_users_per_hour", 1000))

    delta, delta_note = _error_delta(alert)
    minutes = _duration_minutes(alert, now)
    hours = minutes / 60.0

    failed_requests = rps * minutes * 60.0 * delta

    # A user is "affected" if at least one of their requests failed.
    requests_per_user_hour = (rps * 3600.0) / uph if uph else 1.0
    requests_per_user = max(1.0, requests_per_user_hour * min(hours, 1.0))
    hit_probability = 1.0 - (1.0 - min(delta, 1.0)) ** requests_per_user

    direct_users = _unique_users(uph, hours) * hit_probability

    # Downstream user-facing services inherit a fraction of the failure.
    radius = blast_radius(alert.service, topology)
    downstream = 0.0
    for name in radius:
        if name == alert.service:
            continue
        child = services.get(name, {})
        if not child.get("user_facing"):
            continue
        child_uph = float(child.get("unique_users_per_hour", 0))
        # 60% propagation for a direct caller; assume no perfect fallbacks.
        downstream += _unique_users(child_uph, hours) * hit_probability * 0.6

    point = direct_users + downstream
    low = point * (1 - UNCERTAINTY_BAND)
    high = point * (1 + UNCERTAINTY_BAND)

    slo = topology.get("slo", {})
    target = float(slo.get("availability_target", 0.999))
    window_days = float(slo.get("window_days", 30))
    budget_minutes = (1.0 - target) * window_days * 24 * 60
    burn_pct = (delta * minutes) / budget_minutes * 100.0 if budget_minutes else 0.0

    confidence = "high" if "error_rate_after" in alert.metrics and "rps" in spec else "medium"
    if "error_rate_after" not in alert.metrics and "error_rate" not in alert.metrics:
        confidence = "low"

    method = (
        f"{delta_note}; {rps:.0f} rps x {minutes:.0f}m sustained; "
        f"{uph:,.0f} unique users/hr on {alert.service}; "
        f"P(user hit) = 1-(1-delta)^{requests_per_user:.1f} = {hit_probability:.1%}; "
        f"downstream propagation at 60% across {len(radius) - 1} dependent service(s)"
    )

    return ImpactEstimate(
        affected_users_low=int(low),
        affected_users_point=int(point),
        affected_users_high=int(high),
        failed_requests=int(failed_requests),
        duration_minutes=round(minutes, 1),
        error_budget_burn_pct=round(burn_pct, 2),
        blast_radius=radius,
        method=method,
        confidence=confidence,
    )
