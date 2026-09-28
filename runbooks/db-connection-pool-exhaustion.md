# Database connection pool exhaustion

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
