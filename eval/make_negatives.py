"""Negative controls: alerts with no guilty commit.

Every incident in the main suite has exactly one commit to blame, so an agent that
*always* blames something is never penalised for it. Most real alerts are not caused by
a deploy -- a provider outage, a traffic spike, a full disk, an expired certificate. The
correct answer for each alert here is to name no commit and escalate.

A negative is only worth having if its label is defensible, so its window is chosen by
rule rather than by eye: a window where nothing the alerting service could depend on
changed -- none of its own paths, its upstream dependencies' paths, or the shared
libraries -- but where other commits did land, so there is something to be tempted by.
Of the windows that qualify, the one with the most commits is used. The script asserts
each label before writing it.

Two services, checkout-api and web-frontend, have no qualifying window at all: they
depend on so much that something relevant always changed. They get no negatives rather
than a label nobody could defend.

    py -3.13 eval/make_negatives.py
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from scenarios import BASE, INCIDENTS, all_commits  # noqa: E402

OUT = HERE / "negatives.json"
TOPOLOGY = json.loads((HERE.parent / "data" / "topology.json").read_text(encoding="utf-8"))

LOOKBACK = timedelta(hours=72)
LEAD = timedelta(minutes=30)
MIN_TEMPTATIONS = 3                 # commits in the window that the agent could wrongly blame
CLEAR_OF_REAL_INCIDENTS = timedelta(hours=6)
MIN_SPACING = timedelta(hours=24)   # between two negatives

NEGATIVES = [
    {
        "id": "NEG-01",
        "service": "payments-worker",
        "severity": "sev1",
        "cause": "The external payment provider had a regional outage.",
        "title": "payments-worker settlement calls failing, 38% error rate",
        "description": (
            "Settlement requests to api.payprovider.example are failing with HTTP 503 "
            "'upstream temporarily unavailable'. Calls from payments-worker to ledger and to "
            "our own database are healthy, with normal latency. The provider's public status "
            "page shows 'Degraded performance - EU region' starting two minutes before our "
            "alert. Failures are spread evenly across all six worker pods."
        ),
        "metrics": {"error_rate_before": 0.004, "error_rate_after": 0.38, "duration_minutes": 47},
    },
    {
        "id": "NEG-02",
        "service": "auth-gateway",
        "severity": "sev1",
        "cause": "The identity provider's TLS certificate expired.",
        "title": "auth-gateway token refresh failing on every request",
        "description": (
            "Every token refresh fails with 'x509: certificate has expired or is not yet valid' "
            "when calling idp.example.com. It began on every pod within the same second, with "
            "no gradual onset. Login with a still-valid session works; only calls that must reach "
            "the identity provider fail."
        ),
        "metrics": {"error_rate_before": 0.006, "error_rate_after": 0.41, "duration_minutes": 26},
    },
    {
        "id": "NEG-03",
        "service": "search-api",
        "severity": "sev2",
        "cause": "A scraper sent six times normal traffic from one network.",
        "title": "search-api p99 at 5.1s and shedding load",
        "description": (
            "Request volume is 6.2x normal. 81% of the excess comes from a single ASN, with a "
            "user agent that rotates every few requests. Errors are 429s and 503s from our own "
            "load shedding. Latency per request at normal traffic levels, measured on the "
            "remaining traffic, is unchanged from yesterday."
        ),
        "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.09, "duration_minutes": 34,
                    "latency_p99_ms": 5100},
    },
    {
        "id": "NEG-04",
        "service": "inventory-api",
        "severity": "sev2",
        "cause": "The managed database failed over during the provider's maintenance window.",
        "title": "inventory-api connection resets, 90-second burst",
        "description": (
            "A 90-second burst of 'connection reset by peer' from the primary database, then "
            "full recovery with no action taken. The cloud provider's maintenance calendar "
            "lists a scheduled failover of this database cluster in a window that includes "
            "the alert. Replica lag briefly spiked, then returned to zero."
        ),
        "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.11, "duration_minutes": 2},
    },
    {
        "id": "NEG-05",
        "service": "notifications",
        "severity": "sev3",
        "cause": "The email provider's daily sending quota was exhausted.",
        "title": "notifications sends deferred, backlog growing",
        "description": (
            "Outbound email is being refused with 'SMTP 451 4.7.1 Daily sending quota exceeded, "
            "try again later'. Send volume today is normal for a weekday. The email provider "
            "dashboard shows the account at 100% of its daily quota, which was lowered last "
            "week by the provider after a billing plan change."
        ),
        "metrics": {"error_rate_before": 0.01, "error_rate_after": 0.64, "duration_minutes": 95},
    },
    {
        "id": "NEG-06",
        "service": "cart-service",
        "severity": "sev2",
        "cause": "One node ran out of disk space.",
        "title": "cart-service 5xx from a subset of pods",
        "description": (
            "Every failing request is served by one of the two pods scheduled on node "
            "ip-10-0-3-17. Those pods log 'no space left on device' when writing their request "
            "log. The node's disk is at 100%; the other four nodes are below 40%. Pods on the "
            "other nodes serve the same traffic with no errors."
        ),
        "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.07, "duration_minutes": 41},
    },
    {
        "id": "NEG-07",
        "service": "ledger",
        "severity": "sev2",
        "cause": "A networking fault in one cloud availability zone.",
        "title": "ledger write timeouts from one availability zone",
        "description": (
            "Write timeouts are confined to ledger pods in availability zone us-east-1b. Pods in "
            "1a and 1c are healthy. The cloud provider's health dashboard reports 'increased "
            "packet loss' for a networking component in use1-az2, which maps to 1b in our "
            "account."
        ),
        "metrics": {"error_rate_before": 0.001, "error_rate_after": 0.12, "duration_minutes": 38},
    },
    {
        "id": "NEG-08",
        "service": "inventory-api",
        "severity": "sev3",
        "cause": "A third-party supplier feed started sending malformed data.",
        "title": "inventory sync job failing to parse the supplier stock feed",
        "description": (
            "The hourly stock sync fails with 'unexpected token < at position 0' when parsing "
            "the supplier's feed at feeds.supplier.example/stock.json. Fetching that URL by "
            "hand returns an HTML maintenance page instead of JSON. Other inventory-api "
            "endpoints are healthy."
        ),
        "metrics": {"error_rate_before": 0.0, "error_rate_after": 0.05, "duration_minutes": 70},
    },
]


def _paths(commit: dict) -> list[str]:
    return [e[0] for e in commit.get("edits", [])] + list((commit.get("new_files") or {}).keys())


def _reach(service: str) -> list[str]:
    """Everything that could plausibly affect `service`: its own code, its upstream
    dependencies, and the shared libraries every service imports."""
    spec = TOPOLOGY["services"][service]
    prefixes = list(spec["paths"]) + list(TOPOLOGY["shared_paths"])
    for dep in spec.get("depends_on", []):
        prefixes += TOPOLOGY["services"][dep]["paths"]
    return prefixes


def _window(commits: list[dict], onset) -> list[dict]:
    return [c for c in commits if onset - LOOKBACK <= c["at"] <= onset + LEAD]


def pick_onset(service: str, commits: list[dict], used: list) -> tuple:
    """The most tempting window in which nothing the service depends on changed."""
    reach = _reach(service)
    real_onsets = [i["onset"] for i in INCIDENTS]
    best = None
    for hour in range(4, 470):
        onset = BASE + timedelta(hours=hour)
        if any(abs(onset - o) < CLEAR_OF_REAL_INCIDENTS for o in real_onsets):
            continue
        if any(abs(onset - u) < MIN_SPACING for u in used):
            continue
        window = _window(commits, onset)
        if len(window) < MIN_TEMPTATIONS:
            continue
        if any(p.startswith(r) for c in window for p in _paths(c) for r in reach):
            continue
        if best is None or len(window) > best[1]:
            best = (onset, len(window))
    if best is None:
        raise SystemExit(f"no qualifying window for {service}")
    return best


def main() -> int:
    commits = all_commits()
    used: list = []
    records = []
    for neg in NEGATIVES:
        onset, temptations = pick_onset(neg["service"], commits, used)
        used.append(onset)
        window = _window(commits, onset)
        # The label, asserted: nothing that could reach this service changed in its window.
        reach = _reach(neg["service"])
        assert not any(p.startswith(r) for c in window for p in _paths(c) for r in reach)
        records.append({
            "incident_id": neg["id"],
            "negative": True,
            "ground_truth_sha": None,
            "ground_truth_subject": f"No commit -- {neg['cause']}",
            "cause": neg["cause"],
            "temptations": temptations,
            "alert": {
                "fingerprint": neg["id"],
                "source": "generic",
                "service": neg["service"],
                "severity": neg["severity"],
                "title": neg["title"],
                "description": neg["description"],
                "started_at": onset.isoformat().replace("+00:00", "Z"),
                "labels": {"alertname": neg["id"], "negative_control": "true"},
                "metrics": neg["metrics"],
            },
        })
        print(f"  {neg['id']}  {neg['service']:16} onset {onset:%b %d %H:%M}  "
              f"{temptations} innocent commits in the window")
    OUT.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"\n{len(records)} negative controls -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
