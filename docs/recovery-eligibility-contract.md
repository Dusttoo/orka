# Recovery eligibility contract

Contract: `orka.recovery-eligibility/v1`

Machine-readable source:
[`contracts/recovery-eligibility-v1.json`](../contracts/recovery-eligibility-v1.json)

Orka separates recovery **observation**, **eligibility**, and **mutation**.
Host-specific observers collect process, provider, Git, GitHub, worktree, and
usage evidence. The pure evaluator in `scripts/recovery_eligibility.py` then
returns one of three verdicts without reading or changing external state:

- `eligible`: every required identity and settlement fact is exact;
- `waiting`: authoritative evidence shows a live or pending condition;
- `operator_action`: evidence is missing, ambiguous, mismatched, dirty, or
  outside the declared trust profile.

An eligible verdict is not a recovery capability. Later recovery code must
re-observe and re-evaluate the evidence under the controller lock before making
an idempotent transition. Eligibility never increases a budget, attempt count,
repair allowance, review authority, or merge authority.

## Evidence groups

The evaluator accepts a schema-versioned envelope containing:

- `execution`: exact invocation binding, live/absent/unknown status,
  authenticated terminal receipt, descendant status, and cooperative cleanup;
- `provider`: settled, pending, or ambiguous provider and usage-reservation
  state;
- `work`: an `attempt` or `preserved_pr` kind, complete
  repository/ticket/attempt/work binding, worktree state, and exact
  branch/PR/head/tree agreement;
- `history`: the fields the eventual mutation proves it preserves.

Every refusal includes stable machine-readable reason codes and bounded human
explanations. Multiple defects may be returned together so an operator does not
need repeated stop-and-fix cycles to discover the full recovery boundary.

## Trust profiles

### Isolated

The host can inspect the complete execution boundary, such as a Linux cgroup v2
systemd scope. Recovery requires the exact unit to be absent, its terminal
receipt to match the invocation, and all descendants to be absent. Unknown
inspection fails closed.

### Cooperative

The host cannot prove that a worker never escaped its process group. Automatic
eligibility therefore additionally requires the cooperative contract to have
been enabled both at launch and recovery, a closed gateway, and an absent worker
process group. The contract is an explicit trust choice, not equivalent to
isolation.

### Preserved PR

The prior execution must be absent and the repository, ticket, attempt,
worktree, branch, PR, head, and tree must agree. The worktree must be clean,
quiescent, and exclusively owned. Head movement, a dirty or active worktree, or
unknown ownership is not eligible.

## Provider and financial settlement

`pending` provider work is waitable and must not be duplicated. `ambiguous`
provider acknowledgement or financial settlement requires operator
reconciliation. Only `settled` evidence can be eligible. The evaluator does not
guess from elapsed time or a missing response.

## Preservation invariants

Every recovery mutation must preserve attempts, spend, verified progress,
branch, worktree, PR, review findings, and review generation. These values may
be extended monotonically by later work; recovery cannot erase or recreate
them. The evaluator reports the invariant list with every verdict.

## Compatibility mapping

The current `automatic_recovery_available` observer already distinguishes
strictly isolated execution units from explicitly opted-in cooperative sessions.
The current preserved-PR path already checks exact GitHub head/tree evidence,
worktree location and quiescence, execution-unit absence, and usage recovery
fencing. Subsequent #74 slices will normalize those observations into this
contract before enabling automatic state mutation. This slice changes no
recovery admission or authority behavior.
