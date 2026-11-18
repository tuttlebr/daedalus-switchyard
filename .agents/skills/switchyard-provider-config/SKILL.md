---
name: switchyard-provider-config
description: Review Switchyard config/llms TOMLs against config/api-spec, current provider specifications, and the pinned gateway implementation; add or modify provider configurations through Q&A. Use for provider configuration audits and guided configuration edits, not deployment or general gateway optimization.
---

# Switchyard provider configuration

Work from the Switchyard repository root. Determine it from this skill's location,
not the caller's working directory. Read repository instructions and inspect Git
status before editing; preserve existing work, including untracked specs.

## Choose the mode

- **Review:** inspect every `config/llms/**/*.toml` unless the user selects a subset.
  Review is read-only; report concrete corrections without silently applying them.
- **Add or modify:** ask only for missing choices that affect the result, then
  implement the requested TOML changes and validate them. Existing authorization
  to edit is sufficient; do not add a confirmation gate for ordinary edits.
- If the request combines review and remediation, do both within that scope.

Discover `routes_*.toml`, their include dependencies, and `config/api-spec/*` on
each run. Do not hardcode the provider list. Track every selected fragment,
including ones with no route entrypoint or matching spec; those are coverage gaps,
not files to silently skip. Load effective configurations using
`scripts/render_routes.py` rather than concatenating TOML. Later includes override
earlier ones recursively; entrypoint values win last. Identify overrides that hide
or alter a fragment's settings.

## Review provider and gateway compatibility

Read [the validation reference](references/validation.md) for evidence sources,
wire-level checks, and executable validation commands.

For every client and target, establish three separate conclusions:

1. **Provider contract:** compare the actual endpoint and outgoing fields with the
   matching local API spec and current official provider documentation. Read spec
   provenance and compatibility notes first. Follow schema references/unions and
   model-specific restrictions; a provider-wide enum does not establish support
   for a particular model. Verify model IDs and reasoning controls with current
   official evidence. If unavailable, mark the claim unverified.
2. **Switchyard contract:** inspect composition, the pinned upstream implementation
   and applied patches, defaults/overrides, request translation, and response/stream
   handling. A provider-valid field can still be dropped or rewritten by the gateway.
3. **Observed behavior:** distinguish offline configuration checks, built gateway
   tests with synthetic providers, and live provider/gateway probes. Never label
   an untested layer as passing because a different layer passed.

Validate the complete synthetic outgoing request, not `extra_body` alone: the
gateway supplies fields such as `model`, `input`/`messages`, tools, and streaming.
Check unsupported fields as well as types/enums and nested constraints. Schema
acceptance with permissive additional properties is not proof of semantic support;
absence from an incomplete local schema is not proof of rejection.

Task classifications are `max`, `high`, `low`, and `instant`; public aliases and
provider-native reasoning values are distinct contracts. Public `none` is retired,
but a provider-native value named `none` is not thereby invalid. Preserve intentional
model/effort differences and caller overrides. Do not equate `instant` with disabled
reasoning or copy one provider's reasoning object to another.

## Guided additions and edits

Read the relevant existing configuration and specs before asking questions. Use
short rounds of Q&A, offering supported choices with practical tradeoffs. Ask about
only unresolved items:

- Provider and intended file; serverless/dedicated endpoint, transport, and exact
  model IDs. Recommend only choices supported by current evidence.
- Model and provider-native reasoning settings for each of the four task classes;
  desired quality, latency, or cost tradeoff where it changes that mapping.
- Needed tools, streaming, structured output, multimodal input, history handling,
  token limits, and other provider options that affect compatibility.
- Credential **environment variable name**, header variable names, and whether a
  new provider should get its own `routes_<provider>.toml` entrypoint.

For a narrow edit, preserve all unspecified settings. Never ask for API key values
in chat or put them into TOML. Do not guess an endpoint/model, silently substitute
a different provider, or reduce requested capabilities to make validation pass.
Explain unsupported requests and ask for a supported alternative while continuing
independent checks. If gateway code support is required, explain the precise gap;
configuration-edit authorization does not imply an unrelated gateway rewrite.

Edit the owning fragment, preserving comments and formatting. For a new complete
provider configuration, reuse the existing classifier and routing fragments in its
entrypoint and ensure all target references resolve. Do not rewrite shared routing
policy merely to add a provider. Recompose every affected entrypoint and inspect
the effective diff; stale entrypoint overrides can defeat a correct fragment edit.

## Boundaries and result

Local review/editing does not authorize deployment, credential changes, or model
traffic. Require an explicitly selected route source for live probes or deployment,
using prior authorization when present. Never probe every provider merely because
the review covers every file. Preserve working LAN endpoints and consumers.

When a change reaches a shared agent/transport/stream contract in the monorepo,
use the parent `daedalus-co-design` skill and assess its affected consumers. Keep
ordinary configuration review scoped to Switchyard. On relevant reviews, inspect
bounded read-only gateway logs in namespace `daedalus` with an explicitly pinned
Kubernetes context, if available; report access gaps and do not infer healthy
behavior from missing logs. Keep raw logs private, outside tracked documents.

Return coverage for every selected file/client/target and effective route, with
provider-spec, Switchyard, and live-verification statuses kept separate. For each
finding include severity, TOML location, evidence (spec JSON pointer or official
URL and access date; gateway source/test), impact, and the smallest correction.
Include unknowns, skipped checks, tests run, and changed files. Use local Markdown
for saved reports. End an edit with the concrete diff and validation outcome;
do not imply that local changes are deployed or provider-tested.
