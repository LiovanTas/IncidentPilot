# Duplicate charges and payment idempotency

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
