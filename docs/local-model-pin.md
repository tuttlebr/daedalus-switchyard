# Local model request pin

The scalar local targets request `Qwen/Qwen3.6-35B-A3B-FP8`. The serving
artifact is pinned separately to revision
`95a723d08a9490559dae23d0cff1d9466213d989`; a request model name alone does not
verify the loaded weights or configuration.

| Route configuration | Targets using this model |
| --- | --- |
| `routes_openai.toml` | `local`, `hindsight_local` |
| `routes_custom.toml` | `local`, `hindsight_local`, `none` |

Existing public aliases, local client endpoint, credentials, classifiers and
other target choices retain their configured behavior. The OpenAI profile's
`none` route continues to use its existing cloud target. The server retains
`Qwen/Qwen3.8-27B-FP8` as a compatibility alias, but these scalar targets use the
actual model name.

The inherited local default `reasoning_effort="medium"` is preserved for request
compatibility. This pinned Qwen3.6 template does not interpret that field as a
thinking mode or token budget. Its existing `chat_template_kwargs.enable_thinking`
default controls thinking: enabled for `local`, disabled for `hindsight_local`
and the custom profile's `none` target. Trusted caller precedence is unchanged.

For a configuration update, merge these model-field changes into the active
configuration while preserving concurrent route and deployment changes. Do not
replay a complete older route file over the current configuration. Keep the
existing endpoint and authentication contracts and verify both public protocol
paths before applying a separately reviewed deployment. This model pin does not
establish output quality or an application benefit.
