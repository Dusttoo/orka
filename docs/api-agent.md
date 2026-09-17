# API agent runner

`scripts/api_agent.py` is the execution adapter behind an `llm.execution: api`
route. Claude Code or Codex Desktop can remain the interactive controller: it
builds the role payload, launches this runner as a child process, reports its
state, and asks the user for any required approval. The worker model traffic is
billed to the configured Anthropic, OpenAI, Azure Direct Model, Amazon Bedrock
Runtime, or Amazon Bedrock Mantle account rather than the desktop agent surface.

## Run one role

Build the ordered payload and pipe it directly to the runner:

```text
scripts/context_pipeline.py payload \
  --config .orchestration/config.yaml --role code-reviewer \
  --role-file /plugin/agents/orchestration-code-reviewer.md \
  --rules-file AGENTS.md --repo-map .orchestration/repo-map.txt \
  --ticket .orchestration/ticket.json --diff .orchestration/review.diff \
  --mode code-review --execution gate \
| scripts/api_agent.py run --request - \
    --config .orchestration/config.yaml --role code-reviewer \
    --ticket PROJ-123 --run-id PROJ-123-code-review-1
```

Put repository-specific provider credentials in `.orchestration/.env`, beside
`config.yaml`:

```dotenv
ANTHROPIC_API_KEY=your-anthropic-key
OPENAI_API_KEY=your-openai-key
AZURE_ADM_API_KEY=your-azure-resource-key
AZURE_ADM_BASE_URL=https://your-resource.openai.azure.com/openai/v1
```

Only define the provider the repository uses. Optional custom endpoints are
`ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL`; `AZURE_ADM_BASE_URL` is required for
Azure Direct Models. `ANTHROPIC_BASE_URL` accepts either a host URL or a URL
already ending in `/v1`; the runner normalizes both to the versioned Messages
API path. The Azure `model` route value is the deployment name. The
runner parses this file as data; it does not execute shell syntax or expand
variables. Only the documented provider names are loaded. Variables already
supplied by a cloud container or host environment take precedence, making platform secret injection
the highest-priority source.

Always gitignore `.orchestration/.env`. Keys are never copied into
`config.yaml`, run state, logs, or the usage ledger.

Bedrock does not use a repository credential. Install its optional runtime
dependency into the Python environment that launches the controller:

```text
python3 -m pip install -r /path/to/plugin/requirements-bedrock.txt
```

The adapter uses the default AWS credential chain and reuses one
`bedrock-runtime` client. EC2 deployments should receive `bedrock:InvokeModel`
through the instance role. Claude 5 and GPT-5.6 do not support CountTokens on
this endpoint, so preflight budget reservations use a conservative local
estimate and exact response usage settles the ledger. GPT-5.6 routes also
require access to the account's default Bedrock project. The repository `.env` parser
does not accept AWS profile, region, or credential variables, preventing a repo
from selecting a different AWS identity. Set `AWS_REGION` at the host/session
level instead.

Bedrock Mantle uses the same dependency and AWS credential rules. The adapter
calls the regional `bedrock-mantle` OpenAI-compatible endpoint and signs every
request with SigV4 using the EC2 instance role. The role needs
`bedrock-mantle:CreateInference` for the configured model/project. Mantle Chat
Completions has no separate token-count endpoint, so the runner reserves a
conservative local estimate before submission and settles against exact response
usage. Do not put a Mantle API key or AWS credentials in `.orchestration/.env`.

The runner retries explicit provider rate-limit rejections independently from
other pre-ack failures. For HTTP 429 it honors Azure's `retry-after-ms` or
`Retry-After` header. If neither is present, it uses bounded exponential backoff
with jitter, up to `max_rate_limit_retries` and
`max_rate_limit_wait_seconds`. Known-not-accepted 429 requests reuse the same
reservation and client request id. Explicit overload rejections use the smaller
`max_pre_ack_retries` policy. The runner never retries a model submission
timeout or another ambiguous transport/server outcome.

`llm.budgets.provider_read_timeout_seconds` controls how long Orka waits for one
upstream model response and defaults to 900 seconds. This is separate from
`tool_timeout_seconds`, which applies only to repository commands. The longer
provider window is especially important for non-streaming native Claude turns
with large contexts, high reasoning, or large output caps. If the provider
window still expires after submission, Orka preserves the reservation for
reconciliation instead of submitting a potentially duplicate billed request.

