# MILESTONE — PR #1 — Hermes gateway skill synchronization hardening is review-ready

**When:** 2026-09-04 06:49 EDT · **Version:** N/A · **Packet/board:** itinerary reliability / Hermes control plane · **Security:** S2 — filesystem trust boundary

## Outcome and non-goals

Draft fork PR [#1](https://github.com/CWaggs2025/hermes-agent/pull/1) contains the reviewed gateway and external-catalog hardening. It removes blocking catalog work from gateway readiness and prevents gateway consumers from escaping guarded local snapshots. This milestone does not merge, install, restart, deploy, touch production, traverse SMB, or open the upstream NousResearch PR.

## Changed files and intent

- `gateway/run.py`, `hermes_cli/main.py`, and `hermes_cli/config_defaults.py` separate readiness and lifecycle commands from optional external-catalog refresh.
- `agent/skill_utils.py`, `agent/skill_commands.py`, `hermes_cli/commands.py`, and the four affected `tools/` modules preserve guarded snapshot identity and leases through every consumer.
- Eight focused test modules cover startup, timeout/kill, last-known-good, no-follow traversal, generation invalidation, post-yield substitution, mutation denial, and sandbox-export omission.
- English and Chinese skills documentation describe the local-snapshot boundary. `NEXT.md` and this PR-numbered relay are organization-only fork material.

## Decisions and preserved invariants

- The always-on gateway never traverses an SMB external root; it reads only a validated local materialization.
- Readiness starts adapters, housekeeping, scheduler, and health reporting before optional catalog work.
- External scans are single-flight, bounded, killable after 10 seconds, symlink-safe, backed off after failure, and cannot replace last-known-good data with an empty catalog.
- Snapshot consumers retain their lease and identity guard through final file use or fail closed.
- `origin` remains `NousResearch/hermes-agent`; `fork` remains the user-owned reviewed deployment source.

## Security, privacy, and authorization evidence

No credentials, installed skills, live services, catalog roots, private traveler data, or production state were read or changed. Chris authorized pre-merge gates, reviewed commits, fork publication, and review requests only. Merge, installation, restart, deployment, upstream PR creation, and production/catalog mutation remain unauthorized.

## Verification

| Gate | Command/evidence | Result |
|---|---|---|
| Changed and adjacent tests | Repository `scripts/run_tests.sh` wrapper over the 11 recorded suites at implementation tip `04d6ff4bbcd8216550e7cec9e8c82c145a44361b` | PASS — 493 passed, 1 skipped |
| Static and syntax checks | Ruff, Python 3.11 compile, and `git diff --check` | PASS |
| Independent review | Two source/test reviews of the implementation candidate; later exact-head review found documentation/provenance corrections only | PASS for implementation; corrected organization-only head requires external readback |
| Frozen implementation identity | Full base-to-implementation diff excluding the five organization-only pointer/relay files: SHA-256 `4bf444d06c91ff8a27839c9899d080dfb13f8c4d9b663672be0d5c29929b3d4f` | PASS |
| Fork PR readback | Public GitHub API and advertised refs confirmed Draft PR #1 against base and merge-base `63279301bcbdc185c1b07b98a9312eb0c862f26d`; the exact overall head must be read externally after each organization-only update; no checks reported | PASS for read-only publication truth; not CI or authenticated mutation proof |
| Tool health | Public GitHub API and advertised refs available; configured `gh` authentication invalid; CodeGraph exact worktree uninitialized; GrepAI narrow query hit a stale unrelated index; Context7 unavailable; Calendar and Playwright not applicable | DEGRADED, with exact Git/test evidence authoritative |

## Review and Chris acceptance

Chris accepted publication of the reviewed candidate as a review-only fork PR. Chris has not accepted or authorized a merge, installation, restart, deployment, production change, or upstream submission. A fresh exact-head review is still required after this documentation-only relay lands.

## PR, merge, ancestry, CI, and deployment truth

PR #1 is open, Draft, and was reported mergeable against fork `main@63279301bcbdc185c1b07b98a9312eb0c862f26d`. The reviewed implementation ends at `04d6ff4bbcd8216550e7cec9e8c82c145a44361b`; every later commit must change only the five authorized organization-only pointer/relay paths. GitHub reports no status checks; that is absence of CI, not a green build. No merge commit, deployment, installation, restart, or production ancestry claim exists.

## Known gaps and owner-only actions

- Windows junction behavior is mock-covered, not live-validated on NTFS.
- Guarded snapshot consumption intentionally fails closed outside the supported CPython/POSIX path primitives; guarded external assets/scripts are not sandbox-exported.
- A reviewer must read back the post-relay PR head and confirm the relay-only delta plus unchanged implementation identity.
- Chris alone decides whether to authorize a later fork merge, upstream PR, installation, restart, or deployment.
