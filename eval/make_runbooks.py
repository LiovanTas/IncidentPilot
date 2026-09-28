"""Writes the runbook corpus. Kept as a generator so the corpus is versioned as one
reviewable file rather than a dozen near-identical markdown stubs.

    py -3.13 eval/make_runbooks.py
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "runbooks"

RUNBOOKS: dict[str, str] = {}

RUNBOOKS["db-connection-pool-exhaustion"] = """# Database connection pool exhaustion

## Symptoms

- 5xx rate climbs on a service that was healthy minutes ago, with no change in request volume.
- Latency p99 spikes while p50 stays flat: most requests are fine, a minority wait for a connection.
- Logs show `TimeoutError: QueuePool limit of size N overflow M reached`, `could not obtain a connection`,
  or `pool timeout` from the ORM layer.
- Database CPU is *low*. That is the tell: the database is idle because the application cannot reach it.

## Likely causes

1. `pool_size` or `max_overflow` was lowered in a config change or a deploy manifest.
2. Replica count was reduced, so each remaining pod carries more concurrency against the same pool.
3. A slow query started holding connections much longer than before (see `slow-query-and-missing-index`).
4. A connection leak: a code path that opens a session and does not close it on the error branch.

## Diagnosis

- Compare `pool_size * replicas` before and after the suspected change. If the product dropped,
  that is almost certainly the cause.
- Check whether checkout time (`pool.checkout` histogram) rose while query duration stayed flat.
  Pool wait rising with flat query time means starvation, not a slow database.
- Grep the blast-window diffs for `pool_size`, `max_overflow`, `pool_timeout`, `SQLALCHEMY_POOL`,
  `maxPoolSize`, `replicas:`.

## Mitigation

1. Restore the previous pool size and redeploy. This is faster than scaling replicas.
2. If the change cannot be reverted, scale replicas so total connections match the previous product,
  and confirm the database `max_connections` ceiling still has headroom.
3. Only after the error rate recovers, investigate whether the pool reduction was masking a leak.

## Rollback

    git revert --no-edit <sha>

Redeploy and watch the pool checkout histogram; recovery is usually visible within two minutes of
the new pods passing readiness.

## Verification

- Error rate back to baseline within 10 minutes.
- `pool.checkout` p99 under 50ms.
- No `QueuePool limit` lines in the last 5 minutes of logs.
"""

RUNBOOKS["slow-query-and-missing-index"] = """# Slow queries and missing indexes

## Symptoms

- p99 latency rises sharply and stays high; p50 rises modestly. Throughput falls.
- Database CPU and disk read IOPS climb together.
- The slow-query log fills with one statement shape repeated thousands of times.
- Downstream services start timing out against the affected service before it reports errors itself.

## Likely causes

1. A migration dropped or renamed an index that a hot query depended on.
2. A new query shape was introduced that has no supporting index (a new filter column, a new sort).
3. An ORM change turned one query into many (see `orm-n-plus-one`).
4. Data growth crossed the point where a sequential scan stopped being cheap.

## Diagnosis

- `EXPLAIN ANALYZE` the top statement from the slow-query log. `Seq Scan` on a large table with a
  selective filter is the signature.
- Diff any migration that landed in the blast window. Look for `DROP INDEX`, `ALTER TABLE`,
  a renamed column, or a new `CREATE TABLE` without indexes.
- A migration whose *up* step creates an index `CONCURRENTLY` may have failed silently and left the
  index invalid. Check `pg_index.indisvalid`.

## Mitigation

1. Recreate the index concurrently so the rebuild does not lock writes:
   `CREATE INDEX CONCURRENTLY idx_name ON table (column);`
2. If the query is new and unnecessary, feature-flag it off rather than waiting for the index build.
3. Shed load on the affected endpoint if the database is saturated; an index build competes for the
   same IO.

## Rollback

Reverting application code does not undo a migration. Revert the code that issues the query, then
restore the index. Never roll a destructive migration backwards under load.

