# KICKOFF — next milestone — review Hermes fork PR #1 at its exact post-relay head

Paste the block below into a fresh Codex CLI task.

Read, in order:
1. `AGENTS.md`
2. `planning/OVERHAUL-PROGRESS.md` — absent in this repository; verify the absence and continue
3. `HANDOFF-LATEST.md` and its dated handoff
4. this kickoff
5. `NEXT.md` and only the gateway/external-catalog files named by the PR diff

TOOL HEALTH BEFORE WORK:
- CodeGraph must show nonzero files/nodes and answer one representative query; record DEGRADED if this worktree remains uninitialized.
- GrepAI must answer one narrow query from an exact Hermes workspace; reject unrelated-index results.
- Record configured `gh` authentication as degraded until proven otherwise; public GitHub API and advertised refs may support read-only PR evidence, but never mutation authority.
- Calendar is required only if schedule reconciliation enters scope.
- Use Context7 only for current external-library/API facts; none is currently required.
- Playwright is not required because this packet has no UI change.
- Report any degraded tool and use exact Git/source/test evidence as the bounded fallback.

STATELESS CHECKS:
- `pwd`, `git status --short`, branch, worktree list, and active claims
- PR #1 head/base/Draft/merge state/status checks, fork refs, merge-base, changed paths, and diff hygiene

PACKET:
- Goal: independently review the exact post-relay head of Draft fork PR #1.
- Acceptance gate: implementation identity unchanged; relay-only delta correct; no P0/P1/P2 finding; exact head/base/path receipt.
- Security/data boundary: no external-root traversal, credentials, private payloads, installed skills, services, or production access.
- In scope: read-only Git/GitHub review and targeted local tests needed to resolve a concrete finding.
- Out of scope: edits, commit, push, PR mutation, upstream PR, ready transition, merge, installation, restart, deployment, production/catalog mutation, and cleanup.
- Repository/branch/worktree/file claim: `CWaggs2025/hermes-agent`; `codex/gateway-skill-sync-readiness-20260903`; `/Users/admin/Documents/ChatGPT/Hermes Agent Gateway Skill Sync`; no write claim for a read-only review.

DISCOVER:
- Use CodeGraph context/impact first when initialized.
- Use GrepAI only for narrow unresolved semantic questions from an exact Hermes index.
- Use exact `rg`, file reads, and Git diff confirmation where indexed tools are degraded.

PROVE:
- Reconcile the PR diff against base `63279301bcbdc185c1b07b98a9312eb0c862f26d` and implementation tip `04d6ff4bbcd8216550e7cec9e8c82c145a44361b`.
- Confirm every commit after `04d6ff4bbcd8216550e7cec9e8c82c145a44361b` changes only the five authorized organization-only pointer/relay paths.
- Recheck the full base-to-implementation diff SHA-256 `4bf444d06c91ff8a27839c9899d080dfb13f8c4d9b663672be0d5c29929b3d4f` after excluding those five paths, and inspect the adversarial timeout/no-follow/lease tests. If useful, verify `34e5c0c7d6fb9971f7f46a53b3e185f21b4dbe677ba116dd73f318af52ea379f` only as the corrective `76f79f` to `04d6ff4` delta excluding `NEXT.md`.
- Treat the empty GitHub status-check rollup as no CI, never as green CI.

STOP:
- unexplained dirty file or ownership conflict
- PR head/base/path drift or stale pointer truth
- broken required GitHub access
- security boundary failure or implementation identity change
- scope drift or completion of the named exact-head review gate

RETURN AND CLOSEOUT:
- exact PR head/base/path receipt and review findings
- commands/results, tool-health state, and security status
- unresolved issue and first safe next action
- automatically use `$cwt-close-milestone` at checkpoint, review-ready PR, merge completion, interruption, or high context pressure

No commit, push, PR mutation, merge, deploy, NAS/Sally/production action, installation, restart, or calendar reschedule without Chris's explicit authorization.
