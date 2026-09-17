# The review loop

The gates decide whether a PR is safe to land. The review *loop* is what happens
when they say no: fix, re-review, repeat. This document is about making that loop
terminate without lowering the bar.

## Why loops ran long

A review loop with no termination condition runs until something external stops
it. The original design had exactly one exit -- unanimous `VERDICT: PASS` -- and
four properties that kept pushing it away from that exit:

1. **A fresh reviewer every round.** No memory of what was already adjudicated,
   by design, so the author's narrative can never contaminate the gate. The cost
   is that each round is an independent draw from the space of possible findings.
2. **A fresh broad hunt every round**, over a diff that grows with each fix. More
   unrelated surface to find something new in, every time.
3. **No severity floor.** "There is no minor, merge anyway" made a note about
   premature abstraction as load-bearing as an IDOR.
4. **Uncertainty treated as a defect.** A reviewer could spend a repair cycle on
   a plausible concern without naming a failing input, production path, impact,
   or falsifying assertion. The implementer then had no objective closure target.

Together those make P(some blocking finding) roughly constant round over round.
That is a process with no absorbing state, and 10+ rounds is its expected tail,
not an anomaly.

The old strike-based redesign escalation was supposed to break exactly this cycle, and
it almost never fired. It counted failures per component key -- a
free-text string each fresh reviewer invented. `auth/sessionStore` in round 1 and
`session-refresh` in round 4 are the same defect wearing two names, so the strike
never landed. The ledger holding those counts also lived in the orchestrator's
context window, which compacts on precisely the long tickets that need it.

## What makes it converge

**The blocking set must shrink monotonically.** That is the property everything
else serves.

- **Round 1 -- full authority.** A round is one logical review generation of an
  exact PR head, not one reviewer response. Code and security may finish in
  either order and both remain in round 1. Sweep the whole diff; every defect
  class may block. Thoroughness is free here, and a defect not raised now loses blocking
  authority later, so there is pressure to be exhaustive exactly once.
- **Round 2+ -- scope freeze.** Inspect the repair delta, every open component,
  and affected callers/trust boundaries. What may block is also narrow: open
  ledger components, regressions in the delta, and security or data-loss findings.
  Anything else newly noticed becomes advisory.
- **Blocking vs advisory.** Correctness, security, uncovered acceptance criteria,
  disabled tests, cross-surface disagreement, and root-cause suppression block.
  Dead weight, naming, and "while I was in here" are recorded and carried to the
  PR body. They are not discarded -- they are just not merge blockers.
- **Evidence before blocking.** Every round investigates uncertainty, but a
  blocker names the failing input/precondition, production path, wrong outcome
  and impact, plus a reproduction or exact falsifying assertion. Incomplete
  hypotheses are advisory and name the evidence that would settle them. Later
  rounds remain even more conservative because a false FAIL can consume the last
  repair opportunity.
- **Keys that are derived, not invented.** The structured `component` value is
  the bare `<path>:<symbol>` key, with no `[component: ...]` wrapper. Line
  numbers are stripped (they drift on rebase) and free-text subsystem names are
  rejected. `review-ledger.py` normalizes keys so one repair brief cannot split
  a repeated defect into multiple identities.
- **Separate caps end both loops.** `max_design_rounds` (default 5) counts
  pre-code design verdicts. `max_repair_cycles` (default 2) counts explicit
  repair reports, not review passes, so concurrent code and security gates
  consume one attempt together. Neither cap permits a merge with blockers.
- **A repair is a checkable artifact.** `repair-brief` emits one deduplicated set
  of finding IDs. `record-repair` requires root cause, change, and verification
  for every ID. `complete-repair-review` closes an attempt only after every
  required gate reviews that exact head.

## The ledger

`scripts/review-ledger.py` owns this state on disk, under
`.orchestration/.review-ledger/pr-<n>.json` (gitignore it). It is host-neutral:
the Claude commands and the Codex skills drive the same script.

