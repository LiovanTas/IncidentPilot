# Feature flag incidents

## Symptoms

- A behaviour change appears in production with no obvious deploy at the same moment, or with a
  deploy that claims to change nothing.
- Impact is often partial: a percentage of users or one cohort.
- Reverting the application deploy does not fix it, because the flag default lives in config.

## Likely causes

1. A flag's default value was flipped in code or in a values file, so every environment picked it up.
2. A rollout percentage was raised past the level the change was tested at.
3. A flag was deleted from the config while the code still reads it, so the client returns its
   built-in default rather than the intended value.

## Diagnosis

- Diff the flag configuration, not just the service code. Flag defaults often live in
  `config/flags.yaml`, `values.yaml`, or a `defaults` map in a shared library.
- Correlate the onset time with flag-change audit logs before assuming a deploy caused it.
- Confirm whether the affected cohort matches the rollout segment.

## Mitigation

1. Set the flag back to its previous value. This is faster than any code deploy and should be the
   first action.
2. Only then decide whether to revert the code that introduced the flag.

## Rollback

Flag-first, code-second. If the flag default was changed in the repository, reverting the commit is
required to make the fix survive the next deploy:

    git revert --no-edit <sha>

## Verification

- Behaviour returns for the affected cohort within one flag-poll interval (typically under 60s).
- Error rate and the business metric that regressed both return to baseline.
