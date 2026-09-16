# Orchestration pipeline

The process this harness runs. The specifics (branch roles, state graph, CI
checks/categories, commands, approvals, adapters, ticket system) come from
`.orchestration/config.yaml`; the rules being enforced come from this repo's
`CLAUDE.md` / `AGENTS.md`. This file is process guidance, not project knowledge.

## The unit of work

**One ticket = one agent = one worktree = one branch = one PR.** Agents never
build on each other's unmerged branches. Branch source and destination roles are
resolved from config. In legacy configs, feature branches are cut from the
legacy configured source branch and target the legacy configured target branch.
No stacked PRs unless the configured workflow explicitly allows them.

## The pipeline

```
scope/matrix -> design* -> implement -> code-review -> security-review -> verify -> merge
               (fresh)    (worktree)  (fresh agent)   (fresh agent)      (opt.)
```

1. **Pre-implementation.** Before a branch is cut, produce an adversarial test
   matrix with falsifying assertions for every relevant syntax form, boundary,
   inspection failure, partial state, and cleanup/recovery path. Security-sensitive
   infrastructure additionally requires a fresh `orchestration-design-reviewer`
   PASS that defines trust boundaries and impossible guarantees and rejects
   fragile designs before code exists.

2. **Implement** (`orchestration-implementer`). Isolated worktree. TDD
   red-green. Runs the repo's pre-commit self-checks. Opens a PR to the
   configured target branch. Returns a structured report ending in a click-path.

3. **Code review** (`orchestration-code-reviewer`). A FRESH agent with no
   implementer context. Re-derives correctness from the ticket + diff, runs the
   repo's review skill, re-runs the self-checks itself, audits against the
   repo's standards. Ends with `VERDICT: PASS` or `VERDICT: FAIL`.

4. **Security review** (`orchestration-security-reviewer`). Another fresh agent.
   Runs when the shared security-gate decision matches a
   `security_required_when` diff trigger, a
   `security_required_source_branches` pattern, or a
   `security_required_target_branches` pattern. Hunts for leaks / privilege
   escalation / isolation breaks. Ends with `VERDICT: PASS` / `FAIL`.

5. **Verify (optional).** For each `verification:` entry whose `when:` matches
   the target, `run-verification.sh <name>` runs the suite on the rebased branch
   and writes a sha-stamped result file (RED = no file = no merge). If the change
   has a user-visible surface, the `orchestration-visual-qa` agent captures the
   click-path headlessly and compares it against the acceptance criteria. Both
   end in a verdict the orchestrator branches on.

6. **Merge on green.** Only after every gate PASSES, every required verification
   is GREEN, **and** the configured target CI checks are green. The orchestrator
   records the marker (`merge-guard.sh --record-green`, validated against a
   result file when one exists), then merges via `merge-on-green.sh`. The
   merge script mechanically validates the active plugin version, recorded
   all-green marker, and exact PR head/base identity. A trusted merge-guard hook
   additionally blocks raw `gh pr merge` commands when the host supports it.

## The non-negotiables (why this beats "just run CI")

- **CI-green is necessary, not sufficient.** Independent reviews are mandatory.
  CI doesn't catch cross-surface inconsistency, privacy leaks, or a test that
  only mirrors the implementation. The reviewer is a *different* agent than the
  author, on purpose.
- **A finding is reproduced, not trusted.** "tsc clean / tests green" from the
  author is a claim; the gate re-runs it. A "stale" or flaky test is treated as
  a real signal until proven otherwise -- it has more than once been a real bug.
- **The worker threat model is explicit.** `worker_trust_profile` defaults to
  `cooperative-worker`: protect against mistakes, crashes, loops, duplicate work,
  and accidental misuse without pretending same-UID code is adversarially
  isolated. `isolated-worker` requires a separately owned UID/container and
  credential boundary. This setting never weakens application or tenant security.
- **Architecture feasibility precedes code.** If an invariant needs a root-owned
  installation, daemon, container, cloud resource, or rollout outside the ticket,
  split or defer it before implementation. A repository-local approximation is
  not a repair.
