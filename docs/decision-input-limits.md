# Decision input limits

Switchyard fits classifier input to the decision service's serving limit.
Applications continue sending their complete requests; fitting does not trim
the answering-model request. Provider-side context limits still apply separately.

The discovered `routes_*.toml` configurations use:

```toml
[routes.stage.classifier]
model = "auto"
max_input_tokens = "auto"
truncate_input = true
```

The served model and context limit come from `/v1/models`, not a hard-coded
model name or a prior deployment observation. The serving limit is authoritative;
a tokenizer's separate architectural maximum is not used as the serving budget.
The limit is refreshed on each classification, including when the model ID is
pinned but `max_input_tokens` is automatic.

The supported SGLang generic SystemOne scorer requires the formatted input to be
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
classifier deadline (`timeout_ms`, currently two seconds). Missing serving metadata,
unavailable tokenization, a changed prompt renderer or routing instructions that cannot fit by themselves
use the configured fallback; the gateway does not submit a known oversized
prompt to the scorer.

This implementation matches the pinned SGLang **generic SystemOne** renderer
used by Elephant, with up to 26 choice labels. Clef's joint-schema renderer is
different and is not covered by this tokenization mode. SGLang's general
`--allow-auto-truncate` flag does not fix Elephant's scoring path: that path
explicitly rejects overflowing prompts before the general truncation handler.

The image must include `patches/decision-api-input-budget.patch`, applied after
the standalone decision patch by the Dockerfile. Validate that the selected
image supports these fields before deploying the routes.

For QA, inspect `decision_api_input_prepared` events or routing evidence's
`input` object: `context_limit`, `input_tokens`, `original_request_bytes`,
`request_bytes`, and `truncated`. They contain sizes and flags, not prompt text.
The 20-question probe discovers the same hosted limit and checks token counts
for its small synthetic cases; gateway tests cover truncation itself.

Validation covers changing served limits, missing/invalid metadata, exact input
boundaries, tokenizer errors and deadlines, oversized instructions, Unicode tool
results, retained current tasks, unchanged answering requests and legacy routing.
Authorized live synthetic checks can additionally exercise an isolated gateway
through `/v1/decision`, without calling an answering provider. Pin the image,
routes hash, served model/limit and load when comparing fitting latency. A
successful size-boundary test does not establish routing or answer quality.
