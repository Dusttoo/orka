## Summary

<!-- What changed, why, and what users or maintainers should expect. -->

## Related issue

<!-- Use "Closes #123" when this PR fully resolves an issue. -->

## State, safety, and compatibility

<!--
Which state transitions, trust boundaries, budgets, gates, or external side
effects change? How does existing configuration/checkpoint behavior remain safe?
Write "No runtime behavior change" when appropriate.
-->

## Validation

<!-- List the checks you ran and their results. -->

## Documentation

<!-- List documentation updated, or explain why none is required. -->

## Release checklist

- [ ] The linked issue is labelled `status: ready`, or this is a small bug/doc
      fix that does not require an architecture decision.
- [ ] I added or updated regression tests for behavior changes.
- [ ] I did not include credentials, private repository data, or
      project-specific policy in the plugin.
- [ ] Every commit I introduced is signed and shows **Verified** on GitHub.
- [ ] I described any user-facing compatibility or migration impact for the
      release note. The maintainer will choose the final version before merge.

### Maintainer before merge

- [ ] Choose a version after reconciling other open or recently merged PRs.
- [ ] Bump both plugin manifests to the same version and add
      `docs/releases/<version>.md` on the exact PR head that will merge.
- [ ] Verify the final commits are signed and rerun the required checks.
