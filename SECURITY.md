# Security Policy

## Supported versions

Security fixes target the current supported plugin version recorded in the
paired manifests on `main`. Older plugin cache versions are immutable
snapshots and should be upgraded after a fix is available. GitHub Releases are
tagged snapshots and may trail a newly merged plugin version until its release
is published.

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/Dusttoo/orka/security/advisories/new).
Do not open a public issue for a suspected vulnerability.

Include, when available:

- the affected Orka version and host;
- the trust profile and execution route;
- minimal reproduction steps;
- the expected and observed authorization boundary;
- sanitized logs or state excerpts;
- whether credentials, merges, commands, repositories, or spending controls
  may be affected.

Never include live credentials, private repository contents, customer data, or
unsanitized model transcripts. We will acknowledge a complete report as soon as
practical, validate its impact, coordinate a fix, and agree on disclosure
timing with the reporter.

Ordinary reliability bugs that do not expose a security boundary can use the
public bug-report form.
