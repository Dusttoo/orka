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
- [ ] I bumped the version in both `.claude-plugin/plugin.json` and
      `.codex-plugin/plugin.json`, kept the versions identical, and selected the
      appropriate semantic-version increment. Every PR requires a version bump.