Rate-limit retries preserve a ticket lane instead of losing its checkpoint, but
they do not create throughput. Size TPM for the configured concurrency and keep
`max_completion_tokens` close to expected output because Azure may include the
maximum output allowance in its rate-limit estimate.

## Role-limited tools

Review roles receive only bounded file reads, exact-string search, raw Git diff,
Git status, and named repository checks. The implementer and sprint-worker roles
may additionally apply a text-only unified patch. No role receives an arbitrary
shell tool. `run_check` can invoke only commands already named in `self_check` or
`verification`; tool output, line reads, paths, rounds, and execution time are
bounded.

`llm.roles.<role>.allowed_tools` may narrow that role's built-in ceiling. It
cannot grant a reviewer write access or name an unknown tool.

Anthropic tools intentionally omit the provider's `strict` flag because the
tool schemas use standard constraints outside Anthropic strict mode's accepted
subset. The tool executor remains the security boundary and independently
enforces path containment, numeric ranges, list sizes, patch size, allowed
checks, timeouts, and output limits.

The runner is intentionally text/code-only. Keep `visual-qa` on a desktop route
until a separately sandboxed image/browser adapter is configured; an API visual
role fails closed instead of pretending a text-only review inspected the UI.

## Hard budget enforcement

Before each model request, the runner calls the provider's input-token counter.
It atomically reserves the counted input at the most expensive configured input
rate plus the request's full output allowance. The reservation lock is held on
the shared ledger, so lanes running in separate worktrees serialize against each
other rather than against private copies of the limit. Reservations are included in
run, phase, ticket, and sprint checks, preventing concurrent workers from racing past a
shared limit. A request that could exceed any configured ceiling is not sent.

