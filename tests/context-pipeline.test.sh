#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIPELINE="$ROOT/scripts/context_pipeline.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0
ok() { printf 'ok   %s\n' "$1"; }
fail_case() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
check() { local label="$1" expression="$2" file="$3"; if python3 -c 'import json,sys; data=json.load(open(sys.argv[1])); assert eval(sys.argv[2], {"data":data})' "$file" "$expression"; then ok "$label"; else fail_case "$label"; fi; }
run_fail() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then fail_case "$label"; else ok "$label"; fi; }

mkdir -p "$TMP/.orchestration"
cp "$ROOT/templates/config.yaml" "$TMP/.orchestration/config.yaml"
printf 'role brief\n' > "$TMP/role.md"
printf 'repo rules\n' > "$TMP/AGENTS.md"
printf 'scripts/\n  controller.py\n' > "$TMP/map.txt"
printf '{"key":"PROJ-1","summary":"small"}\n' > "$TMP/ticket.json"
printf 'diff --git a/a.py b/a.py\n+new\n' > "$TMP/change.diff"

"$PIPELINE" jira-fields --config "$TMP/.orchestration/config.yaml" > "$TMP/fields.json"
check "configured Jira fields become an explicit fields parameter" 'data["fields"] == "key,summary,description,status,priority,components,subtasks,issuelinks"' "$TMP/fields.json"
"$PIPELINE" jira-fields --config "$TMP/missing.yaml" > "$TMP/default-fields.json"
check "missing config uses the compact Jira field defaults" 'len(data["field_list"]) == 8 and data["field_list"][0] == "key"' "$TMP/default-fields.json"

printf '%s\n' '{"expand":"schema,names","issues":[{"key":"PROJ-1","renderedFields":{"description":"huge"},"editmeta":{"fields":{"x":{}}},"changelog":{"histories":[1]},"fields":{"summary":"small","description":{"type":"doc","avatarUrls":{"48x48":"https://avatar"}},"comment":{"comments":["waste"]},"status":{"name":"Ready","schema":{"type":"status"}}}}]}' > "$TMP/jira.json"
"$PIPELINE" sanitize-jira --config "$TMP/.orchestration/config.yaml" --input "$TMP/jira.json" > "$TMP/sanitized.json"
check "sanitizer keeps only requested Jira fields" 'set(data["issues"][0]["fields"]) == {"summary","description","status"}' "$TMP/sanitized.json"
check "sanitizer drops render, edit, changelog, schemas, and avatar links" '"renderedFields" not in str(data) and "editmeta" not in str(data) and "changelog" not in str(data) and "schema" not in str(data) and "avatar" not in str(data)' "$TMP/sanitized.json"

"$PIPELINE" anthropic --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" \
  --repo-map "$TMP/map.txt" --ticket "$TMP/ticket.json" --diff "$TMP/change.diff" \
  --mode code-review --execution gate --model test-model > "$TMP/payload.json"
check "static Anthropic context is ordered role, rules, repository map" '[x["text"].splitlines()[0] for x in data["system"]] == ["# Global role briefs", "# Repository rules and conventions", "# Stable repository map"]' "$TMP/payload.json"
check "default worker trust profile is injected into role context" '"Selected profile: cooperative-worker" in data["system"][0]["text"] and "never weakens" in data["system"][0]["text"]' "$TMP/payload.json"
check "gate payload caches the final stable block" 'data["system"][2]["cache_control"] == {"type":"ephemeral"} and "cache_control" not in data["system"][0]' "$TMP/payload.json"
check "dynamic ticket and raw diff stay after the cached prefix" '"<ticket>" in data["messages"][0]["content"] and "<active_branch_unified_diff>" in data["messages"][0]["content"] and "Do not index" in data["messages"][0]["content"]' "$TMP/payload.json"
check "Anthropic reviewers use a native strict output shape" 'data["output_config"]["format"]["type"] == "json_schema" and data["output_config"]["format"]["schema"]["properties"]["gate"]["enum"] == ["code-review"]' "$TMP/payload.json"

"$PIPELINE" payload --provider openai --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" \
  --repo-map "$TMP/map.txt" --ticket "$TMP/ticket.json" --diff "$TMP/change.diff" \
  --mode code-review --execution gate --model test-model --effort low > "$TMP/openai.json"
