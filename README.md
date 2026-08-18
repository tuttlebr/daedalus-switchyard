# Switchyard on Daedalus

This repository builds NVIDIA-NeMo Switchyard v0.2.0 and deploys it as a
non-root Helm workload in the `daedalus` namespace. The default route exposes
`switchyard/stage` on the OpenAI Chat Completions, OpenAI Responses, and
Anthropic Messages endpoints.

The default capability classifier exposes one route backed by a local Nemotron
3.5 Lightning judge and two Fireworks completion signatures. The local judge
decides whether the medium target can complete the request; Switchyard then
sends the original request to either the `medium` or `xhigh` completion target:

```toml
[targets.xhigh]
id = "accounts/fireworks/routers/glm-5p2-fast-us"
llm_client = "xhigh"
extra_body = { reasoning = { effort = "xhigh" } }

[targets.medium]
id = "accounts/fireworks/routers/glm-5p2-fast-us"
llm_client = "medium"
extra_body = { reasoning = { effort = "medium" } }

[targets.classifier]
id = "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4"
llm_client = "llm_classifier"
extra_body = { chat_template_kwargs = { enable_thinking = false } }

[routes.stage]
id = "switchyard/stage"
type = "llm_classifier"
mode = "capability"
classifier_target = "classifier"
strong_target = "xhigh"
weak_target = "medium"
base_threshold = 0.5
threshold_step = 0.1
```

The classifier client uses OpenAI Chat Completions at
`http://inference-gateway.daedalus-dynamo.svc.cluster.local:32359/v1`. The live
Dynamo Service exposes port `32359`, not port `80`. The completion clients stay
on OpenAI Responses at Fireworks.

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
invalid, or inconsistent classifier verdict falls back to the strong `xhigh`
target. Session affinity is intentionally disabled, so each turn can be judged
using the opening task and the configured three-turn recent window.

## Files

- `routes.toml` is the deployment source for clients, targets, and routes.
- `charts/switchyard/` contains the Helm chart.
- `Dockerfile` builds the upstream v0.2.0 tag commit as a standalone Rust server.
- `scripts/deploy.sh` validates, reconciles the API-key Secret, deploys Helm, and
  checks rollout, EndpointSlice, and HTTP health.

The chart mounts `routes.toml` from a ConfigMap and runs
`switchyard-server --dry-run` in an init container before the application starts.
It also exposes `/health` for Kubernetes probes and `/metrics` for an optional
Prometheus `ServiceMonitor`.

## Validate

```bash
make validate
```

This checks the classifier endpoint, protocol, auth, structured-verdict, and
completion-effort invariants. It also lints the chart, renders it into the
`daedalus` namespace, and runs Kubernetes client-side validation.

## Build and publish the image

No official v0.2.0 container is assumed. Build the pinned upstream commit and
push it to a registry every Kubernetes node can pull from:

```bash
make image LOCAL_IMAGE=daedalus.ddns.me:5050/btuttle/switchyard:v0.2.0
make validate-image LOCAL_IMAGE=daedalus.ddns.me:5050/btuttle/switchyard:v0.2.0
docker push daedalus.ddns.me:5050/btuttle/switchyard:v0.2.0
```

For an immutable deployment, record the pushed manifest digest and provide it
as `IMAGE_DIGEST=sha256:...` during deployment.

## Deploy

Copy `.env.example` to `.env` and set the Fireworks key. `.env` is ignored by
Git. The deployment script sends the key directly to a Kubernetes Secret; it
does not place the key in Helm values or the ConfigMap.

```bash
cp .env.example .env
# Edit .env and set OPENAI_API_KEY.

IMAGE_REPOSITORY=daedalus.ddns.me:5050/btuttle/switchyard \
IMAGE_TAG=v0.2.0 \
EXPECTED_CONTEXT=kubernetes-admin@kubernetes \
./scripts/deploy.sh
```

The script fixes the namespace to `daedalus`, release name to `switchyard`, and
Secret name to `switchyard-api-key` by default. Override `RELEASE_NAME` or
`SECRET_NAME` only when you also intend to change those resource names.

The default scheduling contract requires `amd64` and excludes the ARM64
`daedalus-06` worker. Publish a compatible multi-architecture image and override
`nodeSelector` and `affinity` together if that topology changes.

## Call the route

Inside the cluster, use this base URL:

```text
http://switchyard.daedalus.svc.cluster.local:4000/v1
```

For example:

```bash
curl http://switchyard.daedalus.svc.cluster.local:4000/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"switchyard/stage","input":"Inspect and fix the failing tests."}'
```

Every routed response includes `x-model-router-selected-model` and
`x-model-router-rationale`. Because all signatures intentionally share one model
ID, use the rationale header or structured logs to distinguish the classifier's
`medium` and `xhigh` completion decisions; the selected-model header will be
identical for both completion targets.