## Verification

- Slow-query log quiet for 5 minutes.
- p99 within 20% of the pre-incident value.
- `EXPLAIN` shows an index scan for the offending statement.
"""

RUNBOOKS["cache-degradation"] = """# Cache degradation and stampedes

## Symptoms

- Cache hit ratio falls off a cliff, often to near zero, at a deploy boundary.
- Origin service or database load multiplies by 10x or more with unchanged user traffic.
- Latency rises everywhere at once rather than on one endpoint.
- Redis or Memcached connection count and network egress spike.

## Likely causes

1. A TTL was lowered, or set to zero, in config. A TTL of `0` means "do not cache" in most clients.
2. The cache key format changed, invalidating the entire warm set in one deploy.
3. Cache invalidation was made more aggressive (a broadened key prefix on write).
4. The cache client was misconfigured after a dependency upgrade and is silently failing open.

## Diagnosis

- Plot hit ratio against deploy markers. A vertical drop at a deploy is a code cause, not a capacity one.
- Grep the blast-window diffs for `ttl`, `TTL`, `expire`, `cache_key`, `CACHE_VERSION`, `invalidate`.
- A key-format change usually appears as an edit to a single `key()` or `cache_key()` helper.
- Check whether the client is erroring: silent failures show as hit ratio 0 *and* miss count 0.

## Mitigation

1. Restore the previous TTL or key format and redeploy.
2. Warm the cache before removing load shedding, or the origin will take the full stampede on restart.
3. Add jitter to TTLs if many keys expire together; synchronized expiry causes recurring spikes.

## Rollback

    git revert --no-edit <sha>

Expect a delay between deploy and recovery equal to roughly one TTL period while the cache refills.

## Verification

- Hit ratio back within 10% of its pre-incident band.
- Origin request rate back to baseline.
- No stampede on the next natural expiry wave (watch one full TTL period).
"""

RUNBOOKS["retry-storms-and-timeouts"] = """# Retry storms, timeouts and circuit breakers

## Symptoms

- A small upstream blip turns into a sustained outage that outlives the original fault.
- Request volume to a downstream service rises well above what clients actually sent.
- Errors are dominated by `DeadlineExceeded`, `context deadline exceeded`, or `504`.
- Load stays high after the original cause is fixed: the system is now failing on its own retries.

## Likely causes

1. Retry count raised without exponential backoff or jitter.
2. A client timeout lowered below the downstream p99, so healthy-but-slow requests are cancelled and
   retried, multiplying load.
3. Circuit-breaker threshold loosened or removed, so the breaker no longer sheds load.
4. Retries added at more than one layer: 3 retries at two layers is 9 requests, not 3.

## Diagnosis

- Compare the caller's outbound request rate with the caller's inbound rate. A widening ratio is a
  retry amplification signature.
- Grep the blast-window diffs for `max_retries`, `retries`, `backoff`, `timeout`, `deadline`,
  `circuit`, `failure_threshold`.
- Check whether the new timeout sits below the measured p99 of the call it guards. That is the single
  most common misconfiguration in this class.

## Mitigation

1. Restore the previous timeout or retry policy and redeploy the *caller*.
2. If the storm is already running, trip the breaker manually or shed load at the edge to let the
   downstream drain.
3. Re-enable traffic gradually. Restoring 100% at once re-triggers the storm.

## Rollback

    git revert --no-edit <sha>

## Verification

- Outbound/inbound request ratio back to ~1.
- Downstream error rate at baseline with the breaker closed.
- No `deadline exceeded` in the last 5 minutes.
"""

RUNBOOKS["auth-401-spike"] = """# Authentication failure spike

## Symptoms

- Sharp rise in `401` and `403` responses across several services at once.
- Users report being logged out mid-session; support volume rises before monitoring pages.
- The spike may be periodic rather than flat, tracking token lifetime rather than traffic.

## Likely causes

