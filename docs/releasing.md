# Releasing Orka

Orka has two visible version surfaces:

- The Claude marketplace and Codex plugin source use the paired plugin manifest
  version on `main`. This is the current supported plugin snapshot.
- A GitHub Release is a tagged source snapshot with release notes. Its
  **Latest** label changes only when a release is published. A just-merged
  version can appear in the marketplace before its GitHub Release is published.

Each merge into `main` must advance the manifest version so installed plugin
caches can detect the change. Contributors may leave the manifests and
`docs/releases/` untouched. The maintainer owns the final version decision and
the release note, resolving collisions with other open PRs before merge.

## Before merging a pull request

1. Bring the PR up to date with `main`. Review the diff and select the next
   semantic version: patch for compatible fixes, docs, and tests; minor for
   backward-compatible features; major for breaking configuration or contract
   changes. Do not reserve a version in an early contributor PR.
2. Update `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` to the
   *same* new version on the PR branch. Add `docs/releases/<version>.md` with a
   concise summary, user-visible compatibility information, and migration
   steps when applicable.
3. Confirm the final PR head contains those files, rerun the relevant checks,
   and inspect the introduced commits with `git log --show-signature`. Verify
   that GitHub marks each introduced commit **Verified**. Never bypass the
   protected branch's signature rule.
4. Merge only the reviewed head. If another PR merges first, choose the next
   available version and repeat the checks on the updated head.

## After merging

1. Verify that `main` contains the merged version in both manifests and its
   release note. Create a **signed annotated** `v<version>` tag on the exact
   `main` commit carrying that version, and verify the tag signature before
   pushing it.
2. Publish a GitHub Release from that tag using
   `docs/releases/<version>.md` as its notes. Check that the Release points to
   the intended commit and is shown as the latest release.
3. For a security fix, publish the corresponding security advisory through
   the repository's private reporting flow as appropriate.

Do not move an existing release tag to incorporate later changes. A later
change receives a new version and tag.
