# Retry storms, timeouts and circuit breakers

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
