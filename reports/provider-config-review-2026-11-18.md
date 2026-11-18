# Provider streaming and reasoning review — 2026-11-18

All six provider fragments and six composed entrypoints define streaming for all
24 generation targets. The tested gateway enforces SSE even when callers omit
`stream` or send `false`. Reasoning defaults are present everywhere, but Nebius
has a documented wire-contract mismatch and Baseten has conflicting model/API
documentation. No live inference requests were sent, and no provider configuration
was changed by this review.

## Coverage

Each row covers `config/llms/<provider>.toml`, its single `llm_clients.<provider>`,
and `routes_<provider>.toml`. Each entrypoint includes the provider fragment,
`config/decision/elephant.toml`, and `config/routing/four-category.toml`.
Composed targets exactly match their provider fragment: no later include or
entrypoint shadows the settings. No orphan provider fragments were found.

All entrypoints expose `daedalus`, plus `daedalus/max`, `daedalus/high`,
`daedalus/low`, and `daedalus/instant`. Classifier choices and the `high` fallback
resolve. All clients use `openai_responses`; each base URL gets `/responses` appended.
The decision API classifier is a separate, non-generation operation.

| Provider | Exact model | max / high / low / instant defaults | Streaming | Provider evidence |
| --- | --- | --- | --- | --- |
| Baseten | `deepseek-ai/DeepSeek-V4.1-Flash-1017-Fast` | `high / medium / low / minimal` | 4/4 | Responses schema accepts all four; model guide conflicts, see below |
| Fireworks | `accounts/fireworks/models/deepseek-v4p1-flash-1017` | `max / high / low / low` | 4/4 | Model exists; these model efforts are documented for Chat; Responses reasoning object is permissive |
| Nebius | `deepseek-ai/DeepSeek-V4.1-Flash-1017` | `max / xhigh / medium / low` | 4/4 | Top-level field is not declared by Responses; nested `max` fails its published schema |
| OpenAI | `gpt-6.1-terra` | `max / high / low / low` | 4/4 | Model documentation supports these exact efforts; `minimal` is not a supported Terra effort |
| OpenRouter | `deepseek/deepseek-v4.1-flash-1017:nitro` | `max / high / low / minimal`, all with `enabled=true` | 4/4 | Responses schema and general reasoning guide support these settings; actual backend effects vary |
| Together | `deepseek-ai/DeepSeek-V4.1-Flash-1017` | `high / medium / low / none` | 4/4 | Responses guide supports these values and documents `none` disabling DeepSeek V4.1 reasoning |

Nebius uses `extra_body.reasoning_effort`; every other provider uses
`extra_body.reasoning.effort`. Every row passed built synthetic gateway transport
checks; every row remains **live-provider unverified**. Configured model strings
were found in official catalogs/guides for Baseten, Fireworks, OpenAI, OpenRouter
(base model), and Together. The exact Nebius model's current availability and its
regional endpoint were not independently established.

## Findings and verification gaps

### P2 — Nebius mixes Chat reasoning fields with the Responses operation

