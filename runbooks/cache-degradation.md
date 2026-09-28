# Cache degradation and stampedes

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