- **The orchestrator is a lossy relay.** Summarizing agent reports into briefs
  into docs drops the uncertainty marker at each hop. So: never put a `file:line`
  or an unrun code snippet in a brief -- hand over a grep target and let the
  implementer write and verify the code. Label every relayed claim (`verified now`
  / `reported, unverified`). Re-derive any fact before it enters a durable artifact
  (CLAUDE.md, ticket, PR body), and treat anything learned before a merge you
  performed as stale. Two agents agreeing is verification only if the second named
  the evidence it looked at, not the first agent's report.
- **The VERDICT contract.** Every gate agent ends with a literal
  `VERDICT: PASS` / `VERDICT: FAIL` last line so the orchestrator can branch
  deterministically.
- **One review, one batch.** A reviewer completes the applicable diff, checklist,
  and adversarial matrix after finding a blocker, then returns every finding at once.
- **A surviving repair means redesign.** Findings carry stable `<path>:<symbol>`
  component keys, normalized by `scripts/review-ledger.py` so the same defect
  named two different ways still counts as one. When one survives an evidenced
  repair it goes through the design gate with a revised matrix before the final
  repair attempt.
- **The ledger is on disk, not in the orchestrator's head.** A failure ledger
  held in conversation is lost to compaction on exactly the long tickets that
  need it. `review-ledger.py`
  owns strike counts, the blocking/advisory split, and the loop's next action.
- **The blocking set only shrinks.** Round 1 sweeps the whole diff with full
  blocking authority. From round 2 reviewers inspect the repair delta and its
  affected boundaries, and only open ledger components, regressions in the
  delta, and security findings may block. Without that, a fresh reviewer each
  round finds a fresh nit forever and the PR never lands.
- **Not everything true is blocking.** Correctness, security, uncovered
  acceptance criteria, disabled tests, and cross-surface disagreement block.
  Dead weight, naming, and "while I was in here" are advisory: recorded on the
  ledger, carried into the PR body, never a FAIL.
- **A blocker carries closure evidence.** It names the failing input or
  precondition, production path, wrong outcome and impact, plus a reproduction or
  exact falsifying assertion under the configured profile. Stronger-profile
  hypotheticals remain advisory instead of expanding the PR mid-review.
- **Implementers preflight once.** Before review they re-run the criterion map
  and adversarial tests, preserve before-fix evidence, inspect affected callers,
  and return any unproven criterion unresolved rather than starting a review loop.
- **Both loops have an end.** `max_design_rounds` (default 5) bounds pre-code
  design iteration. `max_repair_cycles` (default 2) counts durable repair
  reports, not reviewer passes. Hitting either cap stops and hands the human
  `review-ledger.py handoff <pr>` -- it never merges a blocked PR, and it never
  runs round eleven.
- **Repairs close named findings.** One deduplicated repair brief carries stable
  finding IDs. The repair report maps every ID to root cause, change, and
  verification; code and security then re-review the same exact head before the
  attempt is complete.
- **Mechanical enforcement, not just discipline.** The sanctioned merge script
  validates evidence without hooks; an optional trusted hook and branch
  protection add independent layers.
- **Worktree isolation + safe cleanup policy.** Each agent gets its own git worktree.
  Cleanup defaults to `manual`. With `worktree_cleanup: auto`, the orchestrator
  explicitly removes the completed ticket worktree after merge and a trusted
  Stop hook may sweep other finished (unlocked + clean) agent worktrees. Dirty
  and locked worktrees are always preserved. Correctness never assumes a hook
  was registered by the current host.

## Release And Candidate Workflows

If this repository uses `schema_version: 2`, release and candidate behavior is a
configured state machine. Before any release mutation, run:

```bash
scripts/orchestration-engine.py validate-config
scripts/orchestration-engine.py adapter-plan --host claude <transition>
```

The plan declares required evidence, CI categories, approval classes, branch
roles, candidate identity, artifact identity, tags, environment roles,
reconciliation, and cleanup. Execute the transition only through
`scripts/orchestration-engine.py transition ...`.

If this repository still uses the legacy schema, continue following the
repository's existing release process. Legacy configs do not automatically adopt
release-candidate states.

## Recovery: a dead agent's work is not lost

A background agent that dies (rate-limit, crash) leaves its work UNCOMMITTED in
its dirty worktree; the Stop-hook sweep skips dirty worktrees, so it survives.
Read its transcript for the verdict, `git -C <worktree> status` for the work,
then commit FROM the worktree path and open the PR yourself.