Ticket controls are layered: `warn_usd_per_ticket` records an event without
stopping work, `pause_usd_per_ticket` is a hard operator-action stop, and
`max_usd_per_ticket` remains the hard ceiling. Unique run IDs are bounded by
`max_model_runs_per_ticket`. Independent post-implementation code/security run
IDs are separately bounded by `max_reviewer_runs_per_ticket`; design attempts
use the durable `max_design_rounds` ledger and do not consume that later gate
capacity. Tool rounds and reconciled provider continuations inside one stable
run ID do not consume extra run slots. Repository configuration may
tighten the compiled incident ceilings but cannot disable them. The only values
it can raise are the design, code-review, and security-review phase envelopes,
and only up to their $10 hard cap (see [Phase spending envelopes](#phase-spending-envelopes)).
Values above a hard cap are clamped; `orchestration-engine.py validate-config`
prints a `WARNING` for each one. There is deliberately no same-user CLI approval
bypass.

### Host budget policy

The compiled hard caps suit small repositories. A host that runs larger sprints
can raise them for one repository with a standing, root-owned budget policy held
by the same authority that issues ticket grants
([Standing budget policy](sprint-controller.md#standing-budget-policy)):

- The policy may raise `max_usd_per_run`, `max_usd_per_ticket`,
  `max_usd_per_sprint`, `pause_usd_per_ticket`, the four phase envelopes,
  `max_model_runs_per_ticket`, `max_reviewer_runs_per_ticket`, and the
  controller's `max_usd_without_progress`. Output-token and tool bounds are not
  policy values.
- It only raises. Each hard cap becomes the larger of its compiled value and
  the policy value.
- Repository configuration still chooses values within the caps. A policy that
  allows `max_usd_per_ticket: 400` takes effect only when `llm.budgets` asks for
  it, and values the configuration omits keep their defaults. When the ticket
  ceiling is raised, an omitted pause defaults to 75% of it (within the pause
  cap), and an omitted warning to half the pause.
- Only root can set or clear a policy. The runtime user's sudo rule can only
  read it, and nothing in the repository or worktree can change it.
- If the policy cannot be read (no authority installed, an older helper without
  `budget-policy`, a sudo refusal, or a malformed response), the compiled caps
  apply. That can only lower spend. `validate-config` prints why, and captain
  preflight reports it under `budget_policy`.
- Every limit derived from configuration, including the ledger's phase clamp,
  resolves against the same repository's policy, so admission never uses a cap
  higher than the policy allows.

With `issue-budget`, a host operator may authorize one ticket to continue to an exact absolute
ceiling with the separately installed root authority. This raises only that
ticket's cost pause and hard ticket-cost ceiling; model-run and
post-implementation-reviewer-run,
per-run, phase, sprint, and provider breakers remain unchanged. The grant expires and
cannot be created from repository configuration or by the runtime user. The
sprint controller activates it with `grant-budget`; API reservations query the
active grant before every request and stop again at the granted ceiling.

API reviewer runs require a review-ledger phase permit bound to the ledger's
immutable repository work subject, role, PR/design ledger, and full exact commit:

```text
scripts/review-ledger.py permit-review 123 \
  --role code-reviewer --head "$(git rev-parse HEAD)"
# pass the returned token to api_agent.py run --review-pr 123 \
#   --review-authorization <token>
```

Implementer and sprint-worker API routes also require `--attempt-capability`
and `--worker-ref` exactly as returned and bound by `sprint-controller.py
reserve`. Reviewer output is usable only after the API runner creates a
digest-bound completion receipt; native reviewers use `review-ledger.py
complete-review` after writing their structured result.

### Reviewer turns and the final verdict turn

Design, code, and security reviewer turns submit, and therefore reserve, at most
`max_output_tokens_per_review_turn` output tokens (default 8192, never above
`max_output_tokens_per_turn`). Implementer turns keep the full
`max_output_tokens_per_turn` allowance. Every reservation is still the worst
case of the exact request submitted.

The first reviewer request starts with an `ORKA BUDGET NOTE`: the phase ceiling,
what the phase has already spent or reserved, the remaining capacity across the
configured phase, run, ticket, and sprint ceilings, the per-turn output
reservation, and the tool-round limit. The note does not query host budget
grants, so it can understate capacity but never overstate it; admission remains
authoritative.

When the next tool continuation is refused by a dollar ceiling, or
`max_tool_rounds` is reached, the reviewer gets one final verdict turn instead
of losing its work:

- The request keeps the transcript, disables tools (`tool_choice` none; Bedrock
  Converse cannot express this, so a tool call there fails closed), appends an
  instruction to return the final structured review JSON from the evidence
  gathered, and caps output at `final_verdict_output_tokens` (default 4096, never
  above the review turn bound).
- At the tool-round limit, pending tool calls are answered as not executed.
- The request is admitted like any other. If it does not fit either, the run is
  `budget_blocked` as before.
- A FAIL is validated and receives a completion receipt exactly like a normal
  review, so its blocking findings reach the repair brief. A PASS is never
  authoritative: it may rest on partial evidence, so the run ends
  `budget_blocked`, keeps the verdict in its state for inspection, creates no
  receipt, and releases the permit for a fresh review under a larger budget.
  The run state and result record `final_turn` (`budget` or `max_tool_rounds`)
  and `final_turn_reason`.
- There is never a second final turn. A final turn that returns tool calls is
  `budget_blocked`, and the review permit is released.

After every response, actual uncached input, cache writes, cache reads, output,
and reasoning usage is recorded under `.orchestration/.llm-usage/usage.jsonl`.
Run `scripts/api_agent.py usage` for totals and open reservations.

### One ledger per repository, not per worktree

Spend accounting resolves to the root shared by a repository and all of its git
worktrees (`git rev-parse --git-common-dir`), never to the lane's own checkout.
Ceilings are only real if every concurrent lane counts against the same ledger;
a worktree-relative ledger would silently multiply `max_usd_per_ticket` and
`max_usd_per_sprint` by the number of running lanes. Run markers under
`.orchestration/.llm-runs/` share that root for the same reason, so an uncertain
request stays reconcilable after its worktree is cleaned up.

Tool execution is unaffected and stays sandboxed to the lane's own worktree.

Runtime state always resolves from Git's common directory. Environment overrides
are intentionally ignored because selecting a fresh directory would reset every
shared enforcement counter.
Outside a git
repository the ledger falls back to the `--repo` directory.

### Reading the ledger

`scripts/api_agent.py usage` prints lifetime totals. `report` groups the same
events into cost and performance insights:

```text
scripts/api_agent.py report --group-by role --since 7d
```

```text
role                      reqs   runs     cost_usd      in_tok    cache_rd    cache_wr    out_tok   cache_hit    p50_ms    p95_ms
---------------------------------------------------------------------------------------------------------------------------------
implementer                 34     12      $2.3470      176.0k       2.58M      175.9k     103.9k       88.0%      3997      5953
code-reviewer               12      4      $0.7970      129.5k      806.9k       59.8k      22.7k       81.0%      2537      3867
---------------------------------------------------------------------------------------------------------------------------------
TOTAL                       46     16      $3.1440      305.5k       3.39M      235.7k     126.6k       86.4%      3736      5953
run outcomes: completed 44, budget_blocked 1, invalid_output 1
```

- `--group-by` accepts `role`, `model`, `provider`, `ticket`, `sprint`,
  `run_id`, or `day`. Groups sort by cost, except `day`, which sorts by date.
- `--since` and `--until` accept `30m`, `24h`, `7d`, `2w`, or an ISO 8601
  timestamp. A timestamp without a zone is read as UTC.
- `--role`, `--model`, `--provider`, `--ticket`, and `--sprint` narrow the
  window before grouping. `--top N` keeps the N costliest groups; TOTAL still
  covers every group, and the table says how many were hidden.
- `--format json` emits the same report for a dashboard or a checkpoint.
- Open reservations follow the same filters and window. When other open
  reservations exist, the report shows the ledger-wide count separately
  (`open_reservations_ledger_wide` in JSON).

`cache_hit` is the share of billed input served from cache
(`cache_read / (input + cache_read + cache_write)`). It is the fastest signal
that prompt caching is working: a role whose stable prefix is being rebuilt each
launch shows a hit rate far below its neighbours.

`run outcomes` counts run states from `.orchestration/.llm-runs/`. A rising
`invalid_output` or `budget_blocked` count is a quality signal that a role's
model or ceiling needs attention, not just a cost signal.

Latency measures the accepted provider request only. Time spent sleeping on
429 backoff is reported separately so blocked time never hides inside it.
Ledger entries written before latency capture existed are counted in cost and
tokens and excluded from percentiles; the table reports how many. Model prices
are explicit configuration because guessing the price of an unknown or newly
released model would make a USD limit unsafe. Update and verify the model entry
whenever its provider pricing changes.

## Submission recovery

Run markers are written atomically under `.orchestration/.llm-runs/`. The runner
does not blindly retry a timeout or ambiguous server failure: the original
request may already be running and a retry could duplicate writes and billing.
Its worst-case reservation remains open and the state becomes
`needs_reconcile`.

Check the provider dashboard or API records, then reconcile with evidence:

```text
scripts/api_agent.py reconcile --run-id RUN_ID --outcome not-found \
  --evidence "provider request search and timestamp"
```

If the provider completed the request, use `--outcome completed`, its response
id, and the provider-reported token counts. This settles actual cost, but the
controller must still inspect the recovered result before advancing workflow
state. Never release an uncertain reservation merely to make budget available.

A reviewer run records its review permit binding (PR, role, exact head, ledger
directory, logical review id, and a SHA-256 digest and short prefix of the
token, never the token itself) in its run marker. Reconciling that run closes
the money first and then cancels the started permit, reported under
`review_permit` in the output and `review_permit_reconciliation` in the run
state. Both outcomes cancel: a `completed` reconciliation settles cost but has
no validated structured review or completion receipt, so it can never yield a
PASS and the review must be re-run under a new permit. A money failure leaves
the permit untouched. A permit that is already cancelled, superseded, or
completed is reported without failing reconciliation, and rerunning after a
crash is safe. `reconcile-reservations` cancels bound permits the same way.

Run markers written before this binding report `review_permit.status:
unbound`. Once the reservation is reconciled, release the permit explicitly:

```text
scripts/review-ledger.py cancel-permit PR --phase-permit TOKEN \
  --role code-reviewer --reason "provider lookup found no request"
```

`cancel-permit` refuses while any usage reservation for the permit's run or
logical review is still open, and refuses unstarted (reissue it with
`permit-review`), completed, cancelled, or superseded permits. It records the
reason and time on the permit and never creates a receipt. Confirm no reviewer
process for that permit is still running before using it.

For several historical reservations, first generate a bounded manifest inside
the repository:

```text
scripts/api_agent.py reservation-migration-plan --repo . \
  > .orchestration/reservation-migration.json
```

Fill every entry's `evidence` with the provider lookup and timestamp that proves
the request was not found. API run entries (`source: api-run`) accept only
`not-found` in this bulk path; a completed API run request still requires
individual token and response-id reconciliation with `reconcile --run-id`.
Gateway entries have stricter rules, described below. Validate the entire
manifest before mutation, then apply it:

```text
scripts/api_agent.py reconcile-reservations --repo . \
  --manifest .orchestration/reservation-migration.json
scripts/api_agent.py reconcile-reservations --repo . \
  --manifest .orchestration/reservation-migration.json --apply
```

The manifest is repository-bound, limited to 100 unique reservations, and each
entry must match both the run marker and the open ledger reservation. Application
is idempotent and copies the exact evidence into the run's durable audit record.

Each plan entry names its `source`. Open reservations that neither path below can
accept are listed under `excluded` with the refusal reason instead of as entries,
so the plan and the reconciler always agree. The plan is a snapshot: application
re-validates every entry.

#### Gateway reservations

Workers launched with `sprint-controller.py launch-local -- .../claude` or
`.../codex exec` meter through the controller-owned loopback gateway. Its
reservations use the controller invocation id as `run_id` and never have a
`.llm-runs` marker, so `reconcile --run-id` cannot close them. New gateway
reservations carry `origin: native-gateway` or `origin: codex-gateway`; API
runner reservations carry `origin: api-run`. Reservations written before the
marker existed are treated as gateway reservations only when they have no run
marker, role `implementer`, no logical review id, and an `anthropic` or `openai`
provider. Anything else without a run marker is refused as `run state not found`,
and a gateway-origin reservation that also has a run marker is refused as ambiguous.

The bulk manifest accepts a gateway entry as `source: native-gateway` only when
all of the following hold at validation time:

- The reservation is still open with that exact `run_id`, and its ticket and
  sprint select a controller checkpoint in `sprint_checkpoint_dir` whose ticket
  launch history records that invocation as a supervised execution unit.
- `execution-<run_id>.terminal.json` exists in that directory, is `phase:
  terminal`, belongs to the invocation, shows a spawned worker, and finished no
  earlier than the reservation.
- The controller's own liveness check reports the unit `absent`. Live and unknown
  units are refused. A cooperative-session unit also needs a closed-gateway cleanup
  receipt and a worker process group that no longer exists.
- The operator chose the outcome, `not-found` or `completed`, and wrote evidence
  for this reservation (see below).

The plan leaves `outcome` and `evidence` empty for gateway entries, so an
unedited plan is refused. Each gateway entry also lists a `request` block
(ticket, sprint, provider, model, `reserved_at`, projected cost, and the unit's
stop reason) and `evidence_must_name`, the values the evidence has to mention.
Look up every request in the provider console yourself. Do not paste one search
result onto every entry.

Gateway evidence is refused when it:

- contains template or placeholder text, such as `<...>`, `{{`, `...`, `TODO`,
  `TBD`, `FIXME`, `placeholder`, `n/a`, or `same as above`;
- is shorter than 24 characters, or has fewer than three words besides ids and
  timestamps;
- does not name this reservation. It must contain either the reservation id
  (`resv_...`) or the reservation's UTC request minute: the date `YYYY-MM-DD` and
  `HH:MM` anywhere in the text. The request minute is `reserved_at` converted to
  UTC. The run id is not enough, because one invocation can hold several
  reservations.

The same sentence can cover several reservations only if it names each one. For
example, `Anthropic Console searched 2026-09-12: no request at 22:36 or 22:49 UTC`
covers reservations made at 22:36 and 22:49 UTC. A search described as
`22:30-22:50` names neither minute and is refused. This is how a reservation that
nobody looked up is kept from being released by a copied line. If a request came
in near a minute boundary, name the reservation id instead.

A `not-found` entry must not carry usage fields. A `completed` entry settles a
request the provider did finish. It needs the provider's `response_id` and
nonnegative integer `input_tokens`, `cache_write_tokens`, `cache_read_tokens`,
and `output_tokens`, using the ledger's normalized meaning:

- `input_tokens` is uncached input.
- `cache_write_tokens` and `cache_read_tokens` are cache creation and cache read
  input.
- `output_tokens` includes any reasoning. `reasoning_tokens` is optional.

For example:

```json
{"run_id": "5ee5e53594254d6293847c2e5229d057",
 "reservation_id": "resv_7bf57feb5ea848168b534faf88e138f0",
 "outcome": "completed",
 "evidence": "Anthropic Console 2026-09-12 22:36 UTC shows msg_01Abc completed",
 "response_id": "msg_01Abc", "input_tokens": 1830, "cache_write_tokens": 0,
 "cache_read_tokens": 41200, "output_tokens": 912}
```

Orka prices that usage with the current `llm.pricing` entry for the reservation's
model from the canonical `.orchestration/config.yaml`. It refuses a zero total, a
response id that appears on two entries, and a response id that already settled
another reservation. If the priced cost exceeds the reservation's projected worst
case, the entry is refused and the reservation stays open, still counting its
projection. That usually means a typo or the wrong request, so nothing is lost or
settled. If the provider's record really does cost more, for example because
prices changed, rerun with `--accept-cost-above-reservation RESERVATION_ID` for
that reservation. The flag must name a reservation in the manifest. The actual
cost is then settled, and the audit record sets `cost_above_reservation: true`.

Before changing the ledger, Orka writes
`.orchestration/.llm-usage/gateway-reconciliations/<run_id>.json` with the
outcome, the evidence, the reservation's cost envelope, the execution proof
(checkpoint, terminal record path and SHA-256, containment, stop reason) and, for
`completed`, the response id, usage, cost, and `cost_above_reservation`. A
`not-found` entry is then released and the release repeats the evidence. A
`completed` entry is settled through the ledger's normal settle path as a
`usage` event with the response id, token counts, and cost. Re-applying the same
manifest is a no-op. Different evidence or usage for an already reconciled
reservation is refused. Once reconciled, `sprint-controller.py restart-ticket` no
longer sees the ticket's `reserved_usd`. Until then, its refusal lists each
blocking reservation id with its run id, projected cost, reservation time, and
origin.

### Logical review retries

API reviewers derive their logical review identity from the consumed permit's
subject, role, exact HEAD, generation, and design round. A replacement execution
for that identity shares its review capacity and can make at most three reserved
execution attempts. Actual usage and uncertain reservations always remain billed.
Code and security each have a ceiling of three logical rounds, within the existing
combined reviewer ceiling. Design review attempts no longer consume the general
12-attempt execution ceiling; design remains subject to its ledger round gate,
logical retry ceiling, and all dollar limits. The general execution ceiling still
bounds non-design attempts, including operational retries. Historical usage without
a logical identity is counted conservatively by execution ID.


### Phase spending envelopes

`llm.budgets` accepts four positive dollar limits. The defaults are $5 for
`max_usd_per_design_phase` (scoping plus design review), $12 for
`max_usd_per_implementation_phase`, and $5 each for
`max_usd_per_code_review_phase` and `max_usd_per_security_review_phase`.
The starter configuration retains its stricter $2 overall ticket limit.
Repository configuration can tighten every phase ceiling. It can raise the
design, code-review, and security-review envelopes up to a $10 hard cap, equal
to the compiled `max_usd_per_run`, so one run still cannot exceed the run
ceiling. The implementation envelope cannot be raised above $12. Larger values
are clamped to the hard cap and reported by `validate-config`. A ticket budget
grant does not expand a phase envelope.

Each envelope counts settled usage plus unresolved reservations across all runs
for that ticket. Rejection or reconciliation can restore unused reserved capacity;
actual spending is never erased. Phase exhaustion does not latch a ticket pause
or consume another phase's allowance. Ticket and sprint ceilings still apply to
the combined cost. Envelopes are limits, not prepaid allocations. After a
controller-verified design approval, unused design allowance transfers to
implementation once, provided no design reservations remain unresolved. Only
allowance within the $5 design default transfers, so raising the design phase
cannot enlarge implementation. The
transfer removes that allowance from design and leaves review allowances and
aggregate ticket/sprint ceilings unchanged. If uncertain design work still has
a reservation, a later verified progress replay can retry the transfer after
reconciliation.

The phase is derived from the runner role, including after reconciliation.
Legacy events without roles count as implementation unless their reservation
identifies the original role. Direct native Claude launches currently charge their
entire process tree to implementation; they do not self-declare review phases.
Sprint summaries expose spent, reserved, remaining, and exhausted capacity for
each phase using a consistent locked ledger snapshot.


For a coordinated continuation across phase dollars and execution/review counts,
use the root-issued restart allowance documented in [Sprint controller](sprint-controller.md#restart-allowances-and-legacy-reconciliation).
Each granted value is an absolute lifetime ceiling, checked at admission against
the live host authority. Old usage and reservations are never removed. Per-run,
sprint-wide, token/tool, and uncertain-provider-work safeguards still apply.
