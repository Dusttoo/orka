# The merge-guard

The merge-guard turns "never merge on red" from a discipline into a host-neutral
mechanism. `merge-on-green.sh` calls `merge-guard.sh --assert-green` directly
before every sanctioned merge. The same script can also run as a Claude Code or
Codex `PreToolUse` hook to veto raw merge commands when that host supports and
trusts plugin hooks.

## Why a script plus an optional hook

An agent told "do not merge until the gates pass" will, eventually, merge before
the gates pass. Not from malice, from a wrong assumption: a check that returned
empty read as success, a branch protection that was not enforced, a race between
a background CI watcher and the merge command. When that happens at 3am in an
unattended run, an instruction provides no backstop.

The sanctioned script does. It does not matter what the agent believes about the
gate state: the guard re-derives it from the marker, active plugin version, and
live PR head/base identity. A trusted hook adds coverage for raw `gh pr merge`
commands, but correctness does not disappear on a Codex or Claude Code host that
does not register it.

Before recording or accepting a marker, the guard also checks the active plugin
against the repository's `minimum_orka_version`. An older runtime fails closed
even when its marker matches its own version. The review ledger applies the same
policy before issuing any review permit.

## Hook contract

```
stdin  : the PreToolUse JSON (.tool_name, .tool_input.command)
exit 0 : ALLOW  (not a merge, or a valid green marker exists)
exit 2 : BLOCK  (stderr is fed back to the model as the reason)
```

The guard fires only on a literal `gh pr merge`. It parses the command's argv
shape (via `shlex`) so that a commit body, a `--body` string, or a shell comment
that merely *contains* the words "gh pr merge" is not mistaken for a merge. Only
a command whose first three tokens are `gh pr merge` is treated as one.

## What it blocks, and why

1. **A merge with no all-green marker.** The default. A marker is written only by
   `--record-green`, which the orchestrator calls after every gate passes and CI
   is green. No marker means the gates have not been recorded as passed.

2. **A merge whose recorded identity changed.** A marker binds the active plugin
   version, head branch/SHA, and base branch/SHA. A plugin upgrade, rebase, push,
   retarget, or moved target branch invalidates the proof and requires re-gating.

3. **A merge whose marker is older than the freshness window**
   (`MERGE_GUARD_MAX_AGE_SECONDS`, default 3600). Even when the SHA still
   matches, an hours-old marker means CI state and the base branch have moved on;
   re-gate. This closes the gap where a marker is recorded, the run stalls for
   hours, then a merge fires against a world that has changed.

4. **Any merge target or squash strategy blocked by configuration.** Legacy
   configs preserve the old protected release-target and squash blocking
   behavior. Schema v2 configs declare `merge_guard.blocked_merge_roles` and
   `merge_guard.block_squash`. The guard resolves those branch roles through
   `orchestration-engine.py` and blocks before the command executes.

## Recording a marker

```
merge-guard.sh --record-green <pr> [result_file]
```

The orchestrator calls this only after all gates PASS and CI is green. With no
`result_file`, it stamps a marker for the current plugin version and exact PR
head/base identity. With a
`result_file` from `run-verification.sh`, it additionally requires that the file
is `GREEN` and its embedded SHA matches the PR head, so a marker cannot be
recorded unless the heavy verification actually ran on this exact commit. This
folds the strong, artifact-backed proof into the same mechanism that otherwise
trusts the orchestrator's assertion.

`--clear <pr>` drops a marker (e.g. after a rebase); `merge-on-green.sh` clears
the spent marker automatically after a successful merge.

`--assert-green <pr> [expected_head_branch]` is the exact reader/enforcement
contract used by both hosts. It re-reads GitHub, validates every marker field and
the configured target, and exits nonzero on missing, malformed, stale, or changed
evidence. `merge-on-green.sh` always invokes it before merging.

## Fail-closed

The precise argv parse uses `python3`. If `python3` is unavailable, the guard
does **not** fall through to allowing the command, which would silently disable
the only enforcement point. Instead it uses a best-effort bash/grep detector and
blocks anything resembling `gh pr merge`. A security mechanism that disables
itself when a dependency is missing is worse than one that occasionally
over-blocks; the guard chooses over-blocking.

## Where it stops, and what complements it

The direct assertion governs the sanctioned merge path on every host. The hook
governs raw merge commands where supported. These are still only local layers:

- **Branch protection** (applied out-of-band via `gh api`) closes the paths the
  hook cannot see: a direct push, a merge from the GitHub UI, a non-agent shell.
  The guard and branch protection are complementary; neither alone is complete.
- **The independent review gates** decide *whether* the work is correct; the
  guard only enforces that their verdict was recorded before a merge. CI-green is
  necessary but never sufficient, which is the whole reason the marker exists
  rather than the guard just polling CI.

## Test coverage

Every path above is covered in [tests/merge-guard.test.sh](../tests/merge-guard.test.sh):
a non-merge command passes through; a commit body mentioning the words passes
through; a no-marker merge blocks; a valid fresh marker allows; a moved-SHA
marker blocks; head-branch, base-SHA, and plugin-version changes block; an
expired marker blocks; configured blocked branches and
configured squash policy block; a non-Bash tool is ignored; and the fail-closed
fallback both blocks a no-marker merge and allows a plain non-merge command with
the precise command parser forced off.

Isolation seams for the tests: `MERGE_GUARD_STATUS_DIR` redirects the marker
directory, `MERGE_GUARD_PR_*` variables stub the exact identity reader, and
`MERGE_GUARD_FORCE_FALLBACK` exercises the fallback parser path.
