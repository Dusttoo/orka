---
description: Run one ticket end-to-end through the orchestration pipeline (implement -> code-review -> security-review -> verify -> merge-on-green).
argument-hint: <ticket-id or description>
---

Drive ONE unit of work through the full pipeline. Target: `$ARGUMENTS`.

Read `.orchestration/config.yaml` first. Workflow procedures and reusable safety
tests are plugin-owned; the target repository supplies configuration, rules, and
project-specific acceptance criteria/tests.
Validate config before mutation:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/orchestration-engine.py validate-config
```

For `schema_version: 2`, branch roles, transition guards, approvals, CI
categories, ticket adapters, and target branches are policy from the configured
workflow. Use `${CLAUDE_PLUGIN_ROOT}/scripts/orchestration-engine.py adapter-plan --host claude
<transition>` whenever the repo declares this ticket flow as explicit
transitions. For legacy configs, keep the existing implement -> review ->
security -> verify -> merge-on-green flow below.

Before every `design-reviewer`, `implementer`, `code-reviewer`,
`security-reviewer`, or `visual-qa` launch, resolve that role through
`${CLAUDE_PLUGIN_ROOT}/scripts/context_pipeline.py route --config
.orchestration/config.yaml --role <role>`. A `desktop` route uses the existing
native agent launch. An `api` route builds the request with
`context_pipeline.py payload --config ... --role <role>` and pipes it to
`${CLAUDE_PLUGIN_ROOT}/scripts/api_agent.py run --request - --config
.orchestration/config.yaml --role <role>`, including the ticket/sprint/run id
when available. Before an API reviewer run, issue a single-use exact-head permit
with `review-ledger.py permit-review` and pass it to `run --review-pr
<pr-or-design-id> --review-authorization <token>`. The runner owns provider submission, limited tools, durable
usage, and budget enforcement. Desktop fallback is legal only when the runner
proves the API request failed before any provider/run id existed; submitted or
uncertain work stays reserved for reconciliation and is never duplicated.

For a model-less OpenAI desktop route, use Codex's ChatGPT subscription login
and default model without an API credential, custom base URL, or model override.
Subscription turns are unmetered by Orka, but all non-spend workflow gates
remain mandatory. Model-less Claude routes are unsupported because Claude Code
does not expose reliable subscription-auth evidence.

**You are a lossy relay -- do not hand down stale facts.** Every hop from an
agent's report into a brief into a durable doc can drop the uncertainty marker.
Before you write anything into a brief or a durable artifact:
- Put a **grep target or symbol**, never a `file:line`, in a brief -- line numbers
  drift the moment a branch moves; a grep target is self-verifying.
- Never hand over a **selector, snippet, or query you did not run**. State intent +
  constraint and let the implementer write and verify the code.
- **Label provenance** on every relayed claim (`verified now` / `reported by
  <agent>, unverified` / `from the ticket, unverified`) and tell the receiver to
  verify and report what they drop.
- Two agents agreeing is verification **only if the second named the evidence it
  looked at** -- otherwise it is transitive trust.
- **Re-derive before any durable write** (CLAUDE.md, ticket, PR body): open the
  file, now, on this branch. Anything learned before a merge you performed is
  stale.

Steps:

1. **Scope check.** If `ticket.kind != none`, confirm the ticket is Ready (a
   description you could write a failing test from). If it is too thin, scope it
   or push back BEFORE cutting a branch. If `ticket.kind == none`, treat
   `$ARGUMENTS` as the spec.

   Resolve `worker_trust_profile` now. It applies only to orchestration workers
   versus the host and never narrows application security. Keep that profile
   fixed through design, implementation, and review.

2. **Pre-implementation gates.** Before cutting a branch or editing production
   code, create an adversarial test matrix. Every row names the attack/failure
   mode, setup/input, invariant, test layer, and falsifying assertion. Cover all
   relevant parser/interpreter syntax (including shell wrappers, substitutions,
   heredocs, redirections, and pipelines), ignored/untracked files, failed Git
   or other inspection, partial execution, cleanup recovery, permissions,
   concurrency, retries, and hostile input. N/A requires a reason. First perform
   an architecture-feasibility check: if an invariant requires a root-owned
   installation, distinct UID, daemon, container, cloud resource, or rollout
   outside the authorized scope, split or defer it instead of approving a local
   approximation. For planned security-sensitive infrastructure or an external
   boundary crossing, launch a fresh
   `orchestration-design-reviewer`. Open `review-ledger.py design-open
   <ticket-or-change>`, record FAIL with explicit evidence, and record PASS only
   through `design-record --result <json>` bound to the exact source SHA. Stop with
   `design-handoff` if `max_design_rounds` is spent. It must define the trust boundary and
   impossible guarantees, reject fragile designs, audit the matrix, and return
   `VERDICT: PASS` before implementation.

3. **Implement.** Launch the `orchestration-implementer` agent with
   `isolation: "worktree"`, passing the ticket body + the click-path. Use the
   configured source and target branch roles. In legacy configs, this is one
   agent, one ticket, one worktree, one branch, one PR to the legacy configured
   target branch. Wait for its structured report (PR number, branch,
   worktree, SELF_CHECK).

4. **Gate.** Run the gate pipeline on the resulting PR -- invoke
   `/orka:gate <pr>` (code-review and security-review whenever the shared
   `orchestration-engine.py security-gate` decision requires it from diff or
   authoritative PR source/target branches). Both must return validated structured
   PASS results with no blocking findings. Launch both required reviewers
   concurrently against the same exact head and round brief.
   Give each reviewer the raw unified base-to-head git diff by default, not a
   full-codebase index; only a named verification or regression may expand scope.
   - Reviewers finish the entire checklist and adversarial sweep and batch all
     findings, even after the first blocker.
   - A blocker names a concrete failing input/precondition, production path,
     wrong outcome/impact, and reproduction or exact falsifying assertion under
     the configured profile. Stronger-profile hypotheticals are advisory.
   - The durable failure ledger owns the loop. Open it once with the immutable
     ticket binding (`${CLAUDE_PLUGIN_ROOT}/scripts/review-ledger.py open <pr>
     --work-kind jira --work-id <ticket>`), paste
     `review-ledger.py brief <pr>` into every reviewer brief, save the JSON under
     `.orchestration/.review-results/`, and record every completed gate with
     `review-ledger.py record <pr> --gate <gate> --result
     .orchestration/.review-results/<gate>.json --head <exact-sha>
     --phase-permit <token>`. Native reviewers first atomically complete that
     permit with `review-ledger.py complete-review`. It normalizes each finding's
     bare `<path>:<symbol>` component key, counts strikes across all gates and
     rounds, freezes blocking scope after round 1, and returns `next_action`.
   - If a non-findings commit moves the PR head after a complete generation, run
     `${CLAUDE_PLUGIN_ROOT}/scripts/review-ledger.py rebind-generation <pr>
     --head <full-exact-head> --reason "<auditable reason>"` before requesting
     new permits. It preserves history, findings, strikes, and repair-cycle
     accounting and refuses to run with open blockers or permits. Findings
     repairs still use `record-repair`.
   - `review` first generates one `repair-brief` after all gates record. Give its
     deduplicated stable IDs to a fresh implementer, require root cause/change/
     affected-boundary/verification evidence for every ID, and record the strict
     JSON with `record-repair`. Re-run required reviews concurrently on that exact
     head, record each with `--head <exact-sha>`, and call
     `complete-repair-review`. Advisory findings go to the PR body.
     A component that survives a completed repair returns `redesign`. When the
     repair cap is spent, the ledger returns `escalate-human`: STOP and hand the
     user `review-ledger.py handoff <pr>`.

5. **Verify (when configured).** For each entry in the config `verification:`
   block whose `when:` includes this configured target, run it on the rebased
   branch: `${CLAUDE_PLUGIN_ROOT}/scripts/run-verification.sh <name>`. It writes a
   sha-stamped GREEN result file on success (RED = no file = do not merge). If
   the change has a user-visible surface, also run the `orchestration-visual-qa`
   agent against the `Reachable via:` click-path; it must end `VERDICT: PASS`.

6. **Merge on green.** Only after every gate PASSES, every required verification
   is GREEN, and the configured target CI checks are green:
   record the marker with `${CLAUDE_PLUGIN_ROOT}/scripts/merge-guard.sh --record-green <pr>
   [result_file]` (pass the verification result file so the marker is validated
   against the PR head), then merge with `${CLAUDE_PLUGIN_ROOT}/scripts/merge-on-green.sh <pr> <branch>
   all-green <verify_path>`. The script validates the marker, plugin version,
   PR head/branch, target base/sha, and freshness directly. Host hooks are
   defense in depth, never a correctness dependency.

7. **Close the loop.** If `ticket.kind != none`, transition the ticket. Confirm
   the work actually landed on the configured target branch. Read
   `worktree_cleanup` (default `manual`). When it is `auto`, run
   `${CLAUDE_PLUGIN_ROOT}/scripts/cleanup-worktree.sh <WORKTREE>` explicitly after the verified merge;
   when it is `manual`, preserve and report the worktree. Do not assume a Stop
   hook ran. Report what merged + the click path.

Respect `concurrency_max` for ticket lanes and `max_heavy_processes` for local
build/test/browser chains. Apply provider backpressure when the API ledger shows
sustained throttling. Dual gates on ONE PR run concurrently against one head.
