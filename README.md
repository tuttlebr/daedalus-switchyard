# Switchyard on Daedalus

This repository builds NVIDIA-NeMo Switchyard v0.3.0 with local target-model
alias, Fireworks reasoning, Composite fallback, native decision API and
structural credential redaction patches.

Credential redaction walks decoded JSON strings before serializing responses.
Replacing credentials in serialized JSON could match across an escape boundary
and produce an invalid stream event, aborting long Daedalus briefings. The
`structural-credential-redaction.patch` also walks JSON encoded inside tool
arguments and preserves their spelling when no credential changed. The build
runs redactor regressions; the HTTP fixture covers large reasoning events,
valid nested arguments, continued credential removal and unchanged upstream auth.

It deploys a non-root Helm workload in the `daedalus` namespace. The Fireworks configuration
exposes model `daedalus` on the OpenAI Chat Completions, OpenAI Responses, and
Anthropic Messages endpoints.

`routes_fireworks.toml` implements Composite routing for automatic effort and
exposes explicit effort routes for GLM 5.3 Fast. All Fireworks targets use the same
provider model, `accounts/fireworks/routers/glm-5p3-fast`. Fireworks describes
[Fast](https://docs.fireworks.ai/serverless/serverless-modes) as a faster serving
mode of the same model. Switchyard chooses an effort target; Fireworks chooses
the inference replica. The `/local` route is a separate, explicitly requested
Dynamo model.

## Daedalus request path

```mermaid
flowchart LR
  A[Daedalus Rust agent] -->|Responses API| B[Switchyard]
  B -->|daedalus: judge + stage| C[xhigh or medium effort]
  B -->|deep / xhigh / max / other explicit route| D[fixed effort target]
  C --> E[Fireworks GLM 5.3 Fast: Chat Completions]
  D --> E
  B -->|daedalus/local| F[Local Dynamo: Responses API]
```

Switchyard translates Responses input, tool calls/results, reasoning history,
and streamed output to and from Chat Completions. The downstream agent keeps
using `/v1/responses`; it does not need a Fireworks-specific provider adapter.
During the October 4, 2026 incident, direct GLM Responses streams stalled while
Chat Completions completed, so this upstream transport is intentional.

| Requested model | Switchyard target | Requested Fireworks effort | Use |
| --- | --- | --- | --- |
| `daedalus` | Native Composite choices: `low`, `medium`, `high`, `xhigh` | Selected target's effort | Automatic/default profile |
| `daedalus/deep`, `daedalus/xhigh` | `xhigh` | `xhigh` | Deep profile; daily summaries and mapped skills |
| `daedalus/max` | `max` | `max` | Explicit `deep_max` profile |
| `daedalus/high` | `high` | `high` | Fixed high effort |
| `daedalus/medium` | `medium` | `medium` | Fixed requested medium effort |
| `daedalus/low` | `low` | `low` | Explicit latency-oriented choice |
| `daedalus/none` | `low` | `low` | Compatibility alias; thinking remains enabled |
| `daedalus/local` | `local` | N/A | `nvidia/Muse-Glimmer-30B-NVFP4` on local Dynamo |

The effort column describes the value sent on the wire. Live GLM probes accepted
`low`, `medium`, `high`, `xhigh`, and `max` on October 6, 2026; that does not prove
five distinct internal compute tiers. The provider explicitly rejected `none`
because GLM 5.3 is thinking-only. `/none` therefore aliases `/low` and preserves
reasoning between tool calls within the current user turn. It does not send
`reasoning_effort="none"` or discard in-turn reasoning.

Daedalus selects profiles before Switchyard selects targets. Explicit request
profiles take precedence. With automatic selection, daily-summary requests
start deep; loading `daily-summary`, `fantasy-football-recap`, `sre-engineer`, or
`kubernetes-specialist` promotes subsequent rounds to deep. `deep_max` is
explicit only. The deployed deep override is `daedalus/xhigh`; the workflow's
fallback `daedalus/deep` resolves to the same target. These choices do not change
tools, approval authority, timeouts, or iteration budgets.

### What the stage router actually does

The v0.3.0 stage router reads tool results and agent progress. Its built-in
vocabulary is calibrated for coding tools; it does not classify task complexity
without a judge. Native Composite supplies the judge's selected task target when signals are inconclusive.
Critical errors and repeated failures escalate. Error severity, spinning,
exploration, and recent production contribute to a signed, `tanh`-scaled score;
`confidence_threshold = 0.5` is a signal threshold, not a calibrated probability
of task success. The recent window is three tool results. The new default
`capable_hold_turns = 2` holds the capable tier after escalation; a clean test
pass clears the hold early. A stable `x-switchyard-session-id` is needed to
retain state across requests.

Most Daedalus research/MCP tools do not match the coding vocabulary. Before the
October 11 upgrade, all 431 automatic-route requests in the current pod's
18:43–20:26 UTC logs selected `xhigh`; that deployment used DeepSeek V4.1 Flash.
These counts show target selection, not answer correctness or GLM latency.
The upgraded deployment uses the local GLM 5.3 Fast configuration with
`xhigh`/`medium`. Explicit effort routes bypass stage decisions. There is no
configured provider failover; Daedalus owns its bounded model retries.

An isolated October 6 comparison kept the deployed binary/model and changed
only the picker to `efficient_first` and threshold to `0.3`. Twenty synthetic
tasks with two paired repetitions covered no-tool answers, single/independent/
dependent reads, tool-error recovery, and long context. The all-attempt
gateway-loop p50/p95 were 5.72s/25.71s for the current policy and 7.98s/36.36s
for low-first. Exact answer/format assertions passed 28/40 and 29/40,
respectively, but low-first failed six cases the baseline passed. Both arms
had format failures, and the small sample does not establish general model
quality. Low-first failed the frozen correctness and latency gates and was
not promoted. This historical comparison does not evaluate the new Composite
`xhigh`/`medium` configuration.

### Conversation affinity and defaults

The Rust agent sends the standard OpenAI-compatible `user` field as a SHA-256
key scoped to the authenticated user and conversation. All model/tool rounds
and later turns in that conversation reuse it; requests without a conversation
use a run-scoped key. Raw user/conversation IDs are not sent in this field.
Switchyard preserves it through Responses-to-Chat translation.

Fireworks supports [`user` for prompt-cache affinity](https://docs.fireworks.ai/guides/prompt-caching).
Do not add one fixed `x-session-affinity`, `x-multi-turn-session-id`, or Dynamo
session ID to `llm_clients.extra_headers`. A fixed key groups unrelated traffic
onto one affinity destination. With server-owned provider credentials, v0.3.0
forwards only the allowlisted
`x-request-id` caller header. Inbound provider-affinity headers are not forwarded.
Daedalus continues to use the translated `user` field for cache affinity;
`x-switchyard-session-id` is separate gateway routing state. Affinity improves
the opportunity for prefix reuse;
cache hits still depend on exact prompt prefixes, replica placement, and load.

Target defaults are illustrated below:

```toml
[targets.high]
id = "high"
model = "accounts/fireworks/routers/glm-5p3-fast"
llm_client = "fireworks"
extra_body = { reasoning_effort = "high", reasoning_history = "interleaved" }

[targets.low]
id = "low"
model = "accounts/fireworks/routers/glm-5p3-fast"
llm_client = "fireworks"
extra_body = { reasoning_effort = "low", reasoning_history = "interleaved" }
```

`id` identifies the routing target. The optional `model` names the model sent to
the provider; omitting it preserves the original behavior where `id` serves
both purposes. Distinct routing IDs keep each target's defaults independent,
even when they share a client and provider model. Separate clients alone do
not solve this for the stock v0.3.0 stage router, which also selects targets by
ID. The Dockerfile applies `patches/target-model-aliases.patch` to the pinned
`336196f6fbfc97ddc71c1700f6092e564e9f23c2` source. The v0.3.0 patch covers the
shared runner, provider transport, auxiliary operations, and decision metadata.
`patches/fireworks-chat-reasoning.patch` also converts assistant-history plaintext
`reasoning` to Fireworks-native `reasoning_content` for that provider. This keeps
thinking attached to tool calls; generic `reasoning` is rejected by Fireworks.
Other providers keep their existing representation. Conflicting or opaque
reasoning fails explicitly rather than being silently dropped.
**Rebuild the image before deploying this Fireworks configuration**; the unpatched
image rejects the `model` field.

Switchyard treats `extra_body` as a default-only, shallow top-level merge. An
explicit reasoning effort in the inbound request wins after translation to
`reasoning_effort`. The Fireworks and OpenAI routes set `reasoning = true`:
v0.3.0 actively rejects reasoning controls when this flag is false. `/v1/models` omits this flag, so this
change does not inject caller effort through model discovery. Target defaults
continue to allow an explicit caller effort to override the selected tier.

The Fireworks targets use native Chat Completions fields. Do not add the
Responses-only `truncation` field to their `extra_body`: Fireworks rejects it
on Chat Completions. Switchyard translates the downstream Responses request
before applying these target defaults.

## Automatic reasoning routing

`routes_fireworks.toml` uses Composite routing for `daedalus` with a native
Cloudflare Clef-Flash decision classifier. It targets the service configured in
`../decision-api`:

```text
http://decision-api.daedalus.svc.cluster.local:30000/v1/systemone
model: Cloudflare/clef-flash
```

The classifier sends SystemOne `state` and one typed `choice` question named
`effort`, with configurable named target choices and an `uncertain` option.
The initial choices are `low`, `medium`, `high`, and `xhigh`; high uses the GLM
Fast router and the others use GLM Flash, preserving the configured provider IDs.
It uses the model's native joint head without a
Chat endpoint, generated verdict, free-text `crux`, or artificial `p_solve`.
A winning named choice must meet its own `min_probability` threshold, initially
**0.90**, to set the task target. Uncertainty, insufficient probability and judge
failures use `stage.capable_target`, currently xhigh. These criteria and thresholds
are initial policies, not calibrated task-success rates.
The stage router still applies tool evidence, critical-error escalation and
recovery holds. Its existing confidence threshold remains 0.5.

Each choice maps a label to a target and describes when that target is sufficient:

```toml
[routes.stage.classifier.choices.routine]
target = "medium"
min_probability = 0.90
criteria = "Routine bounded synthesis or a short dependent tool sequence."
```

Replace or extend the choice tables to use more configurations or different models.
Labels need not match target names. `uncertain` is reserved and uses
`uncertain_criteria`; every target must exist in `[targets]`. Choose a capable
target suitable for recovery across the entire choice pool. The initial automatic
pool stops at xhigh; max and local remain explicit profiles. To add automatic max,
add its choice and use max as the capable recovery target as well.

The selected task target is retained through tool continuations. Healthy production
signals return to that target, including xhigh, rather than dropping to the fixed
`efficient_target`. Recovery signals escalate to the configured capable target;
the two-turn hold then returns to the retained task target. Provider failures try
the capable target only, without cycling through cheaper choices. In this mode,
`stage.efficient_target` supplies the stage library's tier pair; it does not limit
the judge to that target. The old `efficient_criteria`, `capable_criteria` and
`efficient_threshold` schema remains supported for two-target routing, but cannot
be mixed with named choices.

Traces are not required to define these criteria. They are useful for collecting
representative task inputs, tool sequences, recovery cases and outcomes. To tune
the policy, replay safe cases against fixed target configurations and score task
completion, tool names/arguments/order, output constraints, latency and token use.
A trace from one target cannot show whether a cheaper target would also succeed.
Freeze a held-out workload before tuning, and compare the routed candidate with
the fixed capable baseline. Changing the choice pool changes the probability
distribution, so recheck thresholds whenever choices or models change.

The classifier runs on a request ending in a new user message, including steering
or a runtime synthesis instruction. Daedalus sends `x-switchyard-session-id` as
an opaque hash scoped to authenticated user and conversation (run ID when there
is no conversation). Tool continuations reuse the verdict. The existing `user`
field still supplies provider cache affinity; the gateway session header is not
forwarded to Fireworks. Repeated user requests and retries may judge again.
Session state is in memory and is lost on restart/eviction; unclassified tool
continuations default to xhigh. Do not enable message-hash fallback, which can
merge unrelated users with identical opening text.

The native judge has a **2-second total deadline**, including body reads, with
no retry or redirect. A refused connection, unavailable service, timeout, invalid
answer/distribution, wrong model or exhausted context defaults to the capable
tier. It verifies the exact question/options, a choice matching the highest
probability, finite confidence metadata and normalized probabilities (allowing Clef's four-decimal rounding), and
native usage with zero generated tokens. SystemOne confidence metadata can use
a different normalization from option probabilities; policy reads the option
probabilities directly. Failures are logged with bounded reason
codes, never response bodies; timing is logged as `decision_api_classified`.

The request preserves client instructions, the opening task, six recent messages
and available tool names. Private reasoning is omitted. Unsupported media/opaque
blocks or a request exceeding 12,000 serialized bytes default to xhigh without
silently truncating the judge context. Answer requests retain the complete input.
A reported input-token count at the configured 16,384 context ceiling also defaults
capable because the service may have truncated state. Adjust that ceiling if you
change the target service's context limit.

Explicit profiles such as `daedalus/deep`, `/max`, `/medium`, `/low`, and `/local`
bypass the judge. To enable service-owned bearer authentication, configure
`api_key_env = "DECISION_API_KEY"` in the classifier and supply the matching key;
`scripts/list_api_key_envs.py` includes it in deployment credential discovery.
Caller credentials, routing/user header metadata and arbitrary headers are not
forwarded to the decision service. The default service is configured without a bearer key.

The Dockerfile applies `patches/decision-api-classifier.patch` after the existing
patches. Rebuild before deploying this TOML. Existing LLM Composite classifiers
remain supported with `type = "llm"` or no classifier `type`. They retain their
original verdict schema and target settings. Native decision classifiers have
no judge entry in `llm_clients` or `targets`.

Exact native and Hue MCP tool names supply stage semantics:

- `observe`: memory/time, search, scraping, retrieval, sources, skills and Hue reads.
- `mutate`: memory writes and setting Hue light state.
- `new`: briefing rendering, content distillation and Hue catalogue connection.

Mixed-operation tools such as sandbox, documents, visual media and user interaction
remain unclassified. No dedicated Daedalus tool is exclusively a planning tool;
built-in plan semantics remain available. These labels affect stage scoring, not
authorization. In particular, `mutate` favors the retained task target.

The answering targets use the configured GLM Fast router or Flash model with
different effort settings. The choice mapping can also select other configured
models. Caller-supplied effort continues to
override answering defaults. Native classifier probabilities express choice
confidence and require broader workload calibration.
A judge adds latency to each new user turn; semantic and compatibility checks do
not establish a speedup or prove distinct internal provider compute tiers.

Native integration validation covers the built parser, typed HTTP requests and
responses, probability boundaries and rounding, session reuse/isolation, profile
bypass, error/body deadlines, context guards and server-owned credentials. These
contract tests do not establish Clef's routing accuracy. The companion Daedalus
runtime session-header change in `../daedalus-agent` must be present to reuse
classifications in the live tool loop. The current deployed Daedalus image passed
its HTTP contract, including session-header propagation, on October 11.

The named-choice implementation was deployed on October 11 with
`make deploy-route_fireworks` at Switchyard Helm revision 99. Validation passed
483 Rust tests, Clippy, 48 gateway checks against the release binary, and all
nine live route probes. The deployed binary matches the tested binary; mounted
TOML matches the local configuration except its trailing newline. Credentials
were preserved. Live decision checks covered session reuse, explicit-profile
bypass, critical tool results and unknown-session fallback.

A paired gateway/provider evaluation passed 20/20 tasks and 8/8 synthetic tool
sequences in each arm, using the routed candidate and a fixed xhigh baseline.
The 0.90 policy kept all candidate cases on xhigh, so this verifies the current
request path without establishing cheaper-target accuracy or savings. The first
attempt used an invalid Fireworks combination of forced tools and JSON formatting;
its results are retained alongside the corrected fixture evaluation. Evidence is
in `/tmp/switchyard-multitarget-20261011/implementation-report.json`.

The earlier two-target candidate consumed five real responses from your running Clef
service through `/v1/decision`: simple and lookup cases were labeled efficient,
proof and migration cases capable, and ambiguous context uncertain. The 0.90
policy kept all five defaults at xhigh because efficient probabilities were below
the threshold. These are judge-only contract checks, not calibrated accuracy or
an answering-model benchmark. SGLang's normalized `confidence` differed from its
option probability; both are accepted, and policy uses the option probability.

Probe the exact configured routing question using
only synthetic inputs. Override the base URL when using a port-forward:

```bash
kubectl -n daedalus port-forward service/decision-api 30090:30000
python3 scripts/test_decision_judge.py routes_fireworks.toml \
  --base-url http://127.0.0.1:30090/v1 --output /tmp/clef-routing-probe.json
```

The probe checks the native model/question/usage/distribution contract and
reports choice scores and policy defaults. It does not execute the synthetic
requested actions or certify answer quality. Follow with the frozen task/tool
workload before promoting the Composite policy to production.

The previous October 11 GLM-judge Composite implementation was built and tested
but **not deployed**. Its historical evaluation is described below; it does not
establish Clef routing accuracy or latency.
At that earlier evaluation, the live releases were Switchyard revision 96 and
Daedalus revision 52.
Validation passed 478 upstream Rust tests, 28 gateway contract checks, Daedalus
Rust/Clippy/hooks and its built-image HTTP contract. The final real Fireworks
comparison covered 20 pairs and eight synthetic tool round trips per arm; both
arms passed all eight tool cases. Exact-answer constraints passed 17/20 for the
baseline and 16/20 for Composite, with three candidate regressions. Some medium
answers emitted stray model delimiters or ignored the requested output format.
The candidate recovered a real judge timeout by answering at xhigh.

This small gateway-only sample had all-attempt p50/p95 of 0.94s/1.39s for the
baseline and 1.80s/2.31s for Composite. It excludes real external tools, agent
preparation and UI delivery, and does not establish general model quality.
That GLM-judge candidate did not meet its frozen correctness gate and was not
promoted. Historical local evidence is in
`/tmp/switchyard-composite-20261011/implementation-report.json`.

Upstream references: [v0.3.0 Composite routing](https://github.com/NVIDIA-NeMo/Switchyard/blob/v0.3.0/docs/routing_algorithms/composite_routing.md),
[Stage Router](https://github.com/NVIDIA-NeMo/Switchyard/blob/v0.3.0/docs/routing_algorithms/stage_router_routing.md),
[LLM classifier](https://github.com/NVIDIA-NeMo/Switchyard/blob/v0.3.0/docs/routing_algorithms/llm_classifier_routing.md),
and [Fireworks structured outputs](https://docs.fireworks.ai/structured-responses/structured-response-formatting).

## Files

- `routes_fireworks.toml`, `routes_openai.toml`, and `routes_local.toml` are the
  selectable deployment sources for clients, targets, and routes.
- `charts/switchyard/` contains the Helm chart.
- `Dockerfile` builds the pinned upstream v0.3.0 commit with the target-model alias
  Fireworks reasoning, Composite fallback and native decision API patches.
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

The transport tests use a built binary and a local mock to check both stage
defaults, critical-error escalation, every Fireworks route, buffered/streaming
Responses, tool history, caller effort overrides, open argument schemas,
conversation affinity, and provider-header filtering. Configuration invariants and the route smoke harness
also run. The mocks do not contact Fireworks:

```bash
SWITCHYARD_TEST_BINARY=/path/to/switchyard-server \
  python3 -m unittest discover -s tests -v
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
that includes `stage`, `max`, `xhigh`, `deep`, `high`, `medium`, `low`, `none`, and `local`.
The TOML selects the
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

No official v0.3.0 container is assumed. Build the pinned upstream commit and
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
`daedalus.ddns.me:5050/btuttle/switchyard`, tag `v0.3.0`, and Kubernetes context
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

Routed responses include `x-model-router-selected-model` and, when available,
`x-model-router-rationale`. With the Fireworks aliases, the selected-model
header identifies the target, such as `high`, `low`, or `xhigh`; the request sent
to Fireworks uses the full provider model ID from `model`.
