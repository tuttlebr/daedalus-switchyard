# Decision input limits

The 2026-11-05 change fits classifier input at Switchyard. Applications continue
sending their complete requests; the answering model receives them unchanged.

Both automatic profiles now use:

```toml
[routes.stage.classifier]
model = "auto"
max_input_tokens = "auto"
truncate_input = true
```

The running model is `baselabs/elephant-1`. Its `/v1/models` metadata advertises
`max_model_len = 16384`. That serving limit is authoritative. The tokenizer's
separate architectural maximum is 262144 and is not used as the serving budget.
The limit is refreshed on each classification, including when the model ID is
pinned but `max_input_tokens` is automatic.

The current SGLang generic SystemOne scorer requires the formatted input to be
strictly shorter than the serving context. Thus a 16384-token context allows
16383 input tokens on this scoring path. Switchyard counts the entire prompt,
including routing instructions, options and the hosted chat template, through
the service's `/v1/tokenize` endpoint. It does not equate bytes with tokens.

Requests that fit keep their selected conversation context. Oversized state is
replaced by marked excerpts, preserving both ends of each field. The opening
task, latest ordinary user message and newest context receive higher priority;
unused shares from short fields are reassigned to larger excerpts. System
instructions, tool arguments and tool results are included in the budget.
Routing instructions and option definitions remain intact. Each candidate is
token-counted again before scoring. Canonical state is sent as a JSON string,
which SystemOne accepts, to avoid differences in numeric JSON formatting between
Rust and Python.

The latest user instruction is retained even when older than the six-message
tool window. Tool-result messages normalized to the user role do not replace it.
Private reasoning continues to be omitted. Unsupported media follows the
existing fallback policy.

With truncation enabled, the old `max_request_bytes` rejection gate is bypassed:
a request larger than 64 KiB can still fit the model and need no truncation.
Without truncation, existing configurations retain their byte guard and numeric
token ceiling. Discovery, tokenization, fitting and scoring share the existing
two-second deadline. Missing serving metadata, unavailable tokenization, a
changed prompt renderer or routing instructions that cannot fit by themselves
use the configured fallback; the gateway does not submit a known oversized
prompt to the scorer.

This implementation matches the pinned SGLang **generic SystemOne** renderer
used by Elephant, with up to 26 choice labels. Clef's joint-schema renderer is
different and is not covered by this tokenization mode. SGLang's general
`--allow-auto-truncate` flag does not fix Elephant's scoring path: that path
explicitly rejects overflowing prompts before the general truncation handler.

The image must include `patches/decision-api-input-budget.patch`, applied after
the standalone decision patch. Rebuild before deploying these profiles.

For QA, inspect `decision_api_input_prepared` events or routing evidence's
`input` object: `context_limit`, `input_tokens`, `original_request_bytes`,
`request_bytes`, and `truncated`. They contain sizes and flags, not prompt text.
The 20-question probe discovers the same hosted limit and checks token counts
for its small synthetic cases; gateway tests cover truncation itself.

Validation covers changing served limits, missing/invalid metadata, exact input
boundaries, tokenizer errors and deadlines, oversized instructions, Unicode tool
results, retained current tasks, unchanged answering requests and legacy routing.
Live synthetic checks against the running Elephant service also exercise the
new local gateway through `/v1/decision`, without calling any answering provider.

The final checks passed 396 Rust tests, runner Clippy, 29 focused Python tests,
both server configuration validations and offline Helm checks. All ten image
patches apply in order and reproduce the tested router source. Live Elephant
checks accepted a short request unchanged and truncated synthetic 240–450 KB
requests to 15663–15802 tokens. Those large cases completed in 1.65–1.75 seconds
on this run, within the existing two-second deadline; concurrency can affect
that margin. These checks validate the size boundary, not routing quality.

The pre-change files are in
`.deploy-backups/switchyard-input-x0_ek5h4/`. Local build/test evidence is under
`/tmp/switchyard-input-x0_ek5h4/`. No production workload has been redeployed by
this change.
