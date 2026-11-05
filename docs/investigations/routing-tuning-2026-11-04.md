Switchyard tuning and routing review — November 4, 2026

The configuration follows several useful engineering practices, but the available evidence does not establish a tuned routing policy. The highest priorities are measuring task outcomes, distinguishing the reasons for selecting xhigh, and accounting for the custom task-target retention behavior. Changing probability thresholds is a reasonable experiment; it is not sufficient to validate the algorithm or its savings.

This review covers local configuration, patches, tests, the upstream documentation, and source at the build's pinned commit, `336196f6fbfc97ddc71c1700f6092e564e9f23c2`. The cluster remains excluded during maintenance. Local configuration is not evidence of what is currently deployed. Source hashes and the inspected settings are recorded in [routing-tuning-2026-11-04.json](routing-tuning-2026-11-04.json).

The upstream process is to define an objective, measure fixed efficient, fixed capable, and routed baselines, calibrate candidates, and test the frozen selection on independent tasks. Keep provider settings and evaluation conditions comparable, and include retries and caching in cost. Its suggested 20% calibration / 80% validation allocation is a starting point, with related examples kept together. [Tuning Foundations](https://nvidia-nemo.github.io/Switchyard/tuning/foundations/)

| Area | Assessment of this repository |
| --- | --- |
| Session identity | Aligned: the Daedalus runtime supplies `x-switchyard-session-id`; classification uses `user_turn`. |
| Classifier isolation | Aligned: separate decision service, two-second deadline, response validation, and explicit fallback. |
| Provider compatibility | Partly aligned: deployment checks generation and synthetic tool round trips; actual full-tool-list behavior and long-context task quality still need QA. |
| Domain tool semantics | Aligned in structure: explicit observe, mutate, and new mappings. Actual tool-name coverage needs workload evidence. |
| Tuning evidence | Incomplete: no current representative, held-out comparison was found in the reviewed files. |
| Selection observability | Incomplete in the checked-in deployment: durable routing logging is not enabled, and the classifier's ordinary log event omits the winning label and rejection threshold. |
| Algorithm semantics | Deliberate local variation: named task targets remain selected through normal execution. This differs from the published two-tier Composite policy. |

1. **Your implementation does not offload an xhigh task's routine steps to medium.**

   The local [named-target patch](/volume2/daedalus/datasets/switchyard/patches/decision-api-classifier.patch:249) stores the selected task target. Both an inconclusive stage decision and an efficient stage decision use that retained target. A task initially selected as xhigh therefore remains xhigh through tool continuations; a subsequent user-turn classification can change it. A task initially selected as medium can temporarily use xhigh for recovery and then return to medium.

   Thus the practical policy is task-level selection plus recovery. This is reasonable if consistency across a task is the goal. It does not implement the stronger cost-saving expectation of using a capable model to resolve a hard step and an efficient model for the routine steps that follow. No adjustment to `capable_hold_turns` or `efficient_target` removes the retained task target.

   The published Composite documentation now describes a classifier choosing stage profiles over a shared pair, including separate thresholds and efficient holds. Those settings and failure semantics should not be copied into this patched build without a compatibility review. [Composite Routing](https://nvidia-nemo.github.io/Switchyard/routing_algorithms/composite_routing/)

2. **Today's stage threshold examples do not describe the pinned scorer.**

   The pinned scoring formula is `tanh(0.5 * (severity / 0.7 + spinning + exploring - production_intensity))`. Clean production alone reaches approximately `-0.4621`; at threshold `0.5`, it remains inconclusive. A hard error plus a stall reaches approximately `+0.7616`. These are calculations from source, not measured traffic. The pinned implementation also lets a clean passing test clear a capable hold. [Pinned scoring and hold implementation](https://github.com/NVIDIA-NeMo/Switchyard/blob/336196f6fbfc97ddc71c1700f6092e564e9f23c2/crates/libsy/src/algorithms/util/stage.rs)

   Current documentation describes stronger production signals and holds that passing tests do not end early. This version difference reinforces the need to measure this build rather than adopting current example thresholds mechanically. [Stage Router](https://nvidia-nemo.github.io/Switchyard/routing_algorithms/stage_router_routing/)

   Keep `confidence_threshold = 0.5`, `recent_turn_window = 3`, and `capable_hold_turns = 2` during the initial probability experiment. This preserves experimental isolation; it does not establish those settings as optimal. Test hold 1 separately only if actual recovery holds account for excess xhigh calls.

3. **The existing tests are useful prerequisites, not a tuning benchmark.**

   [Makefile](/volume2/daedalus/datasets/switchyard/Makefile:40) runs the route smoke script. The [20-case judge probe](/volume2/daedalus/datasets/switchyard/scripts/test_decision_judge.py:94) makes one classification per synthetic prompt, with empty client instructions and no tools. It reports probabilities and threshold acceptance, but does not execute answers, tools, recovery, or long conversations. Its current cases also have no scored expected routing labels.

   The [historical comparison](/volume2/daedalus/datasets/switchyard/README.md:426) reports that every candidate stayed on xhigh. Passing that comparison cannot establish quality on lower targets. Those older models and policies also differ from the current files.

   The deployment [capability gate](/volume2/daedalus/datasets/switchyard/docs/capability-gate.md) is a valuable compatibility check. Extend QA with actual agent tool lists, structured deliverables, and representative context sizes. Upstream specifically warns that a model can pass a one-tool probe and fail with the full agent tool list. [Model Selection](https://nvidia-nemo.github.io/Switchyard/routing_algorithms/model_selection/)

4. **Count why xhigh was selected before selecting another tuning control.**

   Separate accepted xhigh classifications, below-threshold choices, uncertain choices, classifier failures, retained targets, stage overrides, recovery holds, and explicit deep routes. The [classifier patch](/volume2/daedalus/datasets/switchyard/patches/decision-api-classifier.patch:1130) constructs detailed decision evidence, but its ordinary `decision_api_classified` log event records only selected target and elapsed time. Stage evidence can replace classifier evidence in the final outcome, so retain both decisions when collecting QA telemetry.

   The [deployment template](/volume2/daedalus/datasets/switchyard/charts/switchyard/templates/deployment.yaml:44) does not set `--routing-log-file`. The pinned server supports that flag, but its routing record contains route/model/tier/token data, not the complete classifier decision or elapsed time. Enable it in an isolated QA deployment and collect separate decision and latency records; adding that flag alone is insufficient. [Pinned routing log schema](https://github.com/NVIDIA-NeMo/Switchyard/blob/336196f6fbfc97ddc71c1700f6092e564e9f23c2/crates/switchyard-server/src/routing_log.rs)

   The [Daedalus workflow](/volume2/daedalus/datasets/daedalus-agent/backend/tool-calling-config.yaml:950) maps daily summaries and selected skills to deep. The default deep route resolves to xhigh. These requests bypass automatic classification and must be counted separately; changing probability thresholds cannot affect them.

5. **Calibrate the effective target configurations, not the profile names.**

   The custom file currently uses the same Fireworks model endpoint for all six cloud profiles, primarily varying reasoning effort; instant additionally changes reasoning history. The OpenAI file uses different model/effort combinations, and its xhigh profile requests high effort. These are different experiments. Record the actual upstream model and effective effort, including caller overrides, alongside the selected alias.

   As inspected, custom thresholds are instant 0.90, low 0.80, medium 0.70, high 0.60, xhigh 0.55, and max/local 0.00. OpenAI still uses 0.90 for low, medium, and high. The lower values remain hypotheses, not calibrated task-success probabilities. Do not transfer results between these configurations automatically.

   For reproducible QA, freeze the answering models/settings and decision model. Production `model = "auto"` is convenient discovery, but a changed served judge invalidates threshold comparisons. The custom classifier deliberately describes abstract task requirements independently of provider identity; whether a target actually satisfies those requirements must be established by outcome testing.

6. **Large-context and destination constraints need their own QA cases.**

   The classifier includes client instructions, the opening user message, six recent messages, and tool names, then rejects payloads above 65,536 bytes. This can select xhigh before any probability is available. Include long tool outputs and later user corrections in QA; the [November 3 investigation](/volume2/daedalus/datasets/switchyard/docs/investigations/structured-reasoning-2026-11-03/README.md) already reproduced this path and a provider-state compatibility failure.

   Automatic local selection is a preference, not a strict destination restriction: recovery can select cloud xhigh. Any comparison involving sensitive tasks must preserve their required destinations. A low probability threshold is not a substitute for enforcing that requirement.

The next comparison should contain fixed medium, fixed high, fixed xhigh, the original routed thresholds, and the proposed routed thresholds, using the same provider configuration and agent. Score task correctness, tool arguments and ordering, output constraints, recovery, full-task latency, and total token/cost usage. Include classifier overhead, retries, failures, and cache effects. The upstream worked examples use illustrative numbers, and explicitly call for end-to-end candidate runs because changed routing changes later execution. [Tuning Examples](https://nvidia-nemo.github.io/Switchyard/tuning/examples/)

Use the 20 synthetic prompts for an initial classification check. Select a separate representative task set and acceptance criteria before running the outcome comparison. Include brief answers, bounded research, code, HTML, public briefings, long investigations, follow-ups, recovery, and synthetic privacy cases. Keep automatic and explicit deep routing identifiable. Freeze the selected candidate before the independent validation run.

Composite remains a defensible architecture for this mixed workload: a task judge covers requests whose complexity is not visible from tool progress. Its optimality is unproven. After establishing the baselines, compare task selection plus recovery against a deliberate execution-offloading candidate on public workloads. That candidate requires an explicit behavior change or reviewed upstream migration, including context compatibility; it is not a stage-threshold adjustment. No routing configuration, application configuration, or cluster state was changed by this review.