check "OpenAI Responses payload preserves the same stable-prefix order" '[x["text"].splitlines()[0] for x in data["input"][0]["content"]] == ["# Global role briefs", "# Repository rules and conventions", "# Stable repository map"]' "$TMP/openai.json"
check "OpenAI payload omits every optional cache request field" 'all(field not in str(data) for field in ["prompt_cache_key", "prompt_cache_options", "prompt_cache_breakpoint", "prompt_cache_retention"]) and data["reasoning"]["effort"] == "low"' "$TMP/openai.json"
check "OpenAI reviewers use low-verbosity strict structured output" 'data["text"]["verbosity"] == "low" and data["text"]["format"]["type"] == "json_schema" and data["text"]["format"]["strict"] is True' "$TMP/openai.json"
check "payloads without repository config retain the 8192 compatibility default" 'data["max_output_tokens"] == 8192' "$TMP/openai.json"

"$PIPELINE" payload --provider openai --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" \
  --repo-map "$TMP/map.txt" --ticket "$TMP/ticket.json" --mode scope \
  --execution on-demand --model test-model > "$TMP/scope.json"
check "ticket scoping is a distinct non-review payload" '"text" not in data and "<ticket>" in data["input"][1]["content"][0]["text"] and "<active_branch_unified_diff>" not in data["input"][1]["content"][0]["text"]' "$TMP/scope.json"

"$PIPELINE" payload --provider openai --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" \
  --repo-map "$TMP/map.txt" --ticket "$TMP/ticket.json" --mode implement \
  --execution on-demand --model test-model > "$TMP/implement.json"
check "implementers are not forced into the reviewer schema" '"text" not in data' "$TMP/implement.json"
check "implement payloads omit every optional cache request field" 'all(field not in str(data) for field in ["prompt_cache_key", "prompt_cache_options", "prompt_cache_breakpoint", "prompt_cache_retention"])' "$TMP/implement.json"

"$PIPELINE" anthropic --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" \
  --repo-map "$TMP/map.txt" --ticket "$TMP/ticket.json" --mode implement \
  --execution on-demand --model test-model > "$TMP/implement-anthropic.json"
check "Anthropic implement payloads cache the stable prefix too" 'data["system"][2]["cache_control"] == {"type":"ephemeral"}' "$TMP/implement-anthropic.json"
run_fail "no execution mode can silently disable prompt caching" "$PIPELINE" anthropic \
  --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" --repo-map "$TMP/map.txt" \
  --ticket "$TMP/ticket.json" --mode implement --execution interactive --model test-model

"$PIPELINE" payload --provider azure_adm --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" \
  --repo-map "$TMP/map.txt" --ticket "$TMP/ticket.json" --diff "$TMP/change.diff" \
  --mode code-review --execution gate --model grok-eval > "$TMP/azure-adm.json"
check "Azure ADM payload uses Chat Completions messages" 'data["model"] == "grok-eval" and data["messages"][0]["role"] == "system" and data["messages"][1]["role"] == "user" and data["max_completion_tokens"] == 8192' "$TMP/azure-adm.json"
check "Azure ADM reviewer contract brackets dynamic context for MAI compatibility" '"Your entire final response must be exactly one JSON object" in data["messages"][0]["content"] and "Your entire final response must be exactly one JSON object" in data["messages"][1]["content"] and data["messages"][1]["content"].rstrip().endswith("}") and "code-review" in data["messages"][0]["content"]' "$TMP/azure-adm.json"

"$PIPELINE" payload --provider bedrock_mantle --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" \
  --repo-map "$TMP/map.txt" --ticket "$TMP/ticket.json" --diff "$TMP/change.diff" \
  --mode code-review --execution gate --model moonshotai.kimi-k2-thinking > "$TMP/bedrock-mantle.json"
check "Bedrock Mantle payload uses portable Chat Completions fields" 'data["model"] == "moonshotai.kimi-k2-thinking" and data["messages"][0]["role"] == "system" and data["messages"][1]["role"] == "user" and data["max_tokens"] == 8192 and "max_completion_tokens" not in data' "$TMP/bedrock-mantle.json"
check "Bedrock Mantle reviewers retain the strict JSON contract" '"Your entire final response must be exactly one JSON object" in data["messages"][0]["content"] and "code-review" in data["messages"][0]["content"]' "$TMP/bedrock-mantle.json"

"$PIPELINE" payload --provider bedrock --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" \
  --repo-map "$TMP/map.txt" --ticket "$TMP/ticket.json" --diff "$TMP/change.diff" \
  --mode code-review --execution gate --model global.anthropic.claude-opus-5 \
  --effort high > "$TMP/bedrock.json"
