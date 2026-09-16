# Switchyard on Daedalus

This repository builds NVIDIA-NeMo Switchyard v0.2.0 with a local target-model
alias patch and deploys it as a non-root Helm workload in the `daedalus`
namespace. The default Fireworks route exposes model `daedalus` on the OpenAI
Chat Completions, OpenAI Responses, and Anthropic Messages endpoints.

`routes_fireworks.toml` uses the stage router with `efficient_first`: ordinary
turns use the weak target; tool-result signals can escalate to the strong target.
Both targets call Fireworks Nemotron 3 Ultra NVFP4 through the Responses API,
with `medium` and `max` reasoning effort respectively.

```toml
[targets.strong]
id = "fireworks-strong"
model = "accounts/fireworks/models/nemotron-3-ultra-nvfp4"
llm_client = "fireworks"
extra_body = { reasoning = { effort = "max" }, truncation = "auto", reasoning_history = "interleaved" }

[targets.weak]
id = "fireworks-weak"
model = "accounts/fireworks/models/nemotron-3-ultra-nvfp4"
llm_client = "fireworks"
extra_body = { reasoning = { effort = "medium" }, truncation = "auto", reasoning_history = "interleaved" }
```

`id` identifies the routing target. The optional `model` names the model sent to
the provider; omitting it preserves the original behavior where `id` serves
both purposes. Distinct routing IDs keep each target's defaults independent,
even when they share a client and provider model. Separate clients alone do
not solve this for the stock v0.2.0 stage router, which also selects targets by
ID. The Dockerfile applies `patches/target-model-aliases.patch` to the pinned
source. **Rebuild the image before deploying this Fireworks configuration**;
the unpatched image rejects the new `model` field.

Switchyard treats `extra_body` as a default-only, shallow top-level merge. An
explicit `reasoning` object in the inbound request wins. The route advertises
`reasoning = false` so clients using discovery do not automatically supply an
effort that overrides the selected target.