Locations: `config/llms/nebius.toml:4`, and `extra_body` at lines 12, 18, 24, 30.
All four targets send top-level `reasoning_effort`. The current official
[Responses schema](https://docs.tokenfactory.nebius.com/api-reference/inference/create-a-response)
defines `#/components/schemas/CreateResponseRequest/properties/reasoning`, whose
`Reasoning.properties.effort` permits `none`, `minimal`, `low`, `medium`, `high`,
and `xhigh`. It does not declare top-level `reasoning_effort`. The
[Chat schema](https://docs.tokenfactory.nebius.com/api-reference/inference/create-chat-completion)
does declare that field and additionally permits `max`.

This is a confirmed documentation/config mismatch, not a demonstrated live
rejection: Responses allows additional properties, so all four existing payloads
pass schema validation without checking that field's meaning. They could be
ignored or supported by an undocumented extension. Synthetic gateway capture
confirms Switchyard forwards the top-level field unchanged. Adding a nested caller
override can therefore leave both forms in the outgoing body.

Smallest correction candidate: use `reasoning = { effort = "..." }` for Responses.
The existing `xhigh`, `medium`, and `low` fit that schema. Do not mechanically move
`max`: nested `max` fails validation. Confirm live extension support or explicitly
choose a supported maximum mapping before changing that target. No transport or
endpoint change is needed merely to investigate this mismatch. There is no local
`config/api-spec/nebius.spec.json`; this review used the published schema privately.

### Verification gap — Baseten Responses and model-specific guidance disagree

Locations: `config/llms/baseten.toml:13`, `:19`, and `:31`.
The current official [Responses specification](https://docs.baseten.co/reference/inference-api/responses-openapi-spec.json)
matches the local enum at
`#/paths/~1v1~1responses/post/requestBody/content/application~1json/schema/properties/reasoning/properties/effort`:
`minimal`, `low`, `medium`, `high`. Existing requests satisfy it.

However, the [model reasoning guide](https://docs.baseten.co/inference/model-apis/reasoning.md#set-reasoning-depth)
lists `none`, `low`, `medium`, `high`, `xhigh`, `max` for the exact 1017 Fast model,
omits `minimal`, and maps `medium` to `high`. That guide describes Chat's top-level
control, so it cannot establish that Responses rejects `minimal` or collapses the
configured max/high tiers. Live Responses comparison is needed. Do not substitute
Chat fields or expand the Responses enum based on that guide alone.

### Other provider conclusions

- **OpenAI:** [Terra model documentation](https://developers.openai.com/api/docs/models/gpt-6.1-terra)
  explicitly supports `max`, `high`, and `low`. Keeping `instant=low` is valid.
  `text.verbosity=low`, `reasoning.summary=concise`, and `store=false` pass the local
  official Responses schema. The shared shallow merge allows an explicit caller
  reasoning object to replace both default effort and summary; this is existing
  override behavior, not enforced target effort.
- **Fireworks:** The [model catalog](https://fireworks.ai/models/deepseek-ai/deepseek-v4p1-flash-1017)
  confirms the model ID. Its [Chat reference](https://docs.fireworks.ai/api-reference/post-chatcompletions)
  documents distinct DeepSeek V4.1 `low`, `high`, and `max` tiers. The
  [Responses reference](https://docs.fireworks.ai/api-reference/post-responses)
  accepts a reasoning object but does not constrain its effort values. Thus the
  mapping is consistent with documented model tiers, while Responses-specific
  application of those values remains unverified. `instant=low` is intentional.
- **OpenRouter:** `#/components/schemas/ReasoningEffort` and `ReasoningConfig` accept
  the current fields. The [reasoning guide](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
  supports `effort` and `enabled` on Responses. The
  [model page](https://openrouter.ai/deepseek/deepseek-v4.1-flash-1017) describes Nitro
  as throughput-based provider selection. A valid enum does not guarantee identical
  effort behavior across those providers; do not certify all backend semantics
  from this config alone.
- **Together:** The [Responses guide](https://docs.together.ai/docs/inference/responses-api#control-reasoning)
  supports the configured values, but forwards efforts to model templates, so
  their effect is model-dependent. It specifically documents reasoning disabled
  by `none` on DeepSeek V4.1 Flash. That native value is not the retired public
  `daedalus/none` alias. No requirement in this request makes `instant` always-on.
  Its [catalog](https://docs.together.ai/docs/serverless/models) lists the exact model.

All web evidence was checked on 2026-11-18. The local Together spec is explicitly
a documentation-derived compatibility profile, not an official schema export.
Baseten and Fireworks local snapshots lack provenance; current official documents
were consulted rather than treating those snapshots as authoritative by themselves.

## Validation performed

- Parsed every fragment and composed every entrypoint using `compose_source` from
  `scripts/render_routes.py`; checked all clients, targets, aliases and defaults.
- Validated 24 complete synthetic Responses request bodies against the five local
  operation schemas plus the current published Nebius schema, using OpenAPI 3.0/3.1
  validators as appropriate. All were schema-accepted, with the permissive-schema
  limitations above. Separate Nebius nested-value checks accepted `xhigh`, `medium`,
  and `low`, and rejected `max`.
- 18 route-rendering tests and 4 provider-route tests passed.
- 2 streaming-contract tests and 3 Responses-transport tests passed against the
  final image binary: Chat and Responses caller paths, true/false/omitted stream,
  terminal stream errors, every native alias's defaults/caller overrides and tool
  history, snapshot replacement, and non-streaming auxiliary operations.
- Tested image: `daedalus.ddns.me:5050/btuttle/switchyard:v0.3.0`, local image ID
  `sha256:7ae56e2dcdc895d11bf999543a89708ccd54065856c224a5feb5de783ce86afe`.
  Upstream revision label: `336196f6fbfc97ddc71c1700f6092e564e9f23c2`.
  Inspected the pinned builder source and repository `required-streaming.patch`.
  An older builder binary failed the auxiliary-operation checks; the final image
  binary above passed them. Only the final-image results establish current coverage.
- Bounded read-only logs: context `kubernetes-admin@kubernetes`, namespace
  `daedalus`, gateway pod `switchyard-cccf8cb77-w7trt`, last 30 minutes capped at
  150 lines, 15-second request timeout. No ERROR/WARN lines in that sample; this
  neither verifies every provider nor attributes live traffic to local candidates.

Private evidence and schema-check script: `/tmp/provider-review-djk8z9va/`.
No credentials were loaded for model requests. No deployment, endpoint, credential,
or provider configuration changes were made. Existing Baseten and skill edits
were preserved. Switchyard HEAD was `309831a9483e9524eb78ca1075bfe6860f7dccf0`.

## Proposed live coverage, not executed

For explicitly authorized route sources, compare each distinct configured effort
with one omitted-effort baseline per provider. Across all six files this is 22
distinct configured requests plus 6 baselines. Use synthetic input, streaming,
at most 4096 output tokens per request, a 60-second request deadline, and a
15-minute run deadline, with no retries. Capture terminal completion, model identity,
usage/reasoning metadata and request IDs. Acceptance alone remains insufficient
to prove distinct reasoning depth. Nebius alternate nested-field tests require
additional explicitly bounded cases. Live traffic, its potential charges, and
the route sources must be authorized before execution.
