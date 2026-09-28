# Dependency upgrade failures

## Symptoms

- Errors appear immediately at a deploy that contains no functional change, only a lockfile diff.
- Deserialization, encoding, or type errors in code paths that were not touched.
- A library's default changed silently across a major version: timeouts, retry behaviour, TLS
  verification, JSON encoding of dates or decimals.

## Likely causes

1. A major-version bump with breaking behavioural defaults, merged as a routine dependency update.
2. A transitive dependency resolved to a new version even though the direct pin did not change.
3. A security patch that also changed serialization or validation strictness.

## Diagnosis

- Read the lockfile diff, not the manifest diff. The manifest may show `^1.2.0` unchanged while the
  lockfile moved from `1.2.0` to `2.0.1`.
- Compare the error's stack frames against the upgraded package's changelog for the versions crossed.
- Reproduce locally by pinning to the previous version; a clean reproduction confirms the cause
  faster than reading release notes.

## Mitigation

1. Pin back to the last known-good version, including transitive pins, and redeploy.
2. If the upgrade carried a security fix, pin back only long enough to ship a compatibility shim.

## Rollback

    git revert --no-edit <sha>

Confirm the lockfile actually reverted; a revert that regenerates the lockfile can resolve forward
again.

## Verification

- The specific exception class disappears from logs.
- Contract tests against the dependency pass on the deployed version.
