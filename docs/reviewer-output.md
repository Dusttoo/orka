# Reviewer output

Code and security reviewers return one compact JSON object. This is the same
contract for desktop execution, Anthropic Messages, and OpenAI Responses.

```json
{
  "schema_version": 1,
  "gate": "code-review",
  "verdict": "PASS",
  "checks": [{"name": "acceptance coverage", "status": "pass"}],
  "findings": []
}
```

`checks` are deliberately limited to names and statuses: `pass`, `fail`,
`not_run`, `not_applicable`, or `ci_verified`. Reviewers do not explain
successful checks. Explanations are generated only for actual findings, where
they are needed to describe the defect, impact, evidence, and required
correction.

`ci_verified` means the check was satisfied by CI on the exact reviewed commit,
for example a database integration suite the repository forbids running
locally. It is the only status that carries a field besides `name` and
`status`, and that `evidence` object is required:

```json
{"name":"CI integration suite","status":"ci_verified","evidence":{"ci_check":"integration-tests","head_sha":"<full 40- or 64-hex reviewed head>"}}
```

`ci_check` is the exact CI check-run name and `head_sha` is the full reviewed
head; every `ci_verified` check in one result must cite the same head. Use it
only when that check genuinely runs in CI for this exact commit and completed
with conclusion `success` (normally as listed in the payload's `<ci_evidence>`
block); otherwise `not_run` with a blocking finding. A pending, skipped,
neutral, failed, or older-commit run is not verification. `ci_verified` does not
require a finding; `fail` and `not_run` semantics are unchanged.

Each finding contains a stable `path:symbol` component, `blocking` or `advisory`
disposition, severity, short title, actionable explanation, and a regression
boolean. PASS cannot contain a blocking finding; FAIL must contain one. A failed
or unrun check also requires a blocking finding so its explanation is never
hidden in generic prose.

The API payload builders install this as provider-native structured output. For
desktop runs, save the JSON and validate/record it mechanically:

```bash
scripts/context_pipeline.py validate-review --gate code-review \
  --input .orchestration/.review-results/code-review.json
scripts/review-ledger.py record <pr> --gate code-review \
  --result .orchestration/.review-results/code-review.json
```

API review payloads can embed exact-head CI results. Capture them with
`gh api --paginate "repos/{owner}/{repo}/commits/<full-exact-head>/check-runs?per_page=100" > ci.json`
and pass `--ci-evidence ci.json --review-head <full-exact-head>` to
`context_pipeline.py payload`, or pass `--fetch-ci-evidence --review-head
<full-exact-head>` to have the builder call `gh` itself (explicit network
opt-in). The builder keeps only each run's name, status, conclusion, head SHA,
and URL, caps the list at 64 runs (marking `truncated`), and refuses evidence
containing any run for a different commit.

Validation is structural by default. `validate-review --head <sha>` also
requires every `ci_verified` check to cite that head, and `--ci-evidence
<file> --head <sha>` additionally fails closed unless each cited check run is
present, `completed`, and `success` at that head. The Python API exposes the
same cross-check as keyword-only `reviewed_head=` and `ci_evidence=` arguments
to `validate_review_output`.

The schema itself is available through `scripts/context_pipeline.py
review-schema --gate code-review` (or `security-review`). Result files are
runtime state and should remain gitignored.