check "Bedrock payload uses native Converse fields and an explicit output cap" 'data["modelId"] == "global.anthropic.claude-opus-5" and data["inferenceConfig"]["maxTokens"] == 8192 and "<ticket>" in data["messages"][0]["content"][0]["text"]' "$TMP/bedrock.json"
check "Bedrock caches the stable prefix for one hour" 'data["system"][3]["cachePoint"] == {"type":"default","ttl":"1h"} and data["system"][2]["text"].startswith("# Stable repository map")' "$TMP/bedrock.json"
check "Bedrock carries adaptive effort without an unsupported structured-output field" 'data["additionalModelRequestFields"]["thinking"] == {"type":"adaptive"} and data["additionalModelRequestFields"]["output_config"] == {"effort":"high"}' "$TMP/bedrock.json"
check "Bedrock Claude reviewers receive the validated JSON contract" '"Your entire final response must be exactly one JSON object" in data["system"][-1]["text"] and "Your entire final response must be exactly one JSON object" in data["messages"][0]["content"][0]["text"]' "$TMP/bedrock.json"

"$PIPELINE" payload --provider bedrock --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" \
  --repo-map "$TMP/map.txt" --ticket "$TMP/ticket.json" --diff "$TMP/change.diff" \
  --mode code-review --execution gate --model global.openai.gpt-5.6-sol \
  --effort xhigh > "$TMP/bedrock-openai.json"
check "Bedrock OpenAI uses its native reasoning field without Anthropic fields" 'data["additionalModelRequestFields"] == {"reasoning_effort":"xhigh"} and all("cachePoint" not in block for block in data["system"])' "$TMP/bedrock-openai.json"
check "Bedrock OpenAI reviewers receive a duplicated strict JSON contract" '"Your entire final response must be exactly one JSON object" in data["system"][-1]["text"] and "Your entire final response must be exactly one JSON object" in data["messages"][0]["content"][0]["text"] and "output_config" not in data["additionalModelRequestFields"]' "$TMP/bedrock-openai.json"

cat > "$TMP/pass-review.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"PASS","checks":[{"name":"acceptance coverage","status":"pass"}],"findings":[]}
JSON
"$PIPELINE" validate-review --gate code-review --input "$TMP/pass-review.json" > "$TMP/validated-review.json"
check "a concise clean review validates" 'data["verdict"] == "PASS" and data["findings"] == []' "$TMP/validated-review.json"
cat > "$TMP/fail-review.json" <<'JSON'
{"schema_version":1,"gate":"code-review","verdict":"FAIL","checks":[{"name":"tests","status":"fail"}],"findings":[{"component":"src/a.py:parse","disposition":"blocking","severity":"high","title":"Parser accepts invalid input","explanation":"Input X reaches parse and returns Y; reject it and add the named regression assertion.","regression":true}]}
JSON
"$PIPELINE" validate-review --gate code-review --input "$TMP/fail-review.json" >/dev/null && ok "finding explanations validate only inside findings" || fail_case "finding explanations validate only inside findings"
python3 - "$TMP/fail-review.json" "$TMP/wrapped-review.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
value["findings"][0]["component"] = "[component: src/a.py:parse]"
json.dump(value, open(sys.argv[2], "w"))
PY
"$PIPELINE" validate-review --gate code-review --input "$TMP/wrapped-review.json" > "$TMP/normalized-review.json"
check "legacy wrapped component keys normalize without discarding the review" 'data["findings"][0]["component"] == "src/a.py:parse"' "$TMP/normalized-review.json"
python3 - "$TMP/fail-review.json" "$TMP/invalid-component-review.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
value["findings"][0]["component"] = "tests/e2e/particles.spec.ts:cold hot reset flow"
json.dump(value, open(sys.argv[2], "w"))
PY
run_fail "component keys reject prose test names with whitespace" "$PIPELINE" validate-review --gate code-review --input "$TMP/invalid-component-review.json"
python3 - "$TMP/pass-review.json" "$TMP/invalid-review.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
value["summary"] = "wasted prose"
json.dump(value, open(sys.argv[2], "w"))
PY
run_fail "top-level review prose is rejected" "$PIPELINE" validate-review --gate code-review --input "$TMP/invalid-review.json"
python3 - "$TMP/fail-review.json" "$TMP/contradictory-review.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
value["verdict"] = "PASS"
json.dump(value, open(sys.argv[2], "w"))
PY
run_fail "PASS with a blocking finding is rejected" "$PIPELINE" validate-review --gate code-review --input "$TMP/contradictory-review.json"

"$PIPELINE" route --config "$TMP/missing.yaml" --role implementer > "$TMP/default-route.json"
check "missing routing config preserves desktop execution" 'data["execution"] == "desktop" and data["fallback"] == "none"' "$TMP/default-route.json"

printf 'llm_provider: openai\n' > "$TMP/legacy-route.yaml"
"$PIPELINE" route --config "$TMP/legacy-route.yaml" --role implementer > "$TMP/legacy-route.json"
check "legacy flat provider config remains desktop-compatible" 'data["execution"] == "desktop" and data["provider"] == "openai"' "$TMP/legacy-route.json"

