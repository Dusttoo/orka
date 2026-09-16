---
name: gate-pr
description: Run the independent review gates on an existing PR and merge it only after code-review, required security review, configured verification, and CI are green. Use when the user asks to gate a PR, review and merge a PR through the orchestration pipeline, run merge-on-green, or run the equivalent of the Claude /gate command.
---

# Gate a PR through merge-on-green

Run the review gates on one existing PR and merge it only after every required
gate is green. This is the natural-language entry point for Codex and the same
workflow as the Claude Code `/gate` command.

## Plugin paths

The role briefs and scripts below live in this plugin, not necessarily in the
target repository. Resolve these paths from this skill file before executing or
reading them:

- `../../agents/orchestration-code-reviewer.md`
- `../../agents/orchestration-design-reviewer.md`
- `../../agents/orchestration-implementer.md`
- `../../agents/orchestration-security-reviewer.md`
- `../../scripts/context_pipeline.py`
- `../../scripts/api_agent.py`
- `../../scripts/merge-guard.sh`
- `../../scripts/merge-on-green.sh`
- `../../scripts/orchestration-engine.py`
- `../../scripts/review-ledger.py`
- `../../scripts/run-gates.sh`
- `../../scripts/run-verification.sh`
- `../../scripts/version_policy.py`

Execute scripts by absolute path while keeping the target repository as the
working directory.

## Procedure

0. Validate the active runtime against repository policy before opening or
   resuming a review:
   `version_policy.py --plugin-root <resolved-plugin-root> --config
   .orchestration/config.yaml`. Stop fail-closed unless it reports
   `status: compatible`. `review-ledger.py permit-review` and
   `merge-guard.sh` repeat this check mechanically, so an older runtime cannot
   mint review authority or record/assert merge evidence.

Before each `code-reviewer` or `security-reviewer` pass, resolve its route with
`scripts/context_pipeline.py route --config .orchestration/config.yaml --role
<role>`. Desktop routes use fresh native agents. API routes build their request
with `context_pipeline.py payload --config ... --role <role>` and use the
`api_agent.py run --request -` adapter with the ticket and a stable run id.
First issue a phase permit from the durable ledger with `review-ledger.py
permit-review <pr> --role <role> --head <full-exact-head>`;
pass it to `run --review-pr <pr> --review-authorization <token>`. The ledger
issues it only while that gate is the permitted next phase, and it cannot be
reused. This is sequencing, not human authentication.
Desktop fallback is allowed only before provider
acknowledgement; submitted, timed-out, or uncertain work must be reconciled
instead of duplicated.
For a native desktop reviewer, write its final structured JSON first, then run
`review-ledger.py complete-review <pr> --role <role>
--phase-permit <token> --result <file>`. API execution creates the same
completion receipt after successful provider output.

1. Read `.orchestration/config.yaml` and run
   `orchestration-engine.py validate-config`. For `schema_version: 2`, use
   `orchestration-engine.py adapter-plan --host codex <transition>` for the
   configured gate or merge transition; branch roles, evidence, approvals, CI
   categories, and adapters come from that plan. For legacy configs, continue
   with the existing review gates below.
   Keep the configured `worker_trust_profile` fixed for every reviewer. It
   governs only worker-versus-host assumptions and never relaxes application,
   tenant, client, ticket-input, or provider security.
2. Open the durable review ledger and build this round's brief:
   `review-ledger.py open <pr>` then `review-ledger.py brief <pr>`. The
   failure ledger lives on disk, not in this conversation -- it survives
   compaction,
   normalizes component keys so a repeated defect actually accumulates strikes,
   and decides when the loop stops. Paste the `brief` output verbatim into every
   reviewer pass this round. A round is the ledger's shared review generation
   for one exact head, never the arrival order of individual gate responses. It
   carries the round number, the scope mode, the
   round-aware uncertainty rule, and the open component keys to reuse. Without it
   a reviewer assumes round 1 and reviews with full blocking authority.
   If the PR head moved for a non-findings change after the current generation
   completed, first run `review-ledger.py rebind-generation <pr> --head
   <full-exact-head> --reason "<auditable reason>"`. This starts a fresh review
   generation while preserving history, findings, strikes, and repair-cycle
   accounting. It is valid only when the prior generation is complete and has no
   open blocker or outstanding permit. Never use it instead of `record-repair`
   for a findings repair.
