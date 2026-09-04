# NEXT — hermes-agent · verified 2026-09-04

## State

- This candidate is based on upstream `NousResearch/hermes-agent@63279301bcbdc185c1b07b98a9312eb0c862f26d`; `origin` remains upstream and `fork` targets `CWaggs2025/hermes-agent` — verified 2026-09-04 via remotes, advertised upstream main, and HEAD.
- Gateway lifecycle/status commands no longer perform implicit skill synchronization, and gateway readiness starts adapters, housekeeping, scheduler, and health reporting before any optional local catalog refresh — verified 2026-09-04 by startup/restart and CLI service tests.
- External catalog refresh runs single-flight in a killable subprocess with a 10-second timeout, terminate/kill escalation, bounded traversal, no followed directory symlinks, backoff, atomic snapshots, and last-known-good preservation; gateway callers cannot traverse external roots — verified 2026-09-04 by adversarial catalog tests.
- Focused hardening tests pass `188 passed, 1 skipped`; adjacent CLI/gateway suites, Ruff, compile checks, Windows compatibility, and patch hygiene pass — verified 2026-09-04 in the dedicated Python 3.11 environment.
- The candidate is committed locally and clean; no branch push, fork/upstream PR, installation, gateway deployment, or production mutation has occurred — verified 2026-09-04 via git state, advertised refs, and the session ledger.

## Do next (max 5, ordered)

1. Obtain a fresh independent exact-head review of the frozen candidate — reviewer · small · blocked by: reviewer availability.
2. Push the reviewed branch to `CWaggs2025/hermes-agent` and open the upstream review request — Codex · small · blocked by: item 1 and confirmed fork access.
3. Install the versioned fork only during the separately authorized control-plane cutover — Chris/ops · medium · blocked by: review, canaries, and deployment authority.

## Explicitly ignoring

Direct SMB traversal in the always-on gateway, automatic skill loading/synchronization, unrelated Hermes features, credentials, installation, service restart, deployment, and production catalog mutation.

## Single next action

Obtain the independent exact-head verdict before publication.
