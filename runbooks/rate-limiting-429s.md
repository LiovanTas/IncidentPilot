# Rate limiting and 429 storms

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
