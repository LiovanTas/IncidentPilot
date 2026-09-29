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

# Distinct daily users as a multiple of hourly, when topology does not state it.
DEFAULT_DAILY_MULTIPLIER = 6.0

# `rps` counts all traffic a service handles, most of which is service-to-service rather
# than one person clicking. Deriving requests-per-user straight from it implies absurd
# figures (1850 rps / 42k users/hr = 158 requests per user per hour), which drives the
# probability that any given user was hit to ~100% for any error rate. Capped instead.
MAX_REQUESTS_PER_USER_HOUR = 20.0


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


def _daily_users(spec: dict) -> float:
    """Distinct people who touch a service in a day. The hard ceiling on its reach."""
    uph = float(spec.get("unique_users_per_hour", 0))
    return float(spec.get("daily_unique_users", uph * DEFAULT_DAILY_MULTIPLIER))


def _reach(spec: dict, hours: float) -> float:
    """Distinct users a service serves over `hours`.

    Reach saturates: extending an outage brings in fewer *new* people because the same
    users keep coming back. Modelled as coupon-collector style saturation toward the
    service's daily population, which makes the daily figure a hard ceiling.

        reach(t) = DAU * (1 - (1 - uph/DAU)^t)

    At t = 1 this returns uph exactly; as t grows it approaches DAU and never exceeds it.
    The previous implementation multiplied uph by hours, so a 12-hour incident claimed 12x
    the hourly population -- more people than the service has.
    """
    if hours <= 0:
        return 0.0
    uph = float(spec.get("unique_users_per_hour", 0))
    dau = _daily_users(spec)
    if uph <= 0 or dau <= 0:
        return 0.0
    if hours < 1.0:
        return uph * (hours ** 0.85)
    ratio = min(uph / dau, 1.0)
    return dau * (1.0 - (1.0 - ratio) ** hours)


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
    derived = (rps * 3600.0) / uph if uph else 1.0
    requests_per_user_hour = min(derived, MAX_REQUESTS_PER_USER_HOUR)
    requests_per_user = max(1.0, requests_per_user_hour * min(hours, 1.0))
    hit_probability = 1.0 - (1.0 - min(delta, 1.0)) ** requests_per_user

    direct_reach = _reach(spec, hours)
    direct_users = direct_reach * hit_probability

    # Downstream user-facing services share most of their audience with the service that
    # broke -- somebody browsing through web-frontend and hitting cart is one person, not
    # two. Adding whole populations double-counts them, so a dependent only contributes
    # the users it reaches *beyond* the alerting service's own reach.
    radius = blast_radius(alert.service, topology)
    downstream = 0.0
    for name in radius:
        if name == alert.service:
            continue
        child = services.get(name, {})
        if not child.get("user_facing"):
            continue
        excess = max(0.0, _reach(child, hours) - direct_reach)
        downstream += excess * hit_probability * 0.6

    point = direct_users + downstream

    # Hard ceiling: you cannot affect more people than exist in the blast radius. The
    # largest single user-facing population bounds it, because the audiences overlap
    # rather than stack.
    ceiling = max(
        (_daily_users(services.get(n, {})) for n in radius
         if services.get(n, {}).get("user_facing")),
        default=_daily_users(spec),
    )
    point = min(point, ceiling)

    low = point * (1 - UNCERTAINTY_BAND)
    high = min(point * (1 + UNCERTAINTY_BAND), ceiling)

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
        f"reach {direct_reach:,.0f} distinct users over that window "
        f"(saturating toward {_daily_users(spec):,.0f}/day, which is the hard ceiling); "
        f"P(user hit) = 1-(1-delta)^{requests_per_user:.1f} = {hit_probability:.1%}; "
        f"dependents contribute only their non-overlapping users, capped at {ceiling:,.0f}"
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
