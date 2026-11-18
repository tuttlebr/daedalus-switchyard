# Live endpoint validation

Work from the Switchyard root. Use this procedure when the user asks to validate
provider behavior, especially reasoning efforts and `extra_body` arguments.
Offline schema checks and synthetic providers cannot establish live support.

## Establish the authorized run

Use existing session authorization; do not ask again for an already authorized
run. A general configuration review or a request to edit this skill is not permission
to send model traffic. Before requesting missing authorization, prepare a concrete
matrix and command using the selected `routes_*.toml`, effective configuration,
endpoint URLs, models/targets, credential environment names, and proposed limits.
Require explicit route-source selection; do not infer it from the current deployment
or probe all discovered providers. If only some targets are authorized, isolate
those targets and their dependencies in a private temporary configuration before
using a helper that iterates the whole file. Preserve their effective settings and
record the subset's relationship to the selected source.

Bound total requests (including discovery, tool continuations, retries and gateway
fallbacks), output tokens per request, per-request timeout and whole-run duration.
Choose limits appropriate to the selected model's reasoning budget. Explain that
real calls may incur cost; token caps are not a known dollar budget. No automatic
retry loop: stop on exhausted limits, authentication failures or rate limits, and
report incomplete coverage. A field rejection can be recorded and followed by the
remaining independent cases within the approved matrix. Do not silently broaden
models, fields, endpoints or budget in response to failure.

Use synthetic prompts and harmless synthetic tools. Read only required credentials
through the existing environment/approved credential path; never print keys or
rendered secret headers. Render header placeholders into a private temporary file
with restrictive permissions, not a tracked file. Do not alter deployed routing,
network access, credentials or shared services to make a probe work.

## Build a discriminating matrix

Identify each distinct effective client/endpoint, API format, model and defaults
combination. Link equivalent targets/aliases to a shared case only when these
settings match. Never extrapolate a result across providers or models with similar
names. Include the selected task classes and provider-specific aliases, keeping
public effort labels separate from native values.

For each setting in scope, specify the exact outgoing JSON path/value and expected
observable result. Use current provider evidence and gateway serialization to
choose the wire shape; do not assume `reasoning_effort`, nested `reasoning`, thinking
budgets or template options are interchangeable.

| Case | What to execute and compare |
| --- | --- |
| Configured default | Complete request with effective target defaults, including its configured reasoning effort and other `extra_body` fields |
| Field omitted | Same request with the tested control absent, where omission is valid; absence is distinct from `null`, `false`, or a native disabled value |
| Supported alternative | Change one control at a time to each in-scope, documented model-specific value; preserve all unrelated fields |
| Caller override | Through the gateway, compare absent versus explicit caller values; verify top-level shallow merging and replacement of an entire nested object such as `reasoning` |
| Coupled fields | Test documented interactions, such as a thinking budget with the output cap or tool choice, when individual-field success cannot establish compatibility |

Keep negative tests optional and within the authorized request count. A deliberately
invalid value can expose validation or silent dropping, but its acceptance does not
prove that valid values take effect. Do not brute-force speculative argument names.
For fields affecting tools, structured output or streaming, use a prompt/assertion
that exercises that feature; a simple text response cannot validate all options.

## Execute the appropriate path

Inspect the current helpers before running them. Existing entrypoints:

```bash
python3 scripts/check_capabilities.py SELECTED_RENDERED_FILE --timeout 60 --max-output-tokens 4096 --report PRIVATE_REPORT
make test routes_PROVIDER.toml TEST_BASE_URL=SELECTED_GATEWAY_URL TEST_TIMEOUT=60 TEST_MAX_OUTPUT_TOKENS=4096
```

Replace placeholders with the explicitly selected source/endpoint and private paths;
adjust limits to the approved run and enforce an outer deadline (for example with
`timeout`). These commands send real traffic and are alternatives for different
coverage, not mandatory steps to execute together.

- **Direct provider:** `check_capabilities.py` groups targets by client, model,
  `extra_body`, and required context window. Each successful group sends five
  generation requests: text, automatic tool selection/result, and forced tool
  selection/result, plus model-metadata discovery. Inspect discovery request counts
  separately. The helper composes configurations but does not interpolate header
  placeholders itself. It forces streaming and replaces configured output-token
  limits with its CLI cap; for Responses it also sets `store=false`. It verifies
  text/tool completion, not reasoning semantics, and its ordinary report does not
  capture all reasoning metadata or usage. Do not claim it validated an overwritten
  field or every candidate value just because the group passed.
- **Gateway:** `make test` sends route smoke requests through the selected running
  gateway. It does not sweep caller overrides or reasoning values. Verify its
  mounted configuration and image identity against the candidate before attributing
  results to local edits. Prefer explicit passthrough aliases to isolate targets;
  automatic routing/fallback can otherwise make a different model's success look
  like the selected target passed. Record the actual selected target where observable.

For a reasoning/argument matrix beyond those helpers, execute a small task-local
harness using the actual complete wire requests and approved credentials. Reuse
existing transport/SSE parsing where suitable, but inspect its overrides and lost
metadata first. Preserve the candidate's fields under test; if a configured token
limit exceeds authorization, report it as untested or obtain a larger budget.
Capture the sanitized serialized request and response metadata needed for each
assertion. Do not invent CLI flags or treat a helper's settings hash as wire evidence.

Direct calls isolate provider acceptance; repeat relevant cases through a matching
gateway when validating translation and caller overrides. For an undeployed
candidate, an isolated local gateway with the matching build/config can exercise
the live upstream within authorization. Do not deploy merely to validate it. If
only one path is accessible/authorized, execute that path and mark the other untested.

Consume streams through terminal completion. Check HTTP errors, SSE error events,
incomplete/truncated responses, tool-call arguments and tool-result continuation as
applicable. A successful status/header alone is not a passing test.

## Interpret and retain evidence

Record per case: timestamp, source/config identity, target/alias, endpoint, requested
and reported model, API format, sanitized request delta, status/terminal event,
provider request ID when available, usage/reasoning-token counts when supplied,
elapsed time, assertion and outcome. Keep raw responses/logs private and outside Git;
redact provider error text before including it in a local Markdown report.

Distinguish **rejected**, **accepted but effect unverified**, **effect observed**,
and **not tested/inconclusive**. Gateway wire capture or a built synthetic transport
test can establish that a field was forwarded; it cannot prove the live provider
honored it. Provider metadata, documented reasoning usage or a feature-specific
observable result may strengthen that conclusion. HTTP 200, an echoed request
field, a plausible answer, or one latency difference is insufficient. Hidden
reasoning need not be exposed to test the request contract; do not ask the model
to reveal its private reasoning as proof.

Keep direct-provider and gateway results separate. Report documentation/runtime
disagreements without generalizing them to other models. Preserve failed cases and
budget/access gaps. After authorized edits, rerun affected cases within the remaining
budget; otherwise mark the revised candidate unverified. End with tested coverage,
actual outcomes, remaining uncertainty and any concrete configuration corrections
supported by the evidence.
