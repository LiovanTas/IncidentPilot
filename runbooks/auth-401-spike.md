# Authentication failure spike

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