1. Token or session TTL shortened, so sessions expire faster than clients refresh.
2. A signing key or secret rotated without a dual-validation overlap window.
3. Clock skew between the issuer and validators, making valid tokens appear expired or not-yet-valid.
4. A scope or audience claim changed, so previously valid tokens fail validation.

## Diagnosis

- Split 401s by token age. A cliff at a specific age is a TTL change; a uniform distribution across
  ages is a key or claim problem.
- Grep the blast-window diffs for `TOKEN_TTL`, `expires_in`, `session_lifetime`, `JWT_SECRET`,
  `audience`, `issuer`, `leeway`, `clock_skew`.
- Check whether the failure is at issue time or validation time. Only validators failing means the
  issuer and validators disagree about a key or a claim.

## Mitigation

1. Restore the previous TTL or reinstate the old signing key alongside the new one so both validate.
2. Widen clock leeway temporarily if skew is the cause, then fix NTP.
3. Do not force a global session flush; it converts a partial outage into a total one.

## Rollback

    git revert --no-edit <sha>

Sessions issued under the bad config keep failing until they are naturally refreshed; expect a tail.

## Verification

- 401 rate back to baseline and, importantly, flat rather than periodic.
- Token refresh success rate above 99%.
- No validation errors mentioning key id or audience.
"""

RUNBOOKS["feature-flag-rollback"] = """# Feature flag incidents

## Symptoms

- A behaviour change appears in production with no obvious deploy at the same moment, or with a
  deploy that claims to change nothing.
- Impact is often partial: a percentage of users or one cohort.
- Reverting the application deploy does not fix it, because the flag default lives in config.

## Likely causes

1. A flag's default value was flipped in code or in a values file, so every environment picked it up.
2. A rollout percentage was raised past the level the change was tested at.
3. A flag was deleted from the config while the code still reads it, so the client returns its
   built-in default rather than the intended value.

## Diagnosis

- Diff the flag configuration, not just the service code. Flag defaults often live in
  `config/flags.yaml`, `values.yaml`, or a `defaults` map in a shared library.
- Correlate the onset time with flag-change audit logs before assuming a deploy caused it.
- Confirm whether the affected cohort matches the rollout segment.

## Mitigation

1. Set the flag back to its previous value. This is faster than any code deploy and should be the
   first action.
2. Only then decide whether to revert the code that introduced the flag.

## Rollback

Flag-first, code-second. If the flag default was changed in the repository, reverting the commit is
required to make the fix survive the next deploy:

    git revert --no-edit <sha>

## Verification

- Behaviour returns for the affected cohort within one flag-poll interval (typically under 60s).
- Error rate and the business metric that regressed both return to baseline.
"""

RUNBOOKS["dependency-upgrade-failures"] = """# Dependency upgrade failures

## Symptoms

- Errors appear immediately at a deploy that contains no functional change, only a lockfile diff.
- Deserialization, encoding, or type errors in code paths that were not touched.
- A library's default changed silently across a major version: timeouts, retry behaviour, TLS
  verification, JSON encoding of dates or decimals.

## Likely causes

1. A major-version bump with breaking behavioural defaults, merged as a routine dependency update.
2. A transitive dependency resolved to a new version even though the direct pin did not change.
3. A security patch that also changed serialization or validation strictness.

## Diagnosis

- Read the lockfile diff, not the manifest diff. The manifest may show `^1.2.0` unchanged while the
  lockfile moved from `1.2.0` to `2.0.1`.
- Compare the error's stack frames against the upgraded package's changelog for the versions crossed.
- Reproduce locally by pinning to the previous version; a clean reproduction confirms the cause
  faster than reading release notes.

## Mitigation

1. Pin back to the last known-good version, including transitive pins, and redeploy.
2. If the upgrade carried a security fix, pin back only long enough to ship a compatibility shim.

## Rollback

    git revert --no-edit <sha>

