# 01-baseline

**Variant:** `baseline` -> `{}`
**Target:** n/a (baseline)

## Pre-registered hypothesis

Production defaults: claude-sonnet-5 with conversation-history caching. Every other variant is judged against it, and its repeated runs set the noise band. NOT the configuration that scored 18/20 -- that was claude-opus-5 without history caching; see the opus-5 variant.

## Pre-registered risk

n/a

## Result

- repeat 1: dev 12/12, test 8/8, wrong-and-unflagged 0, $0.94
- repeat 2: dev 12/12, test 8/8, wrong-and-unflagged 0, $0.88

## Dev-split misses (input for the next iteration)

None.

_Test split (8 incidents): aggregate only, by design._