```bash
review-ledger.py open <pr>                    # once per PR
review-ledger.py brief <pr>                   # paste into every reviewer brief
review-ledger.py record <pr> --gate code-review \
  --result .orchestration/.review-results/code-review.json
review-ledger.py record-security-gate <pr> --head <full-exact-head> \
  --decision .orchestration/.review-results/security-gate.json
review-ledger.py repair-brief <pr>
review-ledger.py record-repair <pr> --report .orchestration/.review-results/repair.json
review-ledger.py correct-repair-head <pr> --head <full-exact-head> --reason "<reason>"
review-ledger.py complete-repair-review <pr>
review-ledger.py rebind-generation <pr> --head <full-exact-head> --reason "<reason>"
review-ledger.py metrics <pr>
review-ledger.py status <pr>                  # strikes, open set, next action
review-ledger.py redesign <pr> --key <key> --verdict PASS
review-ledger.py handoff <pr>                 # the human escalation report
```

`rebind-generation` is the safe path when a PR head changes for a reason other
than fixing ledger findings, such as updating from the target branch or applying
an already-authorized policy change. It requires the exact current Git head and
a complete authoritative generation with no open blockers or outstanding
permits. It keeps all history, findings, strikes, and repair-cycle accounting,
then binds the next generation to the new head. Use `record-repair` instead when
the commit addresses an open finding.

### Required gates

Every configured `gates:` entry the ledger owns (`code-review`,
`security-review`) is required for each generation, whether or not a permit was
issued for it. A missing or empty `gates:` key fails closed to both, matching
`templates/config.yaml`. `code-review` is always required, even when a
`gates:` list omits it; only `security-review` is optional. Issued permits,
rebinds, and a pending repair can only
add to that set, and `complete-repair-review` recomputes it, so a ledger that
stored a shorter set is corrected on read.

A configured `security-review` is waived only by the output of
`orchestration-engine.py security-gate` recorded with `record-security-gate`
for the current generation's exact head with `required: false`. With no
decision it stays required; any `required: true` decision or any issued
security permit keeps it required. Decisions never carry into a repair or
rebind generation, because the new head has a new diff: record a new one.

### Exact heads

`permit-review`, `rebind-generation`, and `record-security-gate` bind the local
`git rev-parse HEAD`, because reviewers and completion receipts read the local
tree. To review a PR head that the current checkout is not on, use a detached
worktree rather than moving the checkout:

```bash
git worktree add --detach <review-path> <full-exact-head>
cd <review-path>   # permit-review, reviewers, complete-review, record
git worktree remove <review-path>
```

Linked worktrees resolve the same shared ledger directory and canonical config
from the main checkout, so every ledger command sees identical state.

`record-repair` resolves the report `head` with `git rev-parse --verify` and
stores the full commit id; a head that does not resolve to exactly one commit
is refused. Ledgers written earlier may hold an abbreviation. `record` accepts
its full expansion only when git resolves the abbreviation unambiguously to
that commit, and `correct-repair-head` rewrites a pending attempt's
abbreviation to that same expansion with an audit entry. It refuses any other
commit, a completed attempt, or a head that is already full, so it needs no
operator capability.

`complete-review` is for desktop reviewers. An API reviewer run completes its
own permit, so its result goes straight to `record`. Repeating
`complete-review` with the identical result returns the existing unconsumed
receipt (`already_completed: true`); a different result digest, or a
recorded, cancelled, or superseded permit, is refused.

An escalated ledger is immutable to workers. If a human decides that the same
PR should receive more repair cycles, issue a PR-bound capability with an
absolute lifetime ceiling and pipe it directly to the ledger:

```text
sudo /usr/local/libexec/orchestration-recovery-authority issue-review-repair \
  --repository /absolute/repo --pr 123 --ceiling-repair-cycles 3 \
  --reason "Approved one additional bounded repair after reviewing the handoff" \
| python3 /absolute/plugin/scripts/review-ledger.py authorize-repair 123 \
  --operator-capability-stdin
```

