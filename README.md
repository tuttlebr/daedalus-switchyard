# Switchyard on Daedalus

This repository builds NVIDIA-NeMo Switchyard v0.2.0 and deploys it as a
non-root Helm workload in the `daedalus` namespace. The default route exposes
`switchyard/stage` on the OpenAI Chat Completions, OpenAI Responses, and
Anthropic Messages endpoints.

The default capability classifier exposes one route backed by a local Qwen
judge and two Fireworks DeepSeek completion signatures. The judge decides
whether the low-effort target can complete the request; Switchyard then sends
the original request to either the `weak` or `strong` completion target:

```toml
[targets.strong]
id = "accounts/fireworks/models/deepseek-v4-flash-0731"
llm_client = "strong"
extra_body = { reasoning = { effort = "max" } }

[targets.weak]
id = "accounts/fireworks/models/deepseek-v4-flash-0731"
llm_client = "weak"
extra_body = { reasoning = { effort = "low" } }

[targets.classifier]
id = "Qwen/Qwen3.8-27B-FP8"
llm_client = "llm_classifier"
extra_body = { chat_template_kwargs = { enable_thinking = false } }

[routes.stage]
id = "switchyard/stage"
type = "llm_classifier"
mode = "capability"
classifier_target = "classifier"
strong_target = "strong"
weak_target = "weak"
base_threshold = 0.5
threshold_step = 0.1
```

The classifier client uses OpenAI Chat Completions at
`http://inference-gateway.daedalus-dynamo.svc.cluster.local:32359/v1`. The live
Dynamo Service exposes port `32359`, not port `80`. Both completion targets use
OpenAI Responses at Fireworks.

Separate completion `llm_client` names are required in Switchyard v0.2.0. A
client stores only one backend per model ID, so same-ID signatures on one client
would collide and all but one target's configuration would be discarded.

Switchyard treats `extra_body` as a default-only, shallow top-level merge. An
explicit `reasoning` object in the inbound request wins. The example therefore
sets `routes.stage.reasoning = false`; Codex-style model discovery will not add
its own effort, leaving the selected target in control. If callers should be
allowed to override the routed effort, set route discovery to `true` and send
`reasoning = { effort = "..." }` in each request.

The classifier adds one upstream model call per routed request. A missing,
invalid, or inconsistent classifier verdict falls back to the `strong` target.
Session affinity is intentionally disabled, so each request can be judged using
the opening task and the configured three-turn recent window.

## Files

- `routes.toml` is the deployment source for clients, targets, and routes.
- `charts/switchyard/` contains the Helm chart.
- `Dockerfile` builds the upstream v0.2.0 tag commit as a standalone Rust server.
- `scripts/deploy.sh` reconciles the API-key Secret, forces a fresh pod rollout,
  deploys Helm, and checks rollout, EndpointSlice, and HTTP health.

The chart mounts `routes.toml` from a ConfigMap and starts Switchyard directly.
It also exposes `/health` for Kubernetes probes and `/metrics` for an optional
Prometheus `ServiceMonitor`.

## Validate

```bash
make validate
```

This lints the chart, renders it into the `daedalus` namespace, and runs
Kubernetes client-side validation. Route policy is intentionally not validated,
so `routes.toml` can be changed freely while experimenting.

## Build and publish the image

No official v0.2.0 container is assumed. Build the pinned upstream commit and
push it to a registry every Kubernetes node can pull from:

```bash
make image LOCAL_IMAGE=daedalus.ddns.me:5050/btuttle/switchyard:v0.2.0
docker push daedalus.ddns.me:5050/btuttle/switchyard:v0.2.0
```

For an immutable deployment, record the pushed manifest digest and provide it
as `IMAGE_DIGEST=sha256:...` during deployment.

## Deploy

Copy `.env.example` to `.env` and set every credential named by an
`llm_clients.*.api_key_env` entry in `routes.toml`. `.env` is ignored by Git.
The deployment script discovers the unique names from `routes.toml`, creates
matching Kubernetes Secret keys, and injects those keys into the Switchyard
container. It does not place credential values in Helm values or the ConfigMap.

```bash
IMAGE_REPOSITORY=daedalus.ddns.me:5050/btuttle/switchyard \
IMAGE_TAG=v0.2.0 \
EXPECTED_CONTEXT=kubernetes-admin@kubernetes \
./scripts/deploy.sh
```

The script fixes the namespace to `daedalus`, release name to `switchyard`, and
Secret name to `switchyard-api-key` by default. Override `RELEASE_NAME` or
`SECRET_NAME` only when you also intend to change those resource names.

Each invocation adds a new non-secret restart annotation to the pod template.
This makes Secret and mutable image-tag updates take effect in newly started
containers. For each name discovered from `routes.toml`, a value supplied by
the caller environment takes precedence over the same name in `.env`. The
current Fireworks clients use `FIREWORKS_API_KEY`; unrelated variables such as
`OPENAI_API_KEY` are ignored.

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
  -d '{"model":"switchyard/stage","input":"Inspect and fix the failing tests."}'
```

Every routed response includes `x-model-router-selected-model` and
`x-model-router-rationale`. Because both completion signatures intentionally
share one model ID, use the rationale header or structured logs to distinguish
the classifier's `weak` and `strong` decisions; the selected-model header is
identical for both completion targets.
