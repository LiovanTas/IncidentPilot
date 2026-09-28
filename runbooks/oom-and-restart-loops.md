# OOM kills and restart loops

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
