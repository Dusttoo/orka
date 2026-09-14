# Sprint controller design contract

The sprint controller is the plugin-owned state machine shared by Claude Code
and Codex. Host adapters query Jira and launch agents; the controller alone
decides which ticket may consume a lane and persists that decision before the
launch.

## Trust boundary

Repository configuration is trusted policy. Jira responses, ticket text,
dependency links, agent reports, and restart-era process state are untrusted
inputs. The controller validates Jira keys, stores text only as JSON data,
constrains checkpoint paths to the repository, and never executes ticket text.
Atomic replacement and a file lock coordinate controller processes using the
same checkpoint. The host must pass arguments without shell interpolation.

Plugin adapters own completeness at external boundaries. The Jira adapter
performs authenticated requests restricted to canonical `jira_base_url` policy,
rejects cross-origin redirects before credentials can follow, exhausts parent,
child, and external-dependency pagination, and stores only sanitized,
content-addressed responses. It constructs query policy from canonical
project/sprint configuration; inventory templates carry no authority. All
scheduler metadata, including sprint identity and dependencies, is derived from
those authenticated responses. `jira_sprint_field` names the REST field that
contains the provider's sprint id/name (often a Jira Cloud custom field). Provider batch
adapters perform authenticated terminal lookup and complete result download.
The controller rejects caller-authored receipts, unknown dependencies, and
duplicate keys rather than inventing those facts.

## Impossible guarantees

The controller cannot protect against a malicious same-UID process or
repository owner that edits checkpoints directly, and does not pretend a
locally available signing key creates authentication. It can require provider
I/O to pass through its adapter boundary and bind raw responses by digest. File
locks coordinate cooperating processes; they are not an authorization boundary.
GitHub branch protection and the plugin's merge guard remain the enforcement
boundary for merges.

Every launch is reserved first, and restart plans surface all running
reservations as `needs_reconcile`. `launch-local` starts the process itself and
records controller-owned evidence bound to the exact repository, sprint,
ticket, and attempt. `attach` consumes only that evidence; it never accepts a
caller-supplied PID. Linux `/proc` start ticks or the macOS kernel process start
time are fingerprinted so PID reuse cannot make a replacement authoritative.
`run_ref` remains display metadata. Native task labels and tasks without a
verified adapter require explicit operator recovery. Unknown inspection errors
also remain fenced; only confirmed absence permits automatic requeue.

## Context and provider efficiency

Before querying Jira, the adapter requests only fields consumed by scheduling
and passes that fixed allowlist as Jira's `fields` request parameter. Jira responses pass through
`sanitize-jira`, which allowlists those issue fields and removes rendered/edit
metadata, changelogs, schemas, and avatar links before ticket data reaches an
LLM.

Before launching, adapters resolve `llm` plus the requested `llm.roles` override
with `context_pipeline.py route`. Desktop routes use the native host agent; API
routes build requests with `context_pipeline.py payload --config ... --role
...` and foreground jobs execute through `api_agent.py run`, which enforces role
tools and run/ticket/sprint budgets. Anthropic Messages and OpenAI Responses
payloads, plus Azure Direct Model Chat Completions payloads, share the same stable order:
role brief, repository rules, then the baseline repository map. Ticket data and
the raw active-branch diff remain in the uncached user message. Anthropic gate
and on-demand payloads place a provider-native explicit cache breakpoint at the
selected stable boundary. OpenAI payloads keep the same stable prefix order but
omit all optional cache-control request fields because supported fields differ
across live OpenAI-compatible routes. A compatible route may still apply its
own automatic caching without request metadata. Optional `--effort` maps to OpenAI reasoning effort
or Anthropic adaptive-thinking effort. Azure Direct Model routes omit the
provider-specific effort field for cross-model compatibility; fixed
`budget_tokens` is intentionally not assumed because support differs across
model generations.

Captain visibility is event-driven by default. The captain blocks on native
worker wait primitives (or the detached process PID on CLI hosts) instead of
spending model turns polling unchanged state. It reports launches, phase or gate
transitions, provider degradation, PR/CI transitions, terminal outcomes,
blockers, and user actions immediately. During an otherwise unchanged run it
emits at most the configured heartbeat; the durable sprint checkpoint remains
available for an on-demand summary at any time.

Non-interactive background lanes can be prepared with `prepare-batch`. The
controller accepts only current `plan.launch` tickets explicitly marked as
background and non-interactive, reserves them under the sprint lock, and writes
an Anthropic Message Batches JSON request or OpenAI Batch JSONL plus a durable
marker beneath the configured checkpoint directory. `submit-batch` invokes the
credential-owning adapter, which uploads or submits the immutable request and
records a content-addressed acceptance receipt. `reconcile-batch` invokes that
same adapter for terminal state and every native result/error page, freezes a
content-addressed normalized bundle, and applies each `custom_id` idempotently
before normal per-ticket `finish` calls. An ambiguous submission, nonterminal
status, or missing terminal row leaves only the unresolved reservations fenced;
successful rows are settled and provider-proven nonexecuted rows are released.

