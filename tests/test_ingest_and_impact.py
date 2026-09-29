"""Alert normalization and impact estimation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from incidentpilot.impact import blast_radius, dependents_of, estimate_impact
from incidentpilot.ingest import alert_keywords, normalize, normalize_severity
from incidentpilot.models import Alert

TOPOLOGY = json.loads(
    (Path(__file__).resolve().parents[1] / "data" / "topology.json").read_text(encoding="utf-8")
)


def make_alert(**overrides) -> Alert:
    base = dict(
        fingerprint="test", source="generic", service="checkout-api", severity="sev1",
        title="checkout 5xx", description="errors",
        started_at=datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
        metrics={"error_rate_before": 0.002, "error_rate_after": 0.084, "duration_minutes": 22},
    )
    base.update(overrides)
    return Alert(**base)


def test_alertmanager_payload_is_normalized():
    payload = {
        "status": "firing",
        "alerts": [{
            "fingerprint": "abc123",
            "labels": {"alertname": "CheckoutHighErrorRate", "service": "checkout-api",
                       "severity": "critical", "error_rate": "0.084"},
            "annotations": {"summary": "checkout 5xx above 8%", "description": "pool timeouts",
                            "runbook_url": "https://wiki/runbooks/checkout"},
            "startsAt": "2026-08-10T09:00:00Z",
        }],
    }
    alerts = normalize(payload)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.source == "alertmanager"
    assert alert.service == "checkout-api"
    assert alert.severity == "sev1"
    assert alert.title == "checkout 5xx above 8%"
    assert alert.metrics["error_rate"] == pytest.approx(0.084)
    assert alert.runbook_hints == ["https://wiki/runbooks/checkout"]
    assert alert.started_at.tzinfo is not None


def test_resolved_alertmanager_payloads_are_dropped():
    payload = {
        "status": "resolved",
        "alerts": [{"labels": {"service": "cart-service", "severity": "warning"},
                    "annotations": {"summary": "recovered"}, "startsAt": "2026-08-10T09:00:00Z"}],
    }
    assert normalize(payload) == []


def test_datadog_payload_is_normalized():
    payload = {
        "alert_id": "99881",
        "title": "cart latency high",
        "body": "p99 over threshold",
        "priority": "P2",
        "tags": "service:cart-service,env:prod,team:checkout",
        "date": 1786000000,
    }
    alert = normalize(payload)[0]
    assert alert.source == "datadog"
    assert alert.service == "cart-service"
    assert alert.severity == "sev2"
    assert alert.labels["team"] == "checkout"


def test_severity_aliases():
    assert normalize_severity("critical") == "sev1"
    assert normalize_severity("warning") == "sev3"
    assert normalize_severity(None) == "sev2"
    assert normalize_severity("nonsense") == "sev2"


def test_alert_keywords_drop_stopwords_and_duplicates():
    alert = make_alert(title="connection pool exhausted", description="the pool is exhausted")
    words = alert_keywords(alert)
    assert "pool" in words
    assert "exhausted" in words
    assert "the" not in words
    assert len(words) == len(set(words))


def test_dependents_and_blast_radius():
    assert "checkout-api" in dependents_of("payments-worker", TOPOLOGY)
    radius = blast_radius("payments-worker", TOPOLOGY)
    assert "payments-worker" in radius
    assert "checkout-api" in radius
    assert "web-frontend" in radius


def test_impact_scales_with_duration_and_error_rate():
    short = estimate_impact(make_alert(metrics={"error_rate_after": 0.08, "duration_minutes": 5}), TOPOLOGY)
    long_run = estimate_impact(make_alert(metrics={"error_rate_after": 0.08, "duration_minutes": 60}), TOPOLOGY)
    assert long_run.affected_users_point > short.affected_users_point
    assert long_run.failed_requests > short.failed_requests

    mild = estimate_impact(make_alert(metrics={"error_rate_after": 0.01, "duration_minutes": 30}), TOPOLOGY)
    severe = estimate_impact(make_alert(metrics={"error_rate_after": 0.40, "duration_minutes": 30}), TOPOLOGY)
    assert severe.affected_users_point > mild.affected_users_point


def test_impact_band_brackets_the_point_estimate():
    estimate = estimate_impact(make_alert(), TOPOLOGY)
    assert estimate.affected_users_low < estimate.affected_users_point < estimate.affected_users_high
    assert estimate.error_budget_burn_pct > 0
    assert estimate.confidence == "high"


def test_impact_without_metrics_is_marked_low_confidence():
    estimate = estimate_impact(make_alert(metrics={}), TOPOLOGY)
    assert estimate.confidence == "low"
    assert estimate.affected_users_point > 0
    assert "assumed" in estimate.method


def test_duration_defaults_to_elapsed_time_when_absent():
    started = datetime.now(timezone.utc) - timedelta(minutes=17)
    estimate = estimate_impact(make_alert(started_at=started, metrics={"error_rate_after": 0.05}), TOPOLOGY)
    assert 15 <= estimate.duration_minutes <= 20


# --------------------------------------------------- impact cannot exceed reality


def _daily(spec):
    return spec.get("daily_unique_users", spec.get("unique_users_per_hour", 0) * 6)


def _population(radius, topology, service):
    """Largest user-facing daily population in the blast radius -- the hard ceiling.

    When nothing in the radius is user-facing (a backend worker whose failure nobody
    calls into), the alerting service's own population is the bound.
    """
    return max(
        (_daily(topology["services"][name]) for name in radius
         if topology["services"][name].get("user_facing")),
        default=_daily(topology["services"][service]),
    )


@pytest.mark.parametrize("service", sorted(TOPOLOGY["services"]))
@pytest.mark.parametrize("error_rate,minutes", [
    (0.02, 720), (0.05, 240), (0.23, 18), (0.5, 60), (1.0, 1440),
])
def test_impact_never_exceeds_the_population_that_exists(service, error_rate, minutes):
    """A 12h outage once claimed 1.58M affected users against a 196k population, because
    reach grew linearly with duration and downstream services were added whole."""
    estimate = estimate_impact(
        make_alert(service=service,
                   metrics={"error_rate_after": error_rate, "duration_minutes": minutes}),
        TOPOLOGY,
    )
    ceiling = _population(estimate.blast_radius, TOPOLOGY, service)
    assert estimate.affected_users_high <= ceiling, (
        f"{service} claims {estimate.affected_users_high:,} affected "
        f"but only {ceiling:,} people exist in the blast radius"
    )


def test_longer_outages_reach_proportionally_fewer_new_people():
    """Doubling the duration must not double the audience -- the same users come back."""
    short = estimate_impact(make_alert(metrics={"error_rate_after": 0.1, "duration_minutes": 60}), TOPOLOGY)
    long_run = estimate_impact(make_alert(metrics={"error_rate_after": 0.1, "duration_minutes": 480}), TOPOLOGY)
    assert long_run.affected_users_point > short.affected_users_point
    assert long_run.affected_users_point < short.affected_users_point * 8


def test_downstream_services_do_not_double_count_shared_users():
    """web-frontend calls cart-service; a user going through both is one person."""
    estimate = estimate_impact(
        make_alert(service="cart-service", metrics={"error_rate_after": 1.0, "duration_minutes": 60}),
        TOPOLOGY,
    )
    naive_sum = sum(
        TOPOLOGY["services"][n]["unique_users_per_hour"]
        for n in estimate.blast_radius if TOPOLOGY["services"][n].get("user_facing")
    )
    assert estimate.affected_users_point < naive_sum