cat > "$TMP/routes.yaml" <<'YAML'
worker_trust_profile: isolated-worker
llm:
  execution: api
  provider: anthropic
  model: claude-global
  effort: high
  fallback: desktop
  budgets:
    max_output_tokens_per_turn: 32768
  roles:
    code-reviewer:
      provider: openai
      model: gpt-review
      effort: low
      allowed_tools: [read_file, search, git_diff]
    security-reviewer:
      execution: desktop
YAML
"$PIPELINE" route --config "$TMP/routes.yaml" --role orchestration-code-reviewer > "$TMP/review-route.json"
check "role overrides inherit and replace individual global route fields" 'data == {"role":"code-reviewer","execution":"api","provider":"openai","model":"gpt-review","effort":"low","fallback":"desktop","allowed_tools":["read_file","search","git_diff"],"fallback_before_provider_ack_only":True}' "$TMP/review-route.json"
"$PIPELINE" route --config "$TMP/routes.yaml" --role security-reviewer > "$TMP/security-route.json"
check "desktop role override disables an inherited API fallback" 'data["execution"] == "desktop" and data["fallback"] == "none" and data["model"] == "claude-global"' "$TMP/security-route.json"

"$PIPELINE" payload --config "$TMP/routes.yaml" --role code-reviewer \
  --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" --repo-map "$TMP/map.txt" \
  --ticket "$TMP/ticket.json" --diff "$TMP/change.diff" --mode code-review \
  --execution gate > "$TMP/routed-payload.json"
check "payload construction consumes the resolved API role route" 'data["model"] == "gpt-review" and data["reasoning"]["effort"] == "low" and "input" in data' "$TMP/routed-payload.json"
check "configured isolated worker profile reaches API reviewers" '"Selected profile: isolated-worker" in data["input"][0]["content"][0]["text"]' "$TMP/routed-payload.json"
check "routed payload defaults to the repository output-token ceiling" 'data["max_output_tokens"] == 32768' "$TMP/routed-payload.json"

"$PIPELINE" payload --config "$TMP/routes.yaml" --role code-reviewer \
  --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" --repo-map "$TMP/map.txt" \
  --ticket "$TMP/ticket.json" --diff "$TMP/change.diff" --mode code-review --max-tokens 4096 \
  --execution gate > "$TMP/routed-explicit-smaller.json"
check "an explicit smaller output cap is preserved" 'data["max_output_tokens"] == 4096' "$TMP/routed-explicit-smaller.json"

"$PIPELINE" payload --config "$TMP/routes.yaml" --role code-reviewer \
  --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" --repo-map "$TMP/map.txt" \
  --ticket "$TMP/ticket.json" --diff "$TMP/change.diff" --mode code-review --max-tokens 65536 \
  --execution gate > "$TMP/routed-explicit-larger.json"
check "an explicit output request cannot exceed repository policy" 'data["max_output_tokens"] == 32768' "$TMP/routed-explicit-larger.json"
run_fail "desktop routes refuse API payload construction" "$PIPELINE" payload \
  --config "$TMP/routes.yaml" --role security-reviewer --role-file "$TMP/role.md" \
  --rules-file "$TMP/AGENTS.md" --repo-map "$TMP/map.txt" --ticket "$TMP/ticket.json"

cat > "$TMP/invalid-route.yaml" <<'YAML'
llm:
  execution: api
  provider: anthropic
  fallback: desktop
YAML
run_fail "API routes without a model fail closed" "$PIPELINE" route --config "$TMP/invalid-route.yaml" --role implementer

cat > "$TMP/invalid-unused-override.yaml" <<'YAML'
llm:
  execution: desktop
  roles:
    security-reviewer:
      provider: unknown
YAML
run_fail "invalid unused role overrides fail the whole routing policy closed" "$PIPELINE" route --config "$TMP/invalid-unused-override.yaml" --role implementer

cat > "$TMP/invalid-trust-profile.yaml" <<'YAML'
worker_trust_profile: omnipotent-worker
llm:
  execution: api
  provider: openai
  model: test-model
YAML
run_fail "invalid worker trust profile fails payload construction" "$PIPELINE" payload \
  --config "$TMP/invalid-trust-profile.yaml" --role implementer \
  --role-file "$TMP/role.md" --rules-file "$TMP/AGENTS.md" \
  --repo-map "$TMP/map.txt" --ticket "$TMP/ticket.json" --mode implement

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "$fails FAILED"; fi
[ "$fails" -eq 0 ]