Batch credentials are bound to the providers' built-in API origins. A custom
gateway must be approved in the canonical
`.orchestration/provider-origins.json` operator policy; caller environment base
URLs are ignored by the batch adapter. The policy maps credential names, never
credential values, to HTTPS URLs:

```json
{"schema_version":1,"credentials":{"OPENAI_API_KEY":"https://gateway.example/v1"}}
```

`inspect-batch` migrates a schema-v1 marker to a fail-closed
`legacy_uncertain` marker. `recover-legacy-batch --reason ...` records operator
disposition without releasing its usage reservations; provider uncertainty must
still be resolved outside the legacy marker before those funds can be reused.

## Ready ordering

Inventory tickets may carry an optional integer `priority`, lower being more
urgent. `plan` orders actionable tickets on `(priority, key)` and fills lanes
from the front, so a ranked sprint spends its next lane on its most urgent
unblocked ticket and an unranked sprint behaves exactly as before.

Priority ranks; it does not release. Prerequisites, external dependency status,
`concurrency_max`, and Jira-derived blocked states are all evaluated first, so a
high-priority ticket waits behind an unfinished dependency instead of preempting
it. Priority is refreshed Jira metadata: a resync re-ranks pending tickets and
never disturbs a running or terminal record, because a reservation is durable
and a rank change must not move work that already launched.

The controller orders, and the host obeys. `reserve` admits an unblocked pending
ticket only while lane and unfinished-PR capacity remain. It is not an ordering
authority. Hosts must launch the keys `plan.launch` returns.

## Autonomous scope, recovery, and progress

Repositories may opt into `sprint_decomposition.auto_decompose_large_tickets`.
The controller then places unscreened ready tickets in `plan.scope` before
reservation. A fresh read-only `ticket-scoper` worker—not the long-lived
captain—produces the schema-v1 `record-scope` result. It either releases a ready ticket,
places an oversized ticket in `plan.decomposition`, or records a genuine
`operator_decision`. A `ready` result at or above the configured
`complexity_threshold` is rejected. Once the scoper selects `decompose`, the
adapter validates the slice structure rather than independently reapplying that
score threshold. The idempotent Jira decomposition adapter creates a bounded
set of linked subtasks with content-addressed provenance labels. Existing issues
are reused only when their type, parent, source, summary, description, and exact
slice provenance match; after authoritative Jira sync, `record-decomposition`
revalidates the subtask type, content identity, and exact child inventory and
leaves the parent as a tracking record. Children inherit that parent's prerequisites and wait until
the decomposition binding is recorded. A downstream dependency on the parent
is satisfied only when its exact bound child set and prerequisites complete;
missing or changed children keep it blocked. The parent remains `decomposed`,
and summary reports its derived `dependency_complete` value rather than inventing
a parent PR or marking unimplemented work merged.

Repositories may declare
`sprint_decomposition.required_slice_contracts` as a bounded list of contract
names. Every slice must then carry a nonempty `contracts` value for each name
before Jira mutation. Contract values are included in the provenance digest and
Jira description. This lets a repository require decisions such as failure
behavior, migration ownership, compatibility, or trust boundaries without
embedding any project's policy in Orka. The adapter creates configured slice
dependencies idempotently and immediately re-reads Jira; `record-decomposition`
also requires the next authenticated sprint inventory to contain every expected
edge before binding or releasing children.

An `operator_decision` assessment may include a stable `decision_key` and
question. A repository can answer recurring questions once in its reviewed
top-level `sprint_decisions` map using `status: approved`, a bounded `answer`,
and `rationale`. That registry is supplied to fresh scopers. When an affected
ticket is synchronized or assessed, the controller records that the decision
was applied and requires a fresh scope pass under the approved policy rather
than treating the old assessment as a design pass.

The Jira adapter transitions untouched children in Jira's new status category
to a configured ready status, in configured preference order. When Jira does
not expose a direct transition, a repository may declare a bounded
`sprint_decomposition.jira_ready_transition_path` of one through five destination
statuses. Only the final status may be launchable. The adapter follows each
exact step, requires a transition without missing required fields, re-reads Jira
after every POST, and safely resumes from an already reached intermediate state.
Active, blocked, and completed work is preserved. Per-child
`readiness_blockers` do not discard successful creation/linking results; sync and
record the returned children, then continue independent work around those blockers.
Untouched Jira-owned readiness follows subsequent authoritative syncs in both
directions. Scoping decisions and started worker outcomes remain durable.
When a source issue is itself a subtask, `jira_subtask_decomposition_mode:
sibling` creates slices under its verified existing parent. The controller then
requires fresh inventory to prove every sibling relationship before binding.

