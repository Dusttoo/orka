# Contributing to Orka

Thank you for helping make autonomous engineering workflows more reliable.
Orka coordinates coding agents, external providers, Jira, GitHub, local
processes, and merge gates. Small-looking changes can therefore affect durable
state or authorization boundaries. This guide explains how to contribute
without weakening those guarantees.

## Start with an issue

- Search the open issues before proposing new work.
- Use the bug form for reproducible defects and attach sanitized evidence.
- Use the feature form for bounded additions.
- Use the architecture form before changing controller state, persistence,
  recovery authority, spending enforcement, worker isolation, or merge policy.
- Issues labelled `status: ready` have an accepted scope and are available for
  implementation. `status: needs design` means discussion is still required.
- Comment before starting an issue so maintainers and other contributors do not
  duplicate work. Assignment is a coordination signal, not ownership forever.

Never include API keys, credentials, private repository contents, customer
data, or unsanitized provider transcripts in an issue.

## Development workflow

1. Fork the repository and branch from the latest `main`.
2. Keep one pull request focused on one issue or independently reviewable unit.
3. Add a regression test before or with every behavior change.
4. Preserve backward compatibility unless the issue is labelled
   `breaking change` and has an accepted migration plan.
5. Update documentation for user-visible behavior or configuration.
6. Link the pull request to its issue with `Closes #123` when appropriate.

You do not need to choose the next Orka version or edit the plugin manifests.
The maintainer assigns the final version, updates both manifests, and adds the
release note when the pull request is ready to merge. This avoids version
collisions between concurrent contributions. If your change needs a specific
compatibility or migration note, describe it in the pull request.

## Signed commits

Orka's protected `main` branch requires every incoming commit to have a
signature that GitHub marks **Verified**. Set up [GPG or SSH commit signing on
GitHub](https://docs.github.com/en/authentication/managing-commit-signature-verification/about-commit-signature-verification)
and add the corresponding public signing key to your GitHub account before your
first commit. Configure signing by default for your fork, or use `git commit -S`
for each commit:

```bash
git config commit.gpgsign true
git commit -S -m "Describe the change"
git log --show-signature origin/main..HEAD
```

Check the signatures on *all* commits you are introducing before pushing, and
confirm that each commit shows **Verified** on the pull request's **Commits** tab.
A local signature alone does not guarantee GitHub can verify it; the signing key
must be associated with your account. An unsigned commit can block the pull
request even when the final squash or merge commit would be signed. If you
already pushed unsigned commits, rewrite and sign those commits on your fork,
then update the pull request with `git push --force-with-lease`. Ask in the pull
request if you need help; maintainers will not disable the signature rule to
merge it. See [GitHub's signing guide](https://docs.github.com/en/authentication/managing-commit-signature-verification/signing-commits)
and [protected branch behavior](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches#require-signed-commits).

## Local verification

Run the full suite from the repository root:

```bash
bash tests/run.sh
```

During development, run the smallest relevant test directly first. Before a
pull request is ready, run the full suite and record any unrelated pre-existing
failure separately rather than hiding it.

Useful focused suites include:

```bash
bash tests/sprint-controller.test.sh
bash tests/review-ledger.test.sh
bash tests/workflow-engine.test.sh
bash tests/provider-adapters.test.sh
```

## Design constraints

Contributions must preserve these boundaries:

- Deterministic host code owns scheduling, durable state, budgets, and merge
  authority. Model output is untrusted input to that machinery.
- A worker may fail, time out, or return malformed output without corrupting or
  globally stopping unrelated work.
- Recovery must be idempotent and based on authenticated or mechanically
  verifiable evidence.
- A ticket-level failure must not weaken review, security, budget, or merge
  gates for another ticket.
- The plugin remains repository-agnostic. Project names, ticket keys, private
  paths, and application-specific policy belong in the consuming repository.
- Claude Code and Codex behavior must remain equivalent unless a documented
  host capability makes parity impossible.

The accepted direction for the next runtime generation is documented in
[the Orka 2 durable runtime roadmap](docs/orka-2-durable-runtime.md).

## Pull request expectations

A reviewable pull request includes:

- the problem and linked issue;
- the state transitions or invariants affected;
- tests covering success, failure, retry, and restart where relevant;
- compatibility and migration impact;
- the commands run and their results;
- release note details when users need to change how they use Orka.

Before merge, the maintainer follows the [release process](docs/releasing.md)
to choose the next version, bump both manifests, and write its release note.

Maintainers may ask for a design issue to be resolved before reviewing a large
implementation. This protects contributors from spending time on an approach
that conflicts with the durable runtime or safety model.

## Reporting security issues

Do not open public issues for vulnerabilities involving credential exposure,
authorization bypass, unsafe command execution, merge-guard bypass, or
cross-repository data disclosure. Follow [SECURITY.md](SECURITY.md).
