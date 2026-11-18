# Validation reference

Paths and commands below are relative to the Switchyard root. Inspect the current
files before using commands; this reference is a navigation aid, not a frozen
provider capability matrix.

## Evidence and source map

| Evidence | Where to look | What it establishes |
| --- | --- | --- |
| Provider fragments and entrypoints | `config/llms/**/*.toml`, `routes_*.toml` | Intended clients, target defaults, and include selection |
| Effective configuration | [render_routes.py](../../../../scripts/render_routes.py) | Recursive merge order, header interpolation, flattened input |
| Provider contract snapshots | `config/api-spec/*` | Endpoint schemas with their own provenance and limitations |
| Build identity and patch order | [Dockerfile](../../../../Dockerfile), [Makefile](../../../../Makefile) | Pinned upstream revision and local changes actually applied |
| Gateway behavior | Exact pinned upstream source plus `patches/` | Config deserialization, URL construction, request/response translation |
| Local expectations | [README.md](../../../../README.md), `docs/`, `tests/` | Project invariants and regression coverage, not independent provider truth |

The server source is fetched during the Docker build; there is no local `src/`
tree in this repository. If the needed implementation is not present, inspect an
existing checkout or fetch the exact Dockerfile revision into a temporary directory
and apply patches in build order. Do not use upstream HEAD as evidence for the
pinned gateway. Record inability to inspect/build as a compatibility evidence gap.

Local specs can be official extracts, documentation-derived profiles, or incomplete
snapshots. Read `info`, `servers`, operation paths, `externalDocs`, `x-provenance`,
and provider compatibility extensions before selecting a request schema. Missing
provenance is itself an evidence gap. Search JSON selectively rather than loading
megabytes into context. The current Together snapshot explicitly identifies
inherited wire shapes as assumptions; do not promote them to official constraints.

Consult current official API references, model documentation/catalogs, and release
notes. Prefer URLs recorded in the spec, then the provider's official documentation.
Record checked date and supporting section/JSON pointer. If documentation, snapshot,
and runtime disagree, report the disagreement and each source's scope. Do not
rewrite a spec to justify the TOML. Refresh specs only when within the user's scope,
preserving provenance and distinguishing upstream text from local corrections.

## Checks that matter

- Resolve the provider operation from the actual client `format`. A Responses-only
  spec cannot validate Chat Completions. Account for server URLs that already include
  `/v1` or `/api/v1`; compare the final URL produced by the gateway, not path strings
  in isolation. Check auth/header names and interpolation requirements without
  printing secrets. `base_url` is a base, not the complete inference operation URL.
- Follow `$ref`, `allOf`, `oneOf`, `anyOf`, required fields, nullability, enums,
  bounds, and explicit unsupported-field notes. Use a validator appropriate to the
  OpenAPI dialect if automating validation: OpenAPI 3.0 `nullable` semantics differ
  from 3.1 JSON Schema. Validate representative complete requests against the
  operation's request schema; whole-spec validation only checks the spec document.
- Verify model availability on the selected endpoint and model-specific reasoning,
  tool, modality, output-limit, and structured-output support. Do not infer these
  from the model name or from another provider serving a similarly named model.
- Trace `extra_body` defaults through serialization and caller overrides. The
  documented merge is shallow at the top level: caller `reasoning` can replace the
  whole default reasoning object. Check both absent and explicit caller values.
  Shipped targets require `stream = true`: the local required-streaming patch
  enforces SSE even for explicit false. Verify callers can consume SSE, including
  automation planning. This user-selected policy supersedes the earlier no-stream
  default rule. Keep Responses-only `truncation` out of Chat defaults.
- Check state/persistence fields, full-history replay, tool-call/result IDs, and
  reasoning history. Inspect request translation and successful/failed terminal
  events, including SSE errors after HTTP 200. A valid request alone does not
  establish that Switchyard can consume the response or continue a tool exchange.
- Check target/client references, unique IDs, aliases, classifier choices and
  fallback, required credential/header environment names, and shadowing by later
  includes. Assess each fragment in every route entrypoint that consumes it.

## Offline checks

Use Python 3.11+ for `tomllib`. Create a temporary directory for composed/rendered
files. `--compose-only` leaves header placeholders unresolved and needs no secrets:

```bash
python3 scripts/render_routes.py routes_PROVIDER.toml /tmp/UNIQUE_DIR/composed.toml --compose-only
python3 scripts/render_routes.py routes_PROVIDER.toml --list-env
python3 scripts/list_api_key_envs.py --json /tmp/UNIQUE_DIR/composed.toml
python3 -m unittest discover -s tests -p 'test_render_routes.py' -v
python3 -m unittest discover -s tests -p 'test_provider_routes.py' -v
```

Replace `routes_PROVIDER.toml` and `/tmp/UNIQUE_DIR` with actual discovered paths.
Compose all affected entrypoints; the two test modules cover all discovered routes.
Parse orphan fragments separately and report that integrated validation is missing.
Use synthetic non-secret header values if testing rendering; do not overwrite `.env`.
Avoid printing complete rendered headers, which may contain sensitive values.

For a config change, run relevant synthetic transport tests with the matching built
binary. These use local fake providers; they do not verify current upstream behavior:

```bash
SWITCHYARD_TEST_BINARY=/path/to/matching/switchyard-server python3 -m unittest discover -s tests -p 'test_responses_transport.py' -v
SWITCHYARD_TEST_BINARY=/path/to/matching/switchyard-server python3 -m unittest discover -s tests -p 'test_stream_contract.py' -v
```

For Chat/Fireworks changes, include `test_fireworks_chat_transport.py`; for broader
transport changes run the full local suite. The binary-dependent tests skip when
`SWITCHYARD_TEST_BINARY` is absent. Report that as unverified, not passing transport
coverage. Inspect test assertions for the actual fields and client paths covered.
Add a focused behavioral regression only when an authorized change introduces
behavior not covered; do not hardcode new provider values just to mirror TOML.

`make validate ROUTES_FILE=routes_PROVIDER.toml` renders, lints/templates Helm and
performs a Kubernetes client dry run. It is not provider-spec validation and may
need header variables, Helm, and Kubernetes tooling. Neither it nor TOML parsing
proves the server accepts the config.

## Live checks, only within authorization

Read [live validation](live-validation.md) when preparing or executing authorized
endpoint tests. It covers test selection, executable helpers and their limits,
reasoning/`extra_body` comparisons, and the evidence needed to report results.

Log reads use `kubectl --context SELECTED_CONTEXT -n daedalus`, bounded with
`--since`, `--tail`, and a request timeout. Discover the gateway pod first; do not
switch the active context. In the monorepo, the parent's bounded runtime collector
can capture only the gateway via `--pod-prefix switchyard-`. Missing access must
not block independent local validation. Logs do not authorize new traffic or rollout.