Transition and issue-link response handling follow the
[Jira transition API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-transitions-post)
and [issue linking model](https://developer.atlassian.com/cloud/jira/platform/issue-linking-model/).

Recoverable execution, review repair, and decomposition are autonomous queues,
not generic user-action stops. `autonomous_work_remaining` remains true while
any of those queues, a live lane, or a launchable ticket exists. External
blockers remain visible without stopping independent lanes. `plan.repair` and
`plan.recovery` include only tickets with remaining admission capacity and a
mechanically stopped execution unit. `plan.recovery_waiting` units still occupy
lanes until exit. Ineligible work appears in `decision_queue` (also included in
`summary`); it does not by itself keep `autonomous_work_remaining` true. Requeue
preserves branch/PR identity for continuation on the existing work. Direct
reservation is bound to the current controller launch plan, so clients cannot
skip an actionable repair, recovery, continuation, or WIP limit.

Temporary sprint reservation pressure is recomputed at each API admission; it
does not latch a permanent pause on the requesting ticket. An actually exhausted
sprint allowance still blocks paid requests. Ticket-local dollar pauses retain
their authority requirements. Completely released nonexecuted requests free
reviewer capacity, but the total execution-attempt cap still bounds repeated
operational failures. Accepted and unresolved reviewer runs continue to count.

Workers record durable milestones through attempt-fenced `record-progress`.
`plan.stalled`
compares actual settled spend—not pessimistic reservations—since the last
milestone against `max_usd_without_progress`. Crossing that threshold never
weakens a gate or grants budget; it tells the captain to stop the execution unit
and recover or decompose its preserved work.

`pr_drain_first` defaults to true. After repairs and mechanically eligible
continuations, pending work already bound to a PR is ordered ahead of untouched
tickets. With `preserved_pr_auto_recovery: true`, `plan.pr_reconciliation`
contains preserved open-PR work only when the ticket has no open usage
reservation, GitHub proves the exact repository/PR/branch/head/tree identity,
one clean worktree matches that branch beneath the configured worktree root,
and Linux process inspection proves the worktree quiescent. The host runs
`reconcile-preserved-pr` for that ticket, which records a new recovery binding
and returns it to bounded repair/continuation. Missing or ambiguous evidence
remains an operator decision; Orka does not fabricate execution authority.

## Rejected fragile designs

- Host-specific queues were rejected because Claude and Codex would drift.
- Launch-then-checkpoint was rejected because a crash can duplicate a worker.
- Treating every Jira `Blocks` link as a prerequisite was rejected because link
  direction reverses the dependency meaning.
- Inferring missing external dependency status as complete was rejected because
  partial Jira reads would release work incorrectly.
- Clearing running entries on restart was rejected because process liveness is
  outside the controller's trust boundary.
- Stopping the sprint on one blocker was rejected because independent tickets
  remain safely actionable.
- Treating an absent priority as most urgent was rejected because a partial Jira
  read would then outrank an explicit ranking decision.
- Letting priority preempt a running lane was rejected because reservations are
  durable and re-ranking cannot prove a worker is gone.
- Requiring a priority on every ticket was rejected because most projects rank
  only part of a sprint and a forced default is an invented fact.

## Recovery invariant

The controller never admits a new ticket while the running count is at or above
`concurrency_max`. If configuration is lowered below an existing running count,
it reports `over_capacity` and waits instead of killing or launching work. A
ticket can enter `running` only from `pending`, with all prerequisites completed,
under the checkpoint lock.
Terminal results are recorded immediately. A refreshed Jira inventory may add
metadata and tickets, but never overwrites a terminal or running local result.
Tickets removed from a refreshed query become user action instead of silently
launching from stale state.

Local workers run inside a controller-owned execution unit. On Linux the
controller uses a transient user systemd scope backed by cgroup v2 when the host
provides it, and recovery requires that whole cgroup to be unpopulated. The
identity binds the boot id, invocation id, cgroup, and exact supervisor birth.
On macOS `proc_pidinfo` supplies the exact birth identity, but the supervisor
session is cooperative containment: an escaped descendant cannot be disproved,
so automatic recovery is disabled. Launch intent precedes process creation and
the supervisor retains a terminal tombstone, including for workers that exit
before attach.

Exceptional recovery is delegated to
`/usr/local/libexec/orchestration-recovery-authority`, which must be owned by a
root, be non-writable by group/other, and be exposed to the runtime user only
through the narrow sudo policy installed by
`scripts/install-operator-authority.sh`. It atomically consumes a
scope-bound token. If that helper is absent or unsafe, override is disabled;
there is deliberately no repository, home-directory, or same-UID secret.

## Operator continuations

Repository settings can tighten the built-in `$20` ticket pause but cannot
relax it. When a reviewed ticket should receive a bounded continuation, root
issues an expiring capability for an **absolute** total ceiling and pipes it
directly into the controller so it does not appear in shell history:

```text
sudo /usr/local/libexec/orchestration-recovery-authority issue-budget \
  --repository /absolute/repo --ticket PROJ-123 --ceiling-usd 35.08 \
| python3 /absolute/plugin/scripts/sprint-controller.py grant-budget \
  --sprint 65 --ticket PROJ-123 --operator-capability-stdin
```

The active grant changes only the ticket pause and ticket dollar ceiling. It
does not relax per-run or sprint budgets, run-count/reviewer-count breakers,
concurrency, review gates, or merge policy. `revoke-budget` removes it early;
otherwise it expires automatically.

When one ticket has exhausted its normal launch count but a bounded additional
attempt is justified, root can issue a separate capability with an **absolute
total-attempt ceiling**. For example, `--ceiling-attempts 4` permits attempts 1
through 4; it does not add four more attempts:

```text
sudo /usr/local/libexec/orchestration-recovery-authority issue-relaunch \
  --repository /absolute/repo --ticket PROJ-123 --ceiling-attempts 4 \
| python3 /absolute/plugin/scripts/sprint-controller.py grant-relaunch \
  --sprint 65 --ticket PROJ-123 --operator-capability-stdin
```

The active grant applies only to that repository and ticket. It does not alter
budgets, model/reviewer run-count breakers, dependencies, concurrency, review
gates, or merge policy. `revoke-relaunch` removes it early; otherwise it expires
automatically. A terminal ticket still requires the separate recovery flow
below before it can return to `pending`.

A terminal checkpoint that lost its attempt token or execution-unit identity
also requires a separate one-shot, attempt-bound capability:

```text
sudo /usr/local/libexec/orchestration-recovery-authority issue-recovery \
  --repository /absolute/repo --ticket PROJ-123 --attempt 2 \
| python3 /absolute/plugin/scripts/sprint-controller.py recover-terminal \
  --sprint 65 --ticket PROJ-123 --reason 'verified stopped; preserved worktree' \
  --operator-capability-stdin
```

`blocked`, `external_blocked`, `operator_decision`, and legacy `user_action`
entries can use terminal recovery after their cause is resolved. The command
records the reason, consumes the capability exactly once, clears stale launch
identity, and returns the ticket to `pending`; the next normal `plan`/`reserve`
creates a fresh fenced attempt.

`concurrency_max` is a ticket-lane limit. The host separately admits local
builds, full test suites, and browser runs under `max_heavy_processes`; model
lanes waiting on providers do not justify oversubscribing those local commands.

### Native worker spending and supervision

A model-less OpenAI desktop route is the subscription-backed alternative. Set
`provider: openai` and leave `model: ""`. The controller launches Codex with its
configured default model, removes inherited API credentials and alternate base
URLs, binds it to verified ChatGPT authentication, and does not insert the
metered gateway. These turns cannot contribute
provider receipts to Orka's USD/token ledger. The execution unit, lane and
review counts, no-progress milestones, worker lifetime, repository lease, CI,
and merge gates remain authoritative.

A direct `launch-local -- .../claude ...` launch now runs through a controller-owned
loopback Messages gateway. It requires `ANTHROPIC_API_KEY` in the controller's
environment or the configuration directory's `.env`, and explicit pricing for
**every requested model**, including nested workers and fallback models. The
child receives a temporary gateway credential; nested Claude processes inherit
that endpoint. Each message reserves counted input plus capped output against
the same run, ticket, and sprint ledger used by API workers. Ambiguous submissions
retain their reservations. Responses are buffered until usage has been settled,
then returned as JSON or Messages SSE.

For direct Claude launches, the supervisor merges inline or file-based
`--settings` with controller-owned routing environment values. These final values
override user/project endpoints, credentials, and alternate-provider selectors;
unrelated settings and configured setting sources remain enabled. Invalid JSON
settings are rejected before launch. Optional cache usage counters must be
nonnegative integers before settlement; malformed responses retain their request
reservation without writing invalid usage to the shared ledger.

The gateway supports standard Anthropic Messages and custom client tools.
Provider-hosted paid tools, premium tiers, and models without configured pricing
are rejected. Configure cache-write rates to cover the cache lifetimes in use.
This adapter has offline contract coverage; a live Claude compatibility smoke test
is still required before production rollout.

A direct `launch-local -- .../codex exec ...` launch uses a controller-owned
Responses gateway with `OPENAI_API_KEY` and explicit model pricing. Each request
counts input and reserves capped output against the shared budgets before
submission; terminal usage is settled before the buffered response is returned
as Responses SSE. Missing usage or uncertain submissions retain reservations.
The worker receives a temporary credential and per-command provider settings;
no persistent Codex configuration is changed. A PATH launcher keeps ordinary
nested `codex exec` calls on the same gateway and ticket allowance. Caller
provider-routing overrides are rejected. Supply a configured model explicitly.

The Codex adapter supports stateless standard-tier Responses with local function
and custom tools, namespaces, and client-executed tool search. It rejects hosted
paid tools, remote compaction endpoints, stateful continuation, background responses,
premium tiers, and unpriced models. Local compaction uses ordinary metered
Responses requests. Model discovery is not provided. Installed-client checks
exercise a local-tool round trip and forced local compaction; authenticated
readiness probes verify the connection without claiming a live generation test. Launch separate metered API
reviewers from the credential-owning controller, not from this worker's temporary
credential environment. Native children share the implementation phase allowance.

Neither adapter meters subscription sessions, shell-wrapped initial launches,
absolute nested commands that bypass the launcher/configuration, or arbitrary
programs using another endpoint. These gateways enforce spending for cooperative
clients; they are not an OS sandbox. Use the API runner for unsupported workflows.

Every local supervisor applies an inactivity timeout (`max_worker_idle_seconds`,
default 1800, maximum 7200) and a total lifetime (`max_worker_lifetime_seconds`,
default 14400, maximum 43200). Output growth and newly verified controller
milestones reset inactivity, not total lifetime. The legacy `max_worker_seconds`
setting remains supported and retains its original 3600-second hard maximum.
The supervisor also checks settled spending since verified progress against
`max_usd_without_progress`. It sends TERM, then KILL to the worker's process group
when a guard trips, preserves its terminal record, and moves only the matching
running attempt to recovery or the decision queue. A supervisor exception also
cleans up its process group. Existing containment requirements for automatic
requeue still apply.

`record-progress --milestone implementation_commit --evidence <full-sha>` verifies that
the commit descends from launch HEAD and changes its tree. Replaying a milestone
and evidence pair is idempotent. `design_passed` accepts the canonical review ledger file and requires a consumed
PASS receipt bound to this ticket. Only verified events reset the spending baseline;
other milestone reports remain informational until their receipt validators are
implemented. No progress event grants a review or merge approval.

A time-limited attempt that recorded a new verified milestone may be requeued as
a continuation. Continuations have their own `max_worker_continuations` counter
and do not spend the ordinary crash/relaunch allowance. A timeout with no new
verified milestone remains an ordinary charged attempt. Planning is finish-first:
actionable repair and recovery work pauses fresh launches, and `max_unmerged_prs`
(default `concurrency_max`) bounds unfinished PR work in progress.


`record-progress --milestone review_finding_closed --evidence
'{"ledger":".orchestration/.review-ledger/<ledger>.json","finding":"<stable-id>"}'`
verifies a finalized independent repair review for this ticket. Every gate claim
on the finding must be resolved, and no repair review may still be pending. A
self-reported repair or one gate's approval cannot reset the watchdog while
another gate still has an open claim. Closing the same finding again does not
create a second progress credit. Failing-test reports remain informational until
a controller-executed test receipt is available.


### Verified PR and CI progress

Record a verified implementation commit before reporting its PR:

```sh
python3 /absolute/plugin/scripts/sprint-controller.py record-progress \
  --sprint 65 --ticket PROJ-123 --attempt-token "$ATTEMPT_TOKEN" \
  --milestone implementation_commit --evidence "$HEAD_SHA"
python3 /absolute/plugin/scripts/sprint-controller.py record-progress \
  --sprint 65 --ticket PROJ-123 --attempt-token "$ATTEMPT_TOKEN" \
  --milestone pr_opened --evidence 123
python3 /absolute/plugin/scripts/sprint-controller.py record-progress \
  --sprint 65 --ticket PROJ-123 --attempt-token "$ATTEMPT_TOKEN" \
  --milestone ci_advanced --evidence 123
```

PR and CI evidence accepts a positive PR number or its canonical HTTPS URL.
The controller uses authenticated, read-only `gh api` requests against the
repository's `origin`. GitHub resolves renamed repositories; the immutable
repository ID binds the PR. Its head must match a verified implementation commit
for this ticket. The first PR receipt also records the branch and canonical PR
URL for recovery. Existing bindings cannot silently switch to another PR or branch.

CI observations exhaust both check-run and commit-status pagination for that
exact head, then recheck the PR head. Waiting alone earns no credit. A check can
advance through running, terminal, and successful states; each forward step earns
at most one credit for the code tree and check identity. A terminal failure counts
as an observed CI transition, not a passing gate. Repeated failures, backwards
transitions, reordered responses, and reruns with new execution IDs do not reset
the spending baseline. Commit-message-only amendments preserve the same tree's
CI progress while retaining the newly verified commit SHA for PR binding.

Lookups run outside the sprint checkpoint lock. Before writing a receipt, the
controller rechecks the ticket and attempt; a concurrent change invalidates the
observation. API errors, incomplete evidence, or a changing PR grant no credit.
These receipts never grant review approval or bypass exact-head merge checks.

Provider contracts: [GitHub pull requests](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request),
[check runs](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference),
and [commit statuses](https://docs.github.com/en/rest/commits/statuses#list-commit-statuses-for-a-reference).

### Controller-executed test progress

Configure named test commands in the shared orchestration configuration:

```yaml
progress_tests:
  unit:
    command: ["python3", "scripts/run-unit-junit.py"]
    timeout_seconds: 120
```

The command must write fresh JUnit XML to the absolute path in
`ORKA_TEST_REPORT`. The example script name is a repository-owned runner, not a
bundled script. Commands are argument arrays and run without an implicit shell.
Each run uses a temporary detached Git worktree at the requested full commit,
which must descend from the ticket's launch baseline. Dependencies must already
be available to the command; this runner does not install them. Uncommitted edits
in the agent's worktree are excluded. The timeout defaults to 120 seconds and may
be configured from 1 through 600 seconds. Descendant processes are killed when
the command exits or times out, and the temporary worktree is removed.

Invoke `record-progress` with the current attempt token, milestone `failing_test`
or `tests_repaired`, and evidence such as:

```json
{"check":"unit","commit":"<full-commit-sha>"}
```

The controller runs the configured command itself. An exit code of 1 must match
explicit JUnit failures; an exit code of 0 must have none. Empty reports, duplicate
class/name identities, runner errors, missing reports, tracked-file mutations,
and timeouts grant no progress. Reports are limited to 8 MiB. Each class/name
identity can advance once from unobserved to failed, then once to repaired on a
later descendant commit with a different tree. Deleted or skipped tests cannot
claim repair. Repeating runs, reopening a repaired failure, and amending only a
commit message do not reset the watchdog. The receipt retains the command
identity, commit/tree, report hash, case outcomes, and credited transitions.
Temporary report and console files are discarded after observation.

Test execution holds no sprint lock. The controller rechecks ticket state and
attempt identity before accepting the result. These receipts establish observed
test progress for configured, cooperative repository runners; they do not prove
that a test is well designed or that its assertions were preserved. They grant
no design, review, CI, or merge approval. The runner is not an OS sandbox and
inherits the controller environment; configure local test commands accordingly.

### Cooperative recovery and unused design capacity

`cooperative_auto_recovery: true` opts a repository into automatic requeue of
cooperative workers on macOS or Linux without a systemd scope. The setting must
have been enabled at launch and remain enabled at recovery. The controller
requires the matching normal supervisor terminal receipt, completed gateway
shutdown, an absent supervisor, and an absent worker process group. Missing or
ambiguous cleanup, supervisor crashes, live descendants, and legacy identities
still require external recovery authority. This contract assumes workers do not
daemonize or escape their session; it does not weaken isolated-worker checks.
The starter configuration leaves this explicit cooperation contract disabled.
Branch and PR identities survive requeue.

A verified `design_passed` receipt now transfers unused design allowance to
implementation atomically. Open design reservations defer the transfer; replay
the same verified receipt after reconciliation to retry. A ticket transfers once,
and cannot spend the transferred allowance on further design. Code/security
allowances, the total phase allocation, and run/ticket/sprint ceilings remain
unchanged. Later configuration reductions cannot create extra capacity. A design
change after approval must be rescoped rather than repeatedly borrowing budget.
Phase summaries show the effective transferred limits.

### Outcome reporting

`summary` includes `outcome_metrics`: attempted tickets, reported completion rate,
settled spend per reported completion, recorded state durations, and entries into
operator-decision states. These are checkpoint observations, not GitHub merge
proof or a count of UI notifications. Missing history remains unknown.

`report-outcomes --sprint <id> --verify-merges` additionally queries GitHub for
completed tickets, verifies repository/PR/branch identity and merged status,
and reports confirmed merge receipts and spend per unique merged PR. Multiple
tickets sharing a PR do not duplicate the merge denominator. Lookup errors are
reported separately. Without `--verify-merges`, merge metrics remain unknown.
The command also lists repeated findings from matching canonical review ledgers.
Use these metrics to compare sprint outcomes before raising limits.

Decomposition slices now require `migration_owner` (a slice ID or `none`) and a
nonempty `test_plan`; both are included in generated Jira descriptions. Older
assessments without these fields must be rescoped before creating children.

Installed native CLI compatibility has now also been exercised offline with
Claude Code 2.0.30 (including its auxiliary Haiku request) and Codex 0.147.0.
These tests use local mock providers and temporary credentials; they do not
establish live billing accuracy. Native Claude children discard inherited OAuth
and alternate-provider selectors. Manual thinking budgets are capped below the
output envelope. Configure prices for auxiliary models as well as the main model.

## Restart allowances and legacy reconciliation

A restart is a bounded operator continuation, not deletion of a ledger. The
root-owned authority supports `issue-restart`, `activate-restart`,
`restart-grant`, and `revoke-restart`. Upgrade the host helper and its sudoers
policy with `scripts/install-operator-authority.sh` before using this feature;
old helpers fail closed on the new command. Runtime users may activate/query a
grant, but issuance and revocation are never added to their sudoers allowlist.

After inspecting the ticket's lifetime spend, phase spend, attempts, model runs,
design rounds, review rounds, and repair cycles, the operator creates a JSON
allowance file. Every ceiling is an **absolute lifetime total**, not an increment.
For example (choose values from the actual ledger, not this example):

```json
{
  "attempts": 6,
  "model_runs": 30,
  "review_runs": 14,
  "design_rounds": 8,
  "code_rounds": 7,
  "security_rounds": 7,
  "repair_cycles": 8,
  "ticket_usd": "70",
  "design_usd": "20",
  "implementation_usd": "30",
  "code_review_usd": "10",
  "security_review_usd": "10",
  "progress_baseline_usd": "15"
}
```

`progress_baseline_usd` acknowledges historical ticket spending for the new
watchdog interval. It must not exceed recorded spending. Only the successfully
applied, still-live restart grant can supply this baseline. Ordinary retries,
requeues, syncs, or checkpoint fields cannot reset the watchdog. Admission checks
run before reservation, so an already-exhausted watchdog does not consume a launch.

```text
sudo /usr/local/libexec/orchestration-recovery-authority issue-restart \
  --repository /absolute/repo --ticket PROJ-123 \
  --allowances /operator/reviewed-allowances.json \
  --reason "Approved bounded continuation after reconciliation" \
  --expires-hours 24 \
| python3 /absolute/plugin/scripts/sprint-controller.py restart-ticket \
  --sprint 65 --ticket PROJ-123 --operator-capability-stdin
```

The checkpoint must already contain the ticket. Restart refuses running,
completed, or decomposed tickets, unknown execution identities, and outstanding
provider reservations. Reconcile those first. An existing PR routes to repair;
its branch, execution fence, findings, failed reviews, and historical counters
remain intact. A preserved scoping/product decision remains an operator decision.
The grant does not authorize a merge, change Jira readiness, satisfy dependencies,
or alter per-run and sprint-wide budgets. Review caps are evaluated against the
live grant rather than overwritten in the review ledger, so expiry/revocation
restores normal enforcement. Re-running `restart-ticket` without a token can
finish applying an already-activated grant after a crash; applying the same grant
twice does not reset state again. A new grant replaces the previous restart grant.

`plan.legacy_reconciliation` identifies old `blocked`/`user_action` tickets with
concrete next actions: refresh inventory, inspect a preserved PR, reconcile
existing children, classify an old worker outcome, or verify Jira readiness.
Investigate these using current Jira/PR evidence, not free-text guesses. Use
`reconcile-legacy --sprint ID --ticket KEY --classification operator_decision|external_blocked
--reason "verified evidence"` to replace opaque non-launching labels while
retaining the original report. Recovery/restart and authenticated decomposition
bindings remain distinct operations. Never duplicate an existing child chain.

Untouched tickets that disappeared from a query are refreshed automatically if
an authenticated sync includes them again. The sync preserves removal/return
history, worker outcomes, scope decisions, and execution evidence.

## Provider startup failures and completion reporting

A controller-owned native gateway distinguishes upstream rate limits (HTTP 429)
from local budget refusal (HTTP 402). Only a stopped, fenced launch with explicit
upstream rejection, no outstanding reservations, and no accepted or uncertain model
work qualifies for a startup retry credit. A ticket receives at most **two** such
credits on its normal physical launch ceiling; attempt numbers and fencing tokens
are never reused or decremented. An explicit relaunch/restart ceiling is still an
absolute total, compared against that bounded normal allowance, not increased by
the number of failures. Token-count rejections qualify even when no paid reservation was created. Unknown failures receive no credit.

`plan.retry_waiting` gives a 30-second cooldown deadline for a qualifying native
startup failure. Waiting tickets keep `autonomous_work_remaining=true` without
occupying a worker lane. Continue independent work and replan at the deadline.
This is not an unbounded provider retry loop. Reservations and per-ticket budgets
are still enforced on every paid request.

Summary `finished` retains its old controller-drained meaning for compatibility.
Use `autonomous_work_exhausted` for that condition and `sprint_complete` for actual
completion of all checkpointed tickets (including verified child bindings).

## Provider-aware admission (1.3.0)

The production controller requires a recently verified resolved worker route
before `reserve` and batch preparation, and binds that route into the reservation. `launch-local`
rechecks health and rejects a different provider, model, profile, or route.
Batch submission also rechecks the prepared provider/model route.
A local settings mistake therefore does not create an execution unit. Old
reservation-only records without a route require reconciliation; they are not
silently upgraded into launch authority.

Run `captain-preflight.py --plugin-root <installation> --repo <repository>
--host codex|claude --verify-runtime`. `installation_status: ready` only proves
files/config exist. `execution_ready: true` requires bounded installed-client
checks and authenticated token-count probes for explicit-model routes.
Repositories may set `minimum_orka_version` to a stable release such as `1.5.2`;
preflight then fails closed before provider checks when the active plugin is
older. Local cachebuster suffixes do not change release ordering.
Model-less desktop subscription routes instead verify that the selected client
is installed and executable without contacting a provider endpoint. Codex must
also report that it is logged in through ChatGPT. Model-less Claude routes are
unsupported because Claude Code lacks reliable subscription-auth evidence.
Probe credentials use the same environment precedence as the runtime; an invalid
process-environment key will still override a corrected repository `.env`.
Never print credential values. No credential is changed by these commands.

Native Codex launches are also bound to a controller-selected checkout. For a
preserved-PR recovery, the controller replaces every caller-provided `--cd`
with the authenticated recovery worktree and passes the same path separately to
the supervisor. The supervisor proves that checkout shares the managed
repository's Git common directory and starts the child there. An unrelated or
unverifiable checkout fails before the worker process starts.

`health-check --role sprint-worker` probes the configured route without a ticket
reservation. Native OpenAI/Anthropic clients first run against a loopback mock;
Codex must execute a tool, compact, and finish with every request metered. Then
an authenticated token-count request checks the provider/model connection.
Other provider adapters report unsupported probe capability rather than claiming
unverified readiness. No real model generation occurs in these health checks.

Provider state is shared across worktrees in `.orchestration/.provider-health`.
A 401/403 is a persistent authentication hold. A 429/529 immediately blocks new
admissions to that provider across tickets; independent healthy providers remain
available. Existing API requests may finish their already-bounded retry sequence
with the same idempotency key, but cannot clear the shared hold on success.
Native startup credits do not authorize an unhealthy provider.

`plan.health_probes` includes deadlines. Only one probe per provider can run at a
time; leases expire after 60 seconds and results cannot overwrite a newer
incident. Transient incidents allow at most three failed automatic probes.
Authentication/client holds need actual repair and an explicit
`health-check --role <role> --after-repair`. Successful probes expire after five
minutes. `plan.provider_holds` keeps these issues distinct from ticket decisions.

Admission scoping is mandatory even when automatic decomposition is disabled.
The authenticated inventory now includes sanitized descriptions and extracts
explicit `Prerequisites:`, `Dependencies:`, `Depends on:`, and `Blocked by:`
sections, including following bullet lists. Unrelated ticket references do not
create edges. External prerequisite statuses are fetched through the existing
Jira adapter. Arbitrary prose still requires the bounded scoper to enumerate
`prerequisites`; a `ready` assessment missing scheduler relationships becomes a
dependency-reconciliation decision. An authenticated sync that supplies those
missing edges automatically returns this specific decision to scoping; unrelated
operator decisions remain fenced. Configuration does not authorize automatic
Jira link edits. Do not guess direction or delete existing dependency edges.

A `tracking_parent` scope result with `children` exactly matching authenticated
subtasks binds the existing chain as `decomposed`, without implementation attempts
or new Jira children. Use this only when children own all parent acceptance
criteria. Changed description, summary, dependencies, or subtasks invalidate a
pending scope assessment on the next sync.


## Anthropic compatibility and failure reporting (1.3.1)

The interactive controller host does not select the worker provider. A Codex
captain with an Anthropic desktop worker route launches Claude through the native
gateway. For supported `context_management` edits, the credential-owning HTTP
transport derives the required context-management beta header; it does not
forward arbitrary client headers. Supported edits clear tool results or thinking
blocks. Server-side compaction is rejected because it lacks gateway accounting.
See [Anthropic context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing).

The first stopped-gateway failure retains its original exception classification
and upstream HTTP status. Only an actual local `BudgetError` becomes a local 402.
A compatibility rejection involving `context_management` holds the provider;
repair and an explicit readiness probe are required before more ticket launches.

## Provider response timeouts (1.3.2)

`llm.budgets.provider_read_timeout_seconds` independently bounds each upstream
model response. It defaults to 900 seconds for both direct API agents and the
native Claude admission gateway. `tool_timeout_seconds` continues to govern only
repository tool commands and does not shorten model generation.

The native gateway currently obtains a complete, non-streaming provider response
before returning Anthropic-compatible streaming events to Claude Code. Large
contexts, high reasoning, and large output caps can therefore legitimately take
longer than two minutes without producing an upstream byte. The provider timeout
remains finite and the worker supervisor provides an additional outer lifetime
bound. If the timeout does expire after submission, Orka keeps the reservation
for reconciliation and does not blindly retry potentially billed work.
