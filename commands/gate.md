---
description: Run the independent review gates (code-review, then security-review when warranted) on a PR and merge it on green.
argument-hint: <pr-number>
---

Gate PR #$ARGUMENTS. Read `.orchestration/config.yaml` and validate it before
mutation:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/orchestration-engine.py validate-config
```

For `schema_version: 2`, use the configured transition plan for the target gate
or merge transition. Branch roles, evidence, approvals, CI categories, and
adapters come from:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/orchestration-engine.py adapter-plan --host claude <transition>
```

For legacy configs, use the existing review gates below.

Before each `code-reviewer` or `security-reviewer` launch, resolve its route with
`${CLAUDE_PLUGIN_ROOT}/scripts/context_pipeline.py route --config
.orchestration/config.yaml --role <role>`. Desktop routes use fresh native
agents; API routes use `context_pipeline.py payload --config ... --role <role>`
and pipe it to `${CLAUDE_PLUGIN_ROOT}/scripts/api_agent.py run --request -
--config .orchestration/config.yaml --role <role> --ticket <ticket> --run-id
<stable-run-id> --review-pr <pr> --review-authorization <token>`. Issue it first
from the valid ledger phase with `review-ledger.py permit-review <pr>
--role <role> --head <full-exact-head>`. Desktop fallback is permitted only before provider
acknowledgement; a provider id, timeout after submission, or uncertain state
must be reconciled and never duplicated.

0. **Open the ledger and build the round brief.** The failure ledger lives on
   disk, not in this conversation -- it survives compaction, normalizes component
   keys so a repeated defect actually accumulates strikes, and decides when the
   loop stops:

   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/review-ledger.py open <pr>
   ${CLAUDE_PLUGIN_ROOT}/scripts/review-ledger.py brief <pr>
   ```

   Paste the `brief` output verbatim into every reviewer's brief this round. Launch
   code and required security reviewers concurrently against the same exact head.
   It
   carries the round number, the scope mode, the round-aware uncertainty rule,
   and the open component keys the reviewer must reuse. Without it a reviewer
   assumes round 1 and reviews with full blocking authority.

   Keep `worker_trust_profile` from `.orchestration/config.yaml` fixed across
   both reviews. It governs only orchestration workers versus the host and never
   narrows application or tenant security.

1. **Code review.** Launch the `orchestration-code-reviewer` agent (a FRESH
   agent, no implementer context) on the PR. Generate and pass the raw unified
   base-to-head git diff as its default and authoritative code input. Also pass
   only the ticket, configured rules docs, stable repository map, and round
   brief; do not pass a full-codebase index. Additional source is allowed only for a
   named verification or regression check. It must re-derive correctness, run
   the repo's review skill + self-checks, finish the full checklist/diff/adversarial
   matrix even after finding a blocker, then return only the concise structured
   review JSON. Explanations belong only to findings.
   Each blocker must state a concrete failing input/precondition, production
   path, wrong outcome/impact, and reproduction or exact falsifying assertion
   under the configured profile. Stronger-profile hypotheticals are advisory;
   they cannot silently turn this PR into host infrastructure work.

2. **Security review.** Read the authoritative PR `headRefName` and `baseRefName`
   from GitHub, save the raw base-to-head unified diff, and run
   `${CLAUDE_PLUGIN_ROOT}/scripts/orchestration-engine.py security-gate
   --source-branch <headRefName> --target-branch <baseRefName> --diff-file
   <raw-diff-file>`. This evaluates diff, source-branch, and target-branch
   triggers. If `required` is true, launch the
   `orchestration-security-reviewer` agent (another fresh agent) with that same
   raw unified diff and diff-isolated context. It must return the same concise
   structured review JSON. If false, record the empty reasons and skip. If PR
   metadata, diff capture, or the decision fails, stop fail-closed.

3. **Record the round.** Record every completed gate through the ledger, blocking
   and advisory findings alike:

   ```bash
   ${CLAUDE_PLUGIN_ROOT}/scripts/review-ledger.py complete-review <pr> \
     --role code-reviewer --phase-permit <token> \
     --result .orchestration/.review-results/code-review.json
   ${CLAUDE_PLUGIN_ROOT}/scripts/review-ledger.py record <pr> --gate code-review \
     --result .orchestration/.review-results/code-review.json --head <exact-sha> \
     --phase-permit <token>
   ```

   The validated result carries blocking, advisory, severity, regression, and
   finding explanations. The ledger increments strikes, auto-resolves components this gate
   no longer reports, demotes out-of-scope new findings in a frozen round, and
   returns `next_action`. Its `effective_verdict` governs, not the reviewer's
   claimed one.

4. **Act on `next_action`.**
   - `review` -> after every required gate records, generate one deduplicated
     `review-ledger.py repair-brief <pr>`. Give it to one fresh implementer on the
     same branch. Require root cause, change, affected boundaries, objective
     closure, and verification for every stable finding ID. Record its strict
     JSON with `record-repair`, re-run code and required security reviews
     concurrently against that exact repaired head, record both, then call
     `record ... --head <exact-sha>` and `complete-repair-review`. Advisory
     findings never enter the repair brief.
   - `redesign` -> an agreed finding survived a completed repair. Launch the
     `orchestration-design-reviewer` scoped to the named component with a revised
     adversarial matrix; do not authorize another narrow patch. On its
     `VERDICT: PASS`, run `review-ledger.py redesign <pr> --key <key> --verdict PASS`.
   - `escalate-human` -> the repair cap is spent with blocking findings still open.
     STOP: do not merge, do not run another round. Give the user
     `review-ledger.py handoff <pr>` with the PR link. A component that survives
     two evidenced repairs needs human diagnosis, not another autonomous patch.
   - `gates-clear` -> necessary, not sufficient. Confirm the security gate
     actually ran if the diff triggers it, then continue below.

   Never merge while a blocking component is open.

5. **Merge on green.**
   - All gates `PASS` AND every required `verification:` is GREEN AND the
     configured target CI checks green -> carry the ledger's advisory findings
     into the PR body as follow-ups, record the marker
     (`${CLAUDE_PLUGIN_ROOT}/scripts/merge-guard.sh --record-green <pr> [result_file]`) and merge via
     `${CLAUDE_PLUGIN_ROOT}/scripts/merge-on-green.sh`. The script directly revalidates the active
     plugin version, exact PR head branch/sha, exact target base branch/sha, and
     marker freshness. The merge-guard hook is optional defense in depth.

Report each gate's verdict, the round number and scope mode, the blocking
findings (if any), the advisory findings carried to the PR body, and the merge
result. CI-green alone is NOT the gate -- the independent verdicts are mandatory.
