# Standalone decision routing

Provider files discovered through `routes_*.toml` use the standalone native
`decision_api` route for `daedalus`, with `classify_trigger = "every_request"`.
The target categories are max, high, low and instant; `default_target`, explicit
aliases and native provider settings are defined independently in each file.
Select a provider explicitly for validation, probing and deployment. The `stage`
table name remains for tooling; no `[routes.stage.stage]` is needed.

The decision model judges the next response from the task and latest tool
context. Each automatic request, including a tool continuation in the same
session, can choose a different profile. There is no retained effort floor or
stage recovery hold. Context guards, invalid/uncertain decisions, rejected
probability gates and service failures choose the default for this request only.
The next request runs classification again. Provider failure tries only the
default after the selected target. Explicit profile aliases bypass classification.

The existing per-choice probability thresholds, provider mappings, credentials,
service addresses, public aliases and ports are preserved. Caller-supplied
reasoning effort still overrides a selected target's default; QA should omit
that override when measuring automatic effort changes.

Routing does not provide a local-processing or privacy boundary.
The judge receives the opening task, latest user instruction and six recent
messages. The input-budget update discovers the hosted context limit and
truncates judge-only state to fit, using the hosted tokenizer. Unsupported media
or unavailable tokenization still falls back. These fallbacks should be counted
separately from accepted judge choices.

## Implementation and local checks

`patches/decision-api-standalone.patch` extends the pinned runner. The Dockerfile
applies it after the existing native classifier, metadata and discovery patches,
runs the native Rust contract tests and builds the server. Legacy Composite
configurations remain supported; they still reject `every_request`.

The built gateway can be tested entirely against local HTTP fixtures:

```bash
SWITCHYARD_TEST_BINARY=/path/to/switchyard-server \
  python3 -m unittest discover -s tests -p test_decision_api.py -v
SWITCHYARD_TEST_BINARY=/path/to/switchyard-server \
  python3 -m unittest discover -s tests -p test_fireworks_chat_transport.py -v
```

The standalone cases cover fresh decisions across tool calls (`xhigh → low →
high → low`), both Chat Completions and Responses, unknown sessions, streaming,
probability gates, timeout/invalid-decision recovery, provider fallback and
explicit profile bypass. These are deterministic contract tests, not model
quality calibration.

## QA after maintenance

1. Validate the selected provider file against the intended built image. Promote
   compatible image/configuration together through the deployment capability gate.
2. Run `make test routes_fireworks.toml` (or the OpenAI profile) for route smoke
   checks. This does not run the separate 20-question judge probe.
3. Run the 20-question probe against a reachable decision service using the
   command below. This checks the question and score gates only.
4. Replay representative multi-step tasks against the automatic alias and fixed
   `daedalus/high`. Include healthy continuations, failures requiring recovery,
   difficult synthesis after successful tools, and local-sensitive work. Record
   the winning label, probability, accepted flag, selected target, fallback
   reasons, task outcomes, tool correctness, latency and token use per call.
5. Keep a held-out QA set. Accept a policy based on quality and measured cost or
   latency, rather than a desired fraction of lower-effort choices. Each request
   now incurs discovery/classification latency within the existing 2-second budget.

```bash
python3 scripts/test_decision_judge.py routes_fireworks.toml \
  --base-url http://127.0.0.1:30090/v1 --output /tmp/decision-routing-probe.json
```

## Rollback

Before rollout, retain the deployed image reference and Helm revision. A rollback
must restore the matching image and route configuration. For local comparison,
the pre-change files are backed up under
`.deploy-backups/switchyard-standalone-c4u4tb9l/`. That backup includes existing
uncommitted operator edits; it is not necessarily the currently deployed profile.
No live Kubernetes resources or LAN endpoints were changed during implementation.

## Validation record

Local validation on 2026-11-04:

- Built `switchyard-server` from the pinned source with all nine Dockerfile
  patches. A fresh patch-application check produced the same router sources.
- Passed 396 Rust tests across the router library and runner, plus runner
  Clippy with `--no-deps -D warnings`.
- Passed 20 focused Python tests for native decisions, profile configuration,
  capability discovery and the judge probe, plus two streaming/legacy-stage
  compatibility checks. This count is separate from the 20-question live probe.
- Both full profile files passed server `--dry-run`, Helm lint and offline
  template rendering with dummy credentials. Provider mappings, fixed routes
  and probability gates were compared with the pre-change backup and preserved.
- The broader Python suite is not fully green: 15 failed subtests and two errors
  in older local-model/default-effort expectations also reproduce using the
  pre-change route files and test fixtures. They expect Bolt, nonthinking local
  defaults, or `/deep → xhigh`, while the custom profile already specifies
  Muse-Glimmer, different effort defaults and `/deep → max`. These unrelated
  provider settings were preserved.

The tested development binary and detailed logs are under
`/tmp/switchyard-standalone-c4u4tb9l/` and `/tmp/switchyard-standalone-*.log`.
No live decision/provider quality benchmark or deployment was performed.
