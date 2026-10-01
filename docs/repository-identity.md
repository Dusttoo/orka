# Repository identity and canonical policy

Orka 1.8.20 implements the repository boundary selected by
[ADR 0001](adr/0001-transactional-event-store.md). The absolute Git common
directory owns a private `orka-runtime` directory:

```text
normal checkout       <checkout>/.git/orka-runtime/
linked worktree       <main-checkout>/.git/orka-runtime/
bare-backed worktree  <repository>.git/orka-runtime/
```

The common directory's parent is never used as the new state root. This keeps
sibling bare repositories in separate identity and enforcement domains.

## Explicit initialization

Initialization is an operator action. Select a fully qualified trusted Git ref
and the repository-relative policy path:

```bash
python3 /path/to/orka/scripts/runtime_state.py init \
  --repo /path/to/repository \
  --policy-ref refs/remotes/origin/main \
  --policy-path .orchestration/config.yaml
```

The command resolves the ref, commit, and blob before atomically creating
`repository.json`. The marker binds a random repository UUID to the canonical
common and object directories plus the selected policy source. Repeating the
same initialization is idempotent. A different binding, copied marker, moved
common directory, malformed marker, unsafe permissions, symlink, missing ref,
or missing policy blob fails closed.

Local-only repositories can bind a fully qualified protected local branch such
as `refs/heads/main`. The ref remains live: every policy read resolves its
current exact commit and blob and reports their identifiers and the SHA-256 of
the policy bytes. An unmerged change in the invoking worktree cannot alter the
effective policy.

## Status and preflight

Inspect the non-secret identity and policy provenance with:

```bash
python3 /path/to/orka/scripts/runtime_state.py status --repo /path/to/repository
```

Captain preflight includes the same report under `repository_identity`, with
the repository UUID, common directory, runtime root, policy ref/path,
commit/blob IDs, and digest. Repositories not yet initialized report
`legacy-uninitialized`; this is an additive compatibility state while the
current JSON controllers remain authoritative.

## Migration boundary

This release does not move, delete, import, or rewrite production JSON state.
It inventories worktree-local and historical parent-level candidates so later
migration can authenticate them. Ambiguous parent-level state is reported and
left untouched. Issue #117 owns explicit import; issue #118 owns the
single-writer cutover.
