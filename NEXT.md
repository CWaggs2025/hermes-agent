# NEXT — hermes-agent · verified 2026-09-04

## State

- This candidate is based on upstream `NousResearch/hermes-agent@63279301bcbdc185c1b07b98a9312eb0c862f26d`; `origin` remains upstream and `fork` targets `CWaggs2025/hermes-agent` — verified 2026-09-04 via remotes, advertised upstream main, and HEAD.
- Gateway lifecycle/status commands no longer perform implicit skill synchronization, and gateway readiness starts adapters, housekeeping, scheduler, and health reporting before any optional local catalog refresh — verified 2026-09-04 by startup/restart and CLI service tests.
- External catalog refresh runs single-flight in a killable subprocess with a 10-second timeout, terminate/kill escalation, bounded traversal, no followed directory symlinks, backoff, atomic snapshots, and last-known-good preservation; gateway callers cannot traverse external roots — verified 2026-09-04 by adversarial catalog tests.
- Scanner cache reads, writes, locks, materialization, publication, and cleanup now use no-follow descriptor descent from `HERMES_HOME` or fail closed; gateway snapshot reads preserve their lease and identity guard through derived paths and final file opens on Python 3.11, while Python 3.12/3.13 and sandbox path exports fail closed — verified 2026-09-04 by cache/junction substitution, post-yield swap, stale-identifier, path-feature, and export-omission regressions.
- Changed and adjacent suites pass `493 passed, 1 skipped`; Ruff, Python 3.11 compile checks, and patch hygiene pass; two independent reviews found no remaining release blocker in the source/test/documentation diff (excluding this live pointer), SHA-256 `34e5c0c7d6fb9971f7f46a53b3e185f21b4dbe677ba116dd73f318af52ea379f` — verified 2026-09-04 in the dedicated repository environment and exact-diff reviews.
- The reviewed implementation is committed through `04d6ff4bbcd8216550e7cec9e8c82c145a44361b`; the dedicated branch is clean and advertised by `CWaggs2025/hermes-agent`, while no PR, merge, installation, gateway deployment, or production mutation has occurred — verified 2026-09-04 via `git rev-parse`, `git status --short`, `git ls-remote`, and authenticated GitHub PR lookup.

## Do next (max 5, ordered)

1. Open a review-only pull request from the advertised candidate branch to `CWaggs2025/hermes-agent` `main` — Codex · small · blocked by: nothing; PR creation is authorized, while merge, installation, and deployment remain unauthorized.
2. Submit a separately bounded upstream review request only after the fork PR is recorded — Codex · small · blocked by: upstream-PR authorization and removal of organization-only pointer material from the upstream diff.
3. Install the versioned fork only during the separately authorized control-plane cutover — Chris/ops · medium · blocked by: canaries and deployment authority.

## Explicitly ignoring

Direct SMB traversal in the always-on gateway, automatic skill loading/synchronization, unrelated Hermes features, credentials, installation, service restart, deployment, and production catalog mutation.

## Single next action

Open the authorized review-only fork PR without changing the reviewed implementation.