Confirm the lockfile actually reverted; a revert that regenerates the lockfile can resolve forward
again.

## Verification

- The specific exception class disappears from logs.
- Contract tests against the dependency pass on the deployed version.
"""

RUNBOOKS["queue-lag-and-worker-saturation"] = """# Queue lag and worker saturation

## Symptoms

- Consumer lag grows monotonically; the graph is a straight ramp rather than a spike.
- Downstream effects are delayed rather than failed: payments settle late, emails arrive late.
- No error-rate change at first. Lag becomes an incident when it crosses a business deadline.

## Likely causes

1. Worker concurrency, replica count, or prefetch was reduced in a config change.
2. Per-message processing time rose (a new external call, a slower query) so the same worker count
   no longer keeps up.
3. Producer volume rose without a matching consumer scale-up.
4. Poison messages causing repeated redelivery, consuming capacity without draining the queue.

## Diagnosis

- Compute required capacity: `producer rate x mean processing time`. Compare with
  `workers x concurrency`. If demand exceeds capacity, it is a scaling problem, not a bug.
- Grep the blast-window diffs for `concurrency`, `replicas`, `prefetch`, `max_workers`,
  `batch_size`, `PARALLELISM`.
- Check the dead-letter queue rate. A rising DLQ alongside lag points at poison messages.

## Mitigation

1. Restore the previous concurrency or replica count. Scale beyond the previous value to work off the
   backlog, then scale back.
2. If processing time regressed, revert the change that added work per message.
3. Divert poison messages to the DLQ aggressively so the main queue drains.

## Rollback

    git revert --no-edit <sha>

## Verification

- Lag curve turns over and trends to zero.
- Oldest-message age back under the SLO.
- DLQ rate at baseline.
"""

RUNBOOKS["oom-and-restart-loops"] = """# OOM kills and restart loops

## Symptoms

- Pods restart repeatedly; `kubectl get pods` shows rising `RESTARTS` and `OOMKilled` in the last
  state.
- Error rate is sawtoothed, tracking the restart cycle rather than being flat.
- Memory graph shows a sawtooth: climb to the limit, drop to zero at kill, repeat.

## Likely causes

1. Memory limit lowered in a deploy manifest without a matching reduction in working set.
2. An unbounded buffer, cache, or batch size introduced in code: a list that accumulates per request,
   a batch size raised from hundreds to hundreds of thousands.
3. A dependency upgrade that increased per-connection or per-request overhead.
4. A genuine leak that was always present but only now crosses the limit.

## Diagnosis

- A sawtooth that begins exactly at a deploy is a code or config cause. A slow ramp over days is a leak.
- Grep the blast-window diffs for `memory:`, `limits`, `batch_size`, `chunk_size`, `buffer`,
  `fetchall`, `read()`, `list(`.
- Compare working-set size before and after. A step change at deploy points at the new allocation.

## Mitigation

1. Raise the memory limit as an immediate stopgap so the service stops flapping, then fix the cause.
2. Revert the change that raised the working set.
3. Reduce batch or buffer sizes to the previous value if a revert is not clean.

## Rollback

    git revert --no-edit <sha>

## Verification

- Zero restarts for 15 minutes.
- Memory plateaus below 80% of the limit rather than climbing to it.
"""

RUNBOOKS["rate-limiting-429s"] = """# Rate limiting and 429 storms

## Symptoms

- Sudden `429 Too Many Requests` at a rate that does not match a traffic increase.
- One client, tenant, or region is disproportionately affected.
- Retries make it worse; see `retry-storms-and-timeouts`.

## Likely causes

1. A rate-limit threshold lowered, or a limit's unit changed (per minute vs per second).
2. The limiter key changed from per-tenant to global, so all tenants share one bucket.
3. A limiter moved behind a load balancer without shared state, so each replica enforces the full
   limit independently, or a shared Redis limiter lost its backing store and fails closed.

## Diagnosis

