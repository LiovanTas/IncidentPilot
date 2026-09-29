"""Live demo: introduce a brand-new bug into the test repo and alert on it.

The 20 replay incidents are a fixed benchmark, which invites the fair objection that the
system might be tuned to them. This script does what you would do by hand: commit a fresh
breaking change that appears in no scenario, commit two innocent changes alongside it, and
emit the alert a monitoring system would fire. Nothing here is referenced by the eval.

    py -3.13 eval/demo_new_bug.py          # add the commits + write the alert
    py -3.13 eval/demo_new_bug.py --reset  # undo, restoring the repo to the benchmark state

Then diagnose it:

    py -3.13 -m incidentpilot.cli replay eval/demo_alert.json --no-agent   # ranker only
    py -3.13 -m incidentpilot.cli replay eval/demo_alert.json              # full agent
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / "eval" / "fixtures" / "repo"
ALERT = ROOT / "eval" / "demo_alert.json"
TAG = "benchmark-baseline"

ONSET = datetime(2026, 9, 29, 14, 20, tzinfo=timezone.utc)

# The real cause. `FAIL_OPEN` decides what the cache client does when Redis is unreachable:
# True falls through to the origin, False turns a cache blip into a user-facing 500. The
# commit message sounds like a correctness improvement, and the diff is one word.
CAUSE = {
    "at": ONSET - timedelta(hours=2, minutes=40),
    "author": "Priya Raghavan",
    "message": (
        "fix: fail closed when the cache backend is unreachable\n\n"
        "Serving on a cache miss during a Redis partition can return stale authorization\n"
        "state. Failing closed is the safer default until we have a proper fallback path."
    ),
    "edits": [("libs/cache.py", "FAIL_OPEN = True", "FAIL_OPEN = False")],
}

# Both land closer to the alert, both are inside the service that alerted, and both sound
# like plausible causes of a 5xx spike. Neither can actually produce this symptom.
DECOYS = [
    {
        "at": ONSET - timedelta(minutes=35),
        "author": "Marcus Bell",
        "message": (
            "perf: fail fast on cart pool checkout\n\n"
            "Waiting 5s for a connection just queues requests behind a slow database."
        ),
        "edits": [("services/cart-service/config.py",
                   "DB_POOL_TIMEOUT_S = 5", "DB_POOL_TIMEOUT_S = 2")],
    },
    {
        "at": ONSET - timedelta(hours=1, minutes=10),
        "author": "Wen Li",
        "message": "chore: scale cart-service up for the weekend promotion",
        "edits": [("deploy/cart-service/values.yaml", "replicas: 6", "replicas: 8")],
    },
]

ALERT_PAYLOAD = {
    "source": "generic",
    "fingerprint": "DEMO-NEW-BUG",
    "service": "cart-service",
    "severity": "sev1",
    "title": "cart-service 5xx rate 23% during Redis maintenance window",
    "description": (
        "GET /cart started returning 500 at 14:20, coinciding with a scheduled Redis "
        "failover. Logs show 'cache backend unreachable' immediately followed by a 500 for "
        "the same request id. The notable part is that origin database load did NOT rise "
        "during the window -- requests are not falling through to the database, they are "
        "just failing. Previous Redis failovers this quarter caused a latency bump and no "
        "errors at all."
    ),
    "started_at": ONSET.isoformat().replace("+00:00", "Z"),
    "labels": {"alertname": "CartHighErrorRate", "endpoint": "/cart", "team": "checkout"},
    "metrics": {"error_rate_before": 0.002, "error_rate_after": 0.23, "duration_minutes": 18},
}


def git(*args: str, env_extra: dict | None = None) -> str:
    import os

    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(REPO)}
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                          env=env, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip()


def apply(spec: dict) -> None:
    for rel, find, replace in spec["edits"]:
        path = REPO / rel
        text = path.read_text(encoding="utf-8")
        if find not in text:
            raise SystemExit(
                f"anchor {find!r} not found in {rel}.\n"
                f"The repo is not in its benchmark state -- rebuild it with:\n"
                f"  py -3.13 eval/build_fixture_repo.py"
            )
        path.write_text(text.replace(find, replace, 1), encoding="utf-8", newline="\n")


def commit(spec: dict) -> str:
    stamp = spec["at"].isoformat()
    email = spec["author"].lower().replace(" ", ".") + "@checkout-platform.dev"
    env = {"GIT_AUTHOR_NAME": spec["author"], "GIT_AUTHOR_EMAIL": email,
           "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_NAME": spec["author"],
           "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": stamp}
    git("add", "-A", env_extra=env)
    git("commit", "-q", "--no-gpg-sign", "-m", spec["message"], env_extra=env)
    return git("rev-parse", "HEAD", env_extra=env)


def reset() -> int:
    try:
        git("rev-parse", "--verify", TAG)
    except RuntimeError:
        print(f"No '{TAG}' tag found -- nothing to reset. "
              f"Rebuild with: py -3.13 eval/build_fixture_repo.py")
        return 1
    git("reset", "--hard", TAG)
    git("tag", "-d", TAG)
    ALERT.unlink(missing_ok=True)
    print(f"Repo restored to the benchmark state ({git('rev-list', '--count', 'HEAD')} commits). "
          f"Demo alert removed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="undo the demo and restore the benchmark repo")
    args = parser.parse_args()

    if not (REPO / ".git").exists():
        print("Test repo missing. Build it first: py -3.13 eval/build_fixture_repo.py",
              file=sys.stderr)
        return 1
    if args.reset:
        return reset()

    try:
        git("rev-parse", "--verify", TAG)
        print(f"The demo is already applied. Run with --reset first to start clean.",
              file=sys.stderr)
        return 1
    except RuntimeError:
        pass

    git("tag", TAG)  # so --reset can get back exactly here

    specs = sorted([CAUSE, *DECOYS], key=lambda s: s["at"])
    cause_sha = ""
    for spec in specs:
        apply(spec)
        sha = commit(spec)
        role = "THE BUG " if spec is CAUSE else "innocent"
        if spec is CAUSE:
            cause_sha = sha
        print(f"  [{role}] {sha[:10]}  {spec['at'].strftime('%b %d %H:%M')}  "
              f"{spec['message'].splitlines()[0]}")

    ALERT.write_text(json.dumps(ALERT_PAYLOAD, indent=2), encoding="utf-8")

    print(f"\n3 new commits added to {REPO}")
    print(f"Alert written to {ALERT}")
    print(f"\nThe answer (do not tell the tool): {cause_sha[:10]}")
    print("\nNow ask the tool:")
    print("  py -3.13 -m incidentpilot.cli replay eval/demo_alert.json --no-agent   # ranker only")
    print("  py -3.13 -m incidentpilot.cli replay eval/demo_alert.json              # full agent")
    print("\nUndo with: py -3.13 eval/demo_new_bug.py --reset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
