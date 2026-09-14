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
tighten the compiled incident ceilings but cannot raise or disable them. There
is deliberately no same-user CLI approval bypass.

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

For several historical reservations, first generate a bounded manifest inside
the repository:

```text
scripts/api_agent.py reservation-migration-plan --repo . \
  > .orchestration/reservation-migration.json
```

Fill every entry's `evidence` with the provider lookup and timestamp that proves
the request was not found. Orka accepts only `not-found` outcomes in this bulk
path; completed requests still require individual token and response-id
reconciliation. Validate the entire manifest before mutation, then apply it:

```text
scripts/api_agent.py reconcile-reservations --repo . \
  --manifest .orchestration/reservation-migration.json
scripts/api_agent.py reconcile-reservations --repo . \
  --manifest .orchestration/reservation-migration.json --apply
```

The manifest is repository-bound, limited to 100 unique reservations, and each
entry must match both the run marker and the open ledger reservation. Application
is idempotent and copies the exact evidence into the run's durable audit record.

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

`llm.budgets` accepts four positive dollar limits. The defaults and compiled
maximums are $5 for `max_usd_per_design_phase` (scoping plus design review), $12
for `max_usd_per_implementation_phase`, and $5 each for
`max_usd_per_code_review_phase` and `max_usd_per_security_review_phase`.
The starter configuration retains its stricter $2 overall ticket limit.
Repository configuration can tighten the phase ceilings. A ticket budget grant does
not expand a phase envelope.

Each envelope counts settled usage plus unresolved reservations across all runs
for that ticket. Rejection or reconciliation can restore unused reserved capacity;
actual spending is never erased. Phase exhaustion does not latch a ticket pause
or consume another phase's allowance. Ticket and sprint ceilings still apply to
the combined cost. Envelopes are limits, not prepaid allocations. After a
controller-verified design approval, unused design allowance transfers to
implementation once, provided no design reservations remain unresolved. The
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