3. Launch the code-review gate and any required security-review gate concurrently
   against the same exact PR head, raw diff, and round brief. Do not let one
   reviewer's findings contaminate the other's independent pass. Run code review
   as a fresh pass. If the host supports
   subagents, launch one with `orchestration-code-reviewer.md`; otherwise apply
   that brief yourself without using the implementer's reasoning as evidence.
   Generate and pass the raw unified base-to-head git diff as the default and
   authoritative code input, alongside only the ticket, configured rules docs,
   stable repository map, and round brief. Do not pass a full-codebase index;
   additional source is allowed only for a named verification or regression.
   The reviewer must finish the full checklist, diff, and adversarial matrix even
   after finding a blocker, then return only concise structured review JSON.
   Explanations belong only to findings; each finding has a stable component key.
   A blocker must name a concrete failing input/precondition, production path,
   wrong outcome/impact, and reproduction or exact falsifying assertion under
   the selected profile. Do not let a reviewer expand the PR into new host
   infrastructure based only on a stronger, unconfigured threat model.
4. Before launching the pair, read the authoritative PR `headRefName` and
   `baseRefName` from GitHub, save the raw base-to-head unified diff to a file,
   and run `orchestration-engine.py security-gate --source-branch <headRefName>
   --target-branch <baseRefName> --diff-file <raw-diff-file>`. This shared
   decision evaluates `security_required_when`,
   `security_required_source_branches`, and
   `security_required_target_branches`. If `required` is true, run a fresh
   security-review pass using `orchestration-security-reviewer.md` with the same
   raw unified diff and diff-isolated context. If it is false, record the empty
   reasons list and skip. If metadata, diff capture, configuration validation,
   or the decision command fails, stop fail-closed; never infer that security is
   optional.
5. Wait for both launched reviewers, then record every completed gate through the
   ledger, blocking and advisory findings
   alike: `review-ledger.py record <pr> --gate code-review --result
   .orchestration/.review-results/code-review.json --head <exact-sha>
   --phase-permit <token>`. The validated JSON carries
   disposition, severity, regression, and explanation. Both concurrent results
   retain the same generation and scope mode regardless of which is recorded
   first. A partial generation cannot return `gates-clear`; every issued gate
   must be recorded. The ledger increments strikes, auto-resolves components this gate
   no longer reports, demotes out-of-scope new findings in a frozen round, and
   returns `next_action`. Its `effective_verdict` governs, not the claimed one.
6. Act on `next_action`. Never merge while a blocking component is open.
   - `review` -- run `review-ledger.py repair-brief <pr>` once after all gates
     record. Give that single deduplicated brief to one fresh implementer on the
     same branch. The implementer must map every stable finding ID to root cause,
     planned change, affected boundaries, objective closure condition, and named
     verification before editing. After editing it writes the strict repair JSON
     named by the brief; record it with `review-ledger.py record-repair <pr>
     --report <file>`. A claimed closure is not proof. Re-run code and required
     security reviewers concurrently against that exact repaired head, record
     both with `record ... --head <exact-sha>`, then call `review-ledger.py
     complete-repair-review <pr>`. Advisory
     findings go to the PR body and never widen the repair.
   - `redesign` -- an agreed finding survived a completed repair. Run
     `orchestration-design-reviewer.md` scoped to the named component against the
     root design and a revised adversarial matrix; no further implementation
     starts until it returns `VERDICT: PASS`, then record
     `review-ledger.py redesign <pr> --key <key> --verdict PASS`.
   - `escalate-human` -- the repair cap is spent with blocking findings still open.
     STOP: do not merge and do not run another round. Give the user
     `review-ledger.py handoff <pr>` with the PR link. A component that survives
     two evidenced repairs needs human diagnosis, not another autonomous patch.
   - `gates-clear` -- necessary, not sufficient. Confirm the security gate
     actually ran if the diff triggers it, then continue.
7. For each configured `verification:` entry whose `when:` applies to the target,
   run `run-verification.sh <name>`. A GREEN result file is required; RED or a
   missing result file blocks the merge.
8. Confirm every required configured target CI check is green.
9. Carry the ledger's advisory findings into the PR body as follow-ups, then
   record the all-green marker with `merge-guard.sh --record-green <pr>
   [result_file]`, then merge with `merge-on-green.sh <pr> <branch> all-green
   <verify_path>`. The merge script itself revalidates the active plugin version,
   exact PR head branch/sha, exact target base branch/sha, and marker freshness;
   never treat host hook registration as required evidence.

Report each gate verdict, the round number and scope mode, any blocking findings,
the advisory findings carried to the PR body, verification result paths, CI
status, and the merge result. CI-green alone is not the gate; the independent
verdicts are mandatory.