This acknowledges the escalation without deleting it. The command preserves
every finding, gate result, repair attempt, and PR binding; it only raises the
absolute repair-cycle ceiling. The effective ceiling is read from the live,
root-owned grant on every decision; repository ledger edits cannot create
authority. Reaching the ceiling, expiry, or root revocation restores the
escalation stop.

Pre-code design uses the same durable store keyed by ticket/change identifier:

```bash
review-ledger.py design-open <ticket>
review-ledger.py design-record <ticket> --verdict FAIL --evidence <artifact>
review-ledger.py design-handoff <ticket>
```

`record` is where the mechanics live. It binds every gate result to the permit's
review generation, increments strikes, auto-resolves any
component this gate no longer reports (a completed re-run that stays silent is
the evidence a fix held), demotes out-of-scope new findings in a frozen round,
and returns `next_action`:

| `next_action` | meaning |
|---|---|
| `review` | generate one repair brief, record the repair, re-run all required gates |
| `redesign` | a finding survived a completed repair -- scoped design gate |
| `escalate-human` | cap spent with findings open -- stop, hand over `handoff` |
| `gates-clear` | necessary, not sufficient; confirm the security gate ran |

The repair report must cover the exact open finding set. A repaired head cannot
complete until every required gate records, and no third repair starts after the
configured cap.

Ledgers written before review generations were explicit can be repaired with
`review-ledger.py migrate-concurrent-review <pr> --reason <audit-reason>`. The
migration is deliberately fail-closed: it accepts only an unrepaired initial
generation whose gate results have consumed permits for one exact head, and it
can only promote ordering-demoted advisories back to blockers. It never clears
a finding, resets a repair, or creates merge authority.

And the ledger's `effective_verdict` governs, not the reviewer's claimed one --
a round whose findings were all demoted is a PASS with advisories attached.

Known terminal API failures (invalid structured output, truncation, or exhausted
tool rounds) release their started phase permit without creating a PASS receipt.
The next attempt requires a fresh permit and retains all billable usage and
execution-attempt accounting. Token-count failures before submission also allow
a fresh permit. Uncertain submissions and nonterminal provider responses remain
fenced for reconciliation; they cannot authorize another reviewer.

## The security gate is exempt

The scope freeze narrows what the *code* reviewer may block on. It does not apply
to `orchestration-security-reviewer`. A leak found in round 4 blocks exactly as
hard as one found in round 1, and `record` never demotes a security-gate finding.
Convergence is a scheduling concern; it is not a reason to ship a data leak.

That exemption does not authorize threat-model expansion. The configured
`worker_trust_profile` applies only to orchestration workers versus their host:
`cooperative-worker` covers mistakes, crashes, loops, duplicate execution, and
accidental misuse; `isolated-worker` requires independently owned process and
credential boundaries. Application users, tenants, remote clients, ticket text,
and provider responses remain untrusted under both profiles. A security blocker
must identify a profile-relevant production path and closure evidence; a
hypothetical stronger-profile concern is recorded as advisory.

## Tuning

`max_design_rounds: 5` lets architecture converge before implementation. Keep
`max_repair_cycles: 2` unless measured closure data proves another value safer.

Resist raising it as a reflex. A ticket that repeatedly burns the cap is usually
telling you the acceptance criteria are too vague to test against -- the same
signal a component collecting strikes gives. Scope the ticket harder instead;
that is cheaper than another three rounds.


A root-issued ticket restart allowance can authorize higher absolute design and
repair caps without rewriting the ledger. The authority is checked when deriving
a review plan or issuing a phase permit; expiry/revocation restores the stored
caps. Failed verdicts, findings, repair histories, and outstanding phase permits
remain intact. Reopening with a larger CLI cap cannot substitute for authority.
See [restart allowances](sprint-controller.md#restart-allowances-and-legacy-reconciliation).