The Fireworks targets use native Responses fields. Fireworks' [NIM compatibility
guide](https://docs.fireworks.ai/tools-sdks/nim-compatibility) documents that
unrecognized `chat_template_kwargs` keys are discarded. The targets therefore
omit `medium_effort`, `truncate_history_thinking`, and `force_nonempty_content`.

NAT's `INFO` message beginning “Expecting generate request payloads” describes
its inbound workflow schema at startup. It is not a provider validation error,
and does not require changing these upstream targets to Chat Completions.

## Files

- `routes_fireworks.toml`, `routes_openai.toml`, and `routes_local.toml` are the
  selectable deployment sources for clients, targets, and routes.
- `charts/switchyard/` contains the Helm chart.
- `Dockerfile` builds the pinned upstream v0.2.0 commit with the target-model alias patch.
- `scripts/deploy.sh` reconciles the API-key Secret, forces a fresh pod rollout,
  deploys Helm, and checks rollout, EndpointSlice, and HTTP health.

The chart mounts the selected routes file as `routes.toml` from a ConfigMap and
starts Switchyard directly.
It also exposes `/health` for Kubernetes probes and `/metrics` for an optional
Prometheus `ServiceMonitor`.

## Validate

```bash
make validate
make validate ROUTES_FILE=routes_local.toml
```

This lints the chart, renders it into the `daedalus` namespace, and runs
Kubernetes client-side validation. Route policy is intentionally not validated,
so the selected routes file can be changed freely while experimenting.

The optional integration tests inspect actual upstream request bodies using a
local mock, including both effort tiers, streaming tool history, escalation,
and caller overrides. They do not contact Fireworks:

```bash
docker build -t switchyard:fireworks-schema-test .
SWITCHYARD_TEST_IMAGE=switchyard:fireworks-schema-test python3 -m unittest discover -s tests -v
```

## Test deployed routes

```bash
make test routes_fireworks.toml
# Equivalent, with the default routes file:
make test
make test ROUTES_FILE=routes_local.toml
```

This sends one live, non-streaming `/v1/responses` request for **every entry in
`[routes]`**, using its `id` as the requested model. For the Fireworks file,
that includes `stage`, `deep`, `deep_max`, and `cheap`. The TOML selects the
route IDs to test; the running server supplies their routing policy, upstream
settings, and credentials. Deploy the intended TOML first. This command does
not deploy or check that the server's configuration matches the local file.

By default it opens a temporary localhost port-forward to
`daedalus/service/switchyard:4000` using `EXPECTED_CONTEXT` and closes it on
completion or interruption. This requires Python 3.11+ and `kubectl` access to
the deployed Service and pods. You can instead supply a reachable Switchyard
URL (with or without `/v1`), which does not require `kubectl`:

```bash
make test routes_fireworks.toml TEST_BASE_URL=http://localhost:4000/v1
make test routes_fireworks.toml TEST_TIMEOUT=180 TEST_MAX_OUTPUT_TOKENS=8192
```

The prompt asks for `OK`, with a default budget of 4096 output tokens to allow
for reasoning and a 120-second HTTP timeout per route. These are real inference
requests and use the deployed providers. No reasoning effort is sent, so each
route keeps its configured default. A pass requires a completed response with
nonempty assistant text; HTTP errors, incomplete/failed responses, invalid JSON,
and empty output fail. Every route is attempted, with HTTP status, elapsed time,
selected-model header, and a short response or error printed. Any failed route
returns a nonzero exit status.

`TEST_NAMESPACE` and `TEST_SERVICE` override the port-forward destination.
The route tester's local HTTP-fixture tests run without provider credentials:

```bash
python3 -m unittest discover -s tests -p 'test_route_smoke.py' -v
```

## Build, publish, and deploy

No official v0.2.0 container is assumed. Build the pinned upstream commit and
push it to a registry every Kubernetes node can pull from. Each route target
runs the complete image build, push, and guarded deployment sequence:

```bash
make deploy-route_fireworks
make deploy-route_openai
make deploy-route_local
```

For an immutable deployment, record the pushed manifest digest and provide it
as `IMAGE_DIGEST=sha256:...` during deployment.

To deploy an already-published image without rebuilding it, select the routes
file explicitly:

```bash
make deploy ROUTES_FILE=routes_local.toml
```

The defaults are image repository
`daedalus.ddns.me:5050/btuttle/switchyard`, tag `v0.2.0`, and Kubernetes context
`kubernetes-admin@kubernetes`; each can still be overridden as a Make variable.

Copy `.env.example` to `.env` and set every credential named by an
`llm_clients.*.api_key_env` entry in the selected routes file. `.env` is ignored
by Git. The deployment script discovers the unique names from that file,
creates matching Kubernetes Secret keys, and injects those keys into the
Switchyard container. It does not place credential values in Helm values or the
ConfigMap.

The script fixes the namespace to `daedalus`, release name to `switchyard`, and
Secret name to `switchyard-api-key` by default. Override `RELEASE_NAME` or
`SECRET_NAME` only when you also intend to change those resource names.

Each invocation adds a new non-secret restart annotation to the pod template.
This makes Secret and mutable image-tag updates take effect in newly started
containers. For each name discovered from the selected routes file, a value
supplied by the caller environment takes precedence over the same name in
`.env`. The Fireworks clients use `FIREWORKS_API_KEY`; unrelated variables are
ignored.

The default scheduling contract requires `amd64` and excludes the ARM64
`daedalus-06` worker. Publish a compatible multi-architecture image and override
`nodeSelector` and `affinity` together if that topology changes.

## Call the route

The Service defaults to `NodePort` and pins the external port to `30260`. From
a network that can reach a cluster node, use this base URL:

```text
http://<node-ip>:30260/v1
```

Inside the cluster, use this base URL:

```text
http://switchyard.daedalus.svc.cluster.local:4000/v1
```

Set `service.type=ClusterIP` to disable node-level exposure; the chart omits the
configured `service.nodePort` unless the Service type supports node ports.

For example:

```bash
curl http://switchyard.daedalus.svc.cluster.local:4000/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"daedalus","input":"Inspect and fix the failing tests."}'
```

Every routed response includes `x-model-router-selected-model` and
`x-model-router-rationale`. With the Fireworks aliases, the selected-model
header distinguishes `fireworks-weak` from `fireworks-strong`; the request sent
to Fireworks uses the full provider model ID from `model`.
