# Checkout 5xx: first-response triage

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
