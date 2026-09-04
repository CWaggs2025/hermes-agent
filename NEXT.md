# NEXT — hermes-agent · verified 2026-09-04

## State

- This candidate is based on upstream `NousResearch/hermes-agent@63279301bcbdc185c1b07b98a9312eb0c862f26d`; `origin` remains upstream and `fork` targets `CWaggs2025/hermes-agent` — verified 2026-09-04 via remotes, advertised upstream main, and HEAD.
- Gateway lifecycle/status commands no longer perform implicit skill synchronization, and gateway readiness starts adapters, housekeeping, scheduler, and health reporting before any optional local catalog refresh — verified 2026-09-04 by startup/restart and CLI service tests.
- External catalog refresh runs single-flight in a killable subprocess with a 10-second timeout, terminate/kill escalation, bounded traversal, no followed directory symlinks, backoff, atomic snapshots, and last-known-good preservation; gateway callers cannot traverse external roots — verified 2026-09-04 by adversarial catalog tests.
- Scanner cache reads, writes, locks, materialization, publication, and cleanup now use no-follow descriptor descent from `HERMES_HOME` or fail closed; gateway snapshot reads preserve their lease and identity guard through derived paths and final file opens on Python 3.11, while Python 3.12/3.13 and sandbox path exports fail closed — verified 2026-09-04 by cache/junction substitution, post-yield swap, stale-identifier, path-feature, and export-omission regressions.
- Changed and adjacent suites pass `493 passed, 1 skipped`; Ruff, Python 3.11 compile checks, and patch hygiene pass; two independent reviews found no remaining implementation blocker. The full base-to-implementation diff, excluding organization-only pointer/relay files, has SHA-256 `4bf444d06c91ff8a27839c9899d080dfb13f8c4d9b663672be0d5c29929b3d4f` — verified 2026-09-04 in the dedicated repository environment and exact-diff reviews.
- The reviewed implementation is committed through `04d6ff4bbcd8216550e7cec9e8c82c145a44361b`; every later commit must change only `NEXT.md` and the four organization-only pointer/relay files. Draft PR [#1](https://github.com/CWaggs2025/hermes-agent/pull/1) targets `CWaggs2025/hermes-agent` `main@63279301bcbdc185c1b07b98a9312eb0c862f26d`; public GitHub API and advertised refs passed for read-only state, while configured `gh` authentication is degraded and supplies no authenticated mutation proof. No merge, installation, gateway restart, deployment, or production mutation has occurred — verified 2026-09-04 via local/remote refs and public GitHub readback.

## Do next (max 5, ordered)

1. Obtain or reconfirm an external exact-head review after any branch update — reviewer · small · blocked by: nothing.
2. Decide whether to authorize a fork merge after an exact-head PASS — Chris · small · blocked by: fresh review and separate merge authority.
3. Submit a separately bounded upstream review request without organization-only relay material — Codex · small · blocked by: upstream-PR authorization.
4. Install the versioned fork only during the separately authorized control-plane cutover — Chris/ops · medium · blocked by: canaries and deployment authority.

## Explicitly ignoring

Direct SMB traversal in the always-on gateway, automatic skill loading/synchronization, unrelated Hermes features, credentials, installation, service restart, deployment, and production catalog mutation.

## Single next action

Read back the current Draft PR head/base and complete exact-head review before requesting a separate fork-merge decision.
