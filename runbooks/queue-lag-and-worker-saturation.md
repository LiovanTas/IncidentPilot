# Queue lag and worker saturation

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
