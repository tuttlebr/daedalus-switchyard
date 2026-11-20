# Standalone decision routing

Provider files discovered through `routes_*.toml` use the standalone native
`decision_api` route for `daedalus`, with `classify_trigger = "user_turn"`.
The target categories are max, high, low and instant; `default_target`, explicit
aliases and native provider settings are defined independently in each file.
Select a provider explicitly for validation, probing and deployment. The `stage`
table name remains for tooling; no `[routes.stage.stage]` is needed.

The decision model judges the whole task once. A Daedalus run keeps its target
through tool calls, steering, retries and final synthesis. The next run gets a
fresh choice, including in the same conversation. An uncertain decision, a
rejected score or an unavailable judge selects the configured default for the
run. Explicit profile aliases bypass classification.

The agent sends an opaque `x-switchyard-task-id` for the run. It reads
`x-model-router-selected-model` from the gateway response headers, so even
a stream interrupted before completion can carry its serving target into a
retry. If that header is unavailable, a completed response's model metadata
also supplies the target. The agent sends the target as
`x-switchyard-previous-target` on the next automatic call. The gateway checks it against the
current route's configured choices and default. This lets another replica
on the same profile continue the run without a judge call. Changing a target's
provider or model in a later configuration changes where that target goes.
The agent's per-attempt correlation ID
remains distinct. Provider session affinity is configured on the provider client.

Direct API clients can use a fresh task ID for each independent task and reuse
it for retries and tool calls. A session ID plus a stable turn ID or replayed
history also identifies a user turn. A client can carry the serving target from
the preceding response to continue across replicas. The per-route assignment
table is bounded and local to each gateway process. A request without an
acknowledged target or a recognizable task is classified normally. Restart or
eviction can require a new decision when a client relies solely on that table.

If a target fails before output, the existing provider fallback can try
`default_target` once it exhausts that target's configured retries. A successful
fallback becomes the serving target for subsequent calls. Errors after output
begins follow the existing stream and partial-result handling.

Change category criteria and probability gates in
`config/routing/four-category.toml`, decision endpoint/budget settings in
`config/decision/elephant.toml`, and target models/defaults in the selected
`config/llms/` fragment. The root profile can override any of these when composed.
Caller-supplied reasoning fields override a selected target's defaults through
a shallow merge; omit those overrides when measuring the configured mapping.

The decision server is a separately provisioned dependency. The gateway discovers
its model, prepares the classifier request and uses its scores; it does not
install or train that model. A healthy gateway can still select the default
for new tasks if discovery, tokenization or classification fails.

Routing does not provide a local-processing or privacy boundary.
The judge receives the opening task, latest user instruction and six recent
messages. The input-budget update discovers the hosted context limit and
truncates judge-only state to fit, using the hosted tokenizer. Unsupported media
or unavailable tokenization still falls back. These fallbacks should be counted
separately from accepted judge choices.

## Implementation and local checks

`patches/decision-api-standalone.patch` extends the pinned runner. The Dockerfile
applies it after the existing native classifier, metadata and discovery patches,
runs the native Rust contract tests and builds the server. The task-retention
patch adds the `user_turn` lifecycle. An explicitly configured standalone
`every_request` route preserves per-request decisions. Native Switchyard
documents the `every_request`, `user_turn` and `new_session` triggers for
its generic classifier. [Upstream trigger semantics](https://github.com/NVIDIA-NeMo/Switchyard/blob/336196f6fbfc97ddc71c1700f6092e564e9f23c2/docs/routing_algorithms/llm_classifier_routing.md#when-the-judge-runs)

The built gateway can be tested entirely against local HTTP fixtures:

```bash
SWITCHYARD_TEST_BINARY=/path/to/switchyard-server \
  python3 -m unittest discover -s tests -p test_decision_api.py -v
SWITCHYARD_TEST_BINARY=/path/to/switchyard-server \
  python3 -m unittest discover -s tests -p test_fireworks_chat_transport.py -v
```

The local cases exercise task retention and reset, legacy per-request routing,
Chat Completions and Responses, streaming, probability gates, classifier errors,
provider fallback and explicit profile bypass.

## Check a routing change

1. Validate the selected provider file against the intended built image. Promote
   compatible image/configuration together through the deployment capability gate.
2. Run `make test ROUTES_FILE=<selected-provider-file>` for live route smoke
   checks against the intended gateway. This sends model traffic and does not
   run the separate 20-question judge probe.
3. Run the 20-question probe against a reachable decision service using the
   command below. This checks the question and score gates only.
4. Replay representative multi-step tasks against the automatic alias and fixed
   `daedalus/high`. Include healthy continuations, failures requiring recovery,
   difficult synthesis after successful tools, and local-sensitive work. Record
   the winning label, probability, accepted flag, selected target, fallback
   reasons, task outcomes, tool correctness, latency and token use per call.
5. Keep a held-out QA set. Accept a policy based on quality and measured cost or
   latency, rather than a desired fraction of lower-effort choices. Discovery,
   tokenization, input fitting and classification share the selected classifier's
   `timeout_ms` budget (currently 2000 ms in the shipped files). Measure its
   overhead with the answering path, including failures and fallback.

```bash
python3 scripts/test_decision_judge.py routes_fireworks.toml \
  --base-url http://127.0.0.1:30090/v1 --output /tmp/decision-routing-probe.json
```

## Rollback

Before rollout, retain the deployed image reference and Helm revision. A rollback
must restore the matching image and route configuration. Use the private snapshot
and `restore.sh` generated by the deployment script, as described in the root
README. Preserve Secret contents, LAN exposure and fixed ports. An old source
file alone does not establish the configuration of the running release.

Keep per-run validation results in private local evidence. The built gateway
suite checks protocol behavior; real provider capability, source/deployment
parity and matched task-quality evaluation remain separate checks. Historical
Composite fixtures are compatibility coverage, not the current routing policy.