- Compare the observed allowed rate against the configured limit. An order-of-magnitude mismatch
  usually means a unit or key change.
- Grep the blast-window diffs for `rate_limit`, `burst`, `per_second`, `per_minute`, `quota`,
  `limiter`, `bucket_key`.
- Check whether the limiter fails open or closed when its store is unreachable. Failing closed during
  a Redis blip produces exactly this symptom.

## Mitigation

1. Restore the previous threshold and key, and redeploy.
2. Temporarily raise the limit for the affected tenant if a revert will take longer than a few minutes.
3. Make the limiter fail open for read paths if its backing store is the problem.

## Rollback

    git revert --no-edit <sha>

## Verification

- 429 rate at baseline.
- Allowed request rate matches the configured limit.
"""

RUNBOOKS["payment-duplicate-charges"] = """# Duplicate charges and payment idempotency

## Symptoms

- Support or the finance reconciliation job reports customers charged more than once.
- Ledger entries share an order id but have distinct payment ids.
- Payment provider dashboard shows a retry rate spike shortly before the duplicates.

## Likely causes

1. The idempotency key derivation changed, so a retry of the same logical payment produces a new key
   and the provider treats it as a new charge.
2. Idempotency key scope narrowed (for example, keyed on attempt id instead of order id).
3. Retries introduced or increased on a non-idempotent endpoint.
4. A migration changed the uniqueness constraint that previously caught duplicates at write time.

## Diagnosis

- This is a money-losing failure class. Escalate to the payments owner immediately and in parallel
  with diagnosis.
- Grep the blast-window diffs for `idempotency`, `Idempotency-Key`, `unique`, `order_id`, `attempt`,
  `charge(`, `capture(`.
- Query the ledger for orders with more than one successful charge in the incident window; that count
  is the true impact number, not an estimate.

## Mitigation

1. Stop the bleeding first: disable retries on the charge path, or gate the endpoint behind a flag.
2. Restore the previous idempotency key derivation and redeploy.
3. Produce the exact list of affected orders for refund. Do not refund automatically without the
   payments owner signing off.

## Rollback

    git revert --no-edit <sha>

## Verification

- No new orders with multiple successful charges for 30 minutes.
- Reconciliation job runs clean.
- Refund list handed to the payments owner with order ids and amounts.
"""

RUNBOOKS["checkout-5xx-triage"] = """# Checkout 5xx: first-response triage

## Symptoms

- Elevated 5xx on `checkout-api` or `web-frontend` checkout routes.
- Conversion rate drops within minutes; this is the highest revenue-impact alert class in the platform.

## First 5 minutes

1. Confirm the blast radius. `checkout-api` depends on `cart-service`, `payments-worker`,
   `auth-gateway` and `inventory-api`. Check whether the failure is local or inherited from one of
   those before investigating checkout code.
2. Check the deploy timeline for every service in that dependency set, not just checkout. The most
   common cause of a checkout incident is a change in something checkout calls.
3. Check shared libraries. A change under `libs/` or `packages/common/` ships to every service and
   will not appear in a service-scoped commit search.

## Escalation

- Page `#team-checkout` for anything sustained over 5 minutes.
- Page `#team-payments` in parallel if payment error codes appear; see `payment-duplicate-charges`.
- Declare a sev1 if the checkout error rate exceeds 10% for more than 5 minutes.

## Common causes, in observed frequency order

1. A dependency service degraded (see its own runbook).
2. A shared library change: serialization, HTTP client defaults, retry policy.
3. A database change: pool size, migration, missing index.
4. A config or flag change with a wrong default.

## Verification

- Checkout error rate under 0.5% for 10 minutes.
- Conversion rate recovered to the pre-incident band.
- Ledger reconciliation clean for the incident window.
"""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in RUNBOOKS.items():
        path = OUT / f"{name}.md"
        path.write_text(body, encoding="utf-8")
    print(f"wrote {len(RUNBOOKS)} runbooks to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
