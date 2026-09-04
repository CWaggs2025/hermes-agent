# NEXT — hermes-agent · verified 2026-09-04

## State

- This candidate is based on upstream `NousResearch/hermes-agent@63279301bcbdc185c1b07b98a9312eb0c862f26d`; `origin` remains upstream and `fork` targets `CWaggs2025/hermes-agent` — verified 2026-09-04 via remotes, advertised upstream main, and HEAD.
- Gateway lifecycle/status commands no longer perform implicit skill synchronization, and gateway readiness starts adapters, housekeeping, scheduler, and health reporting before any optional local catalog refresh — verified 2026-09-04 by startup/restart and CLI service tests.
- External catalog refresh runs single-flight in a killable subprocess with a 10-second timeout, terminate/kill escalation, bounded traversal, no followed directory symlinks, backoff, atomic snapshots, and last-known-good preservation; gateway callers cannot traverse external roots — verified 2026-09-04 by adversarial catalog tests.
- Scanner cache reads, writes, locks, materialization, publication, and cleanup now use no-follow descriptor descent from `HERMES_HOME` or fail closed; gateway snapshot reads preserve their lease and identity guard through derived paths and final file opens on Python 3.11, while Python 3.12/3.13 and sandbox path exports fail closed — verified 2026-09-04 by cache/junction substitution, post-yield swap, stale-identifier, path-feature, and export-omission regressions.
- Changed and adjacent suites pass `493 passed, 1 skipped`; Ruff, Python 3.11 compile checks, and patch hygiene pass; two independent reviews found no remaining release blocker in the source/test/documentation diff (excluding this live pointer), SHA-256 `34e5c0c7d6fb9971f7f46a53b3e185f21b4dbe677ba116dd73f318af52ea379f` — verified 2026-09-04 in the dedicated repository environment and exact-diff reviews.
- The reviewed implementation is committed through `04d6ff4bbcd8216550e7cec9e8c82c145a44361b`; Draft PR [#1](https://github.com/CWaggs2025/hermes-agent/pull/1) tracks the dedicated fork branch against `CWaggs2025/hermes-agent` `main@63279301bcbdc185c1b07b98a9312eb0c862f26d`. Its opening pointer head was `3c8a14a0149ba7fcacabd30f6d40d0877c7e7b8b`; no merge, installation, gateway restart, deployment, or production mutation has occurred — verified 2026-09-04 via local/remote refs and authenticated GitHub readback.

## Do next (max 5, ordered)

1. Perform a fresh read-only review of Draft PR #1 at its exact post-relay head — reviewer · small · blocked by: documentation-only relay push.
2. Decide whether to authorize a fork merge after an exact-head PASS — Chris · small · blocked by: fresh review and separate merge authority.
3. Submit a separately bounded upstream review request without organization-only relay material — Codex · small · blocked by: upstream-PR authorization.
4. Install the versioned fork only during the separately authorized control-plane cutover — Chris/ops · medium · blocked by: canaries and deployment authority.

## Explicitly ignoring

Direct SMB traversal in the always-on gateway, automatic skill loading/synchronization, unrelated Hermes features, credentials, installation, service restart, deployment, and production catalog mutation.

## Single next action

Push the PR #1 documentation-only relay, then re-read the exact PR head and request a fresh read-only review.
