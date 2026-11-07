# Custom serverless routing optimization

Reviewed November 6, 2026 (America/New_York); deployment timestamps are November 7 UTC.
The objective is balanced latency, token use and capability using only Fireworks
serverless answering models. Existing public aliases, LAN access, internal
consumers, context declarations, image support and the decision service are retained.

| Profile | Serverless model | Provider reasoning effort | Intended work |
| --- | --- | --- | --- |
| instant | DeepSeek V4.1 Flash 1017 Ultrafast | none | Trivial conversational responses |
| low | DeepSeek V4.1 Flash 1017 Ultrafast | low | Mechanical work and predictable tools |
| medium | DeepSeek V4.1 Flash 1017 Ultrafast | high | Ordinary bounded agent work |
| high | Kimi K3.1 Pro | low | Hard debugging and adaptive investigation |
| xhigh | Kimi K3.1 Pro | high | Deep synthesis and independent cross-checks |
| max | Kimi K3.1 Pro | max | Exceptional reasoning or concrete high-impact work |

Profile names represent task requirements, not equivalent effort scales across
model families. Moving from medium to high changes model family. The internal
judge remains `baselabs/elephant-1`, discovered through the existing decision API;
it classifies requests and does not answer them. Automatic fallback remains xhigh.

## Findings and changes

- The source already contained uncommitted Muse target changes. The exact starting
  file is preserved in [before.toml](before.toml); it was not reset to Git HEAD.
- Stale Kimi/US-router/Rune descriptions and the incorrect instant fallback were
  removed from classifier instructions. Task criteria now avoid provider identities
  that can drift. Instant is restricted to trivial work; requesting speed does not
  permit downgrading difficult work.
- DeepSeek V4.1 maps medium/high/xhigh to the same provider effort. The selected
  none/low/high defaults avoid three nominally different tiers doing identical work.
  See [Fireworks effort semantics](https://docs.fireworks.ai/api-reference/post-chatcompletions).
- Muse Open documents low/medium/high/xhigh, not a distinct max tier. More
  decisively, live streaming probes produced text after the tool-call finish event
  for both medium and xhigh. Later xhigh retries passed, so this is intermittent,
  not a claim that every request fails. Muse is excluded rather than weakening
  the deployment gate. See [Meta's settings](https://dev.meta.ai/docs/muse-spark-open/settings),
  [initial failures](capabilities.json) and [follow-up probes](extra-capabilities.json).
- The selected models are in the [live serverless catalog](serverless-catalog.json).
  Kimi's [Fireworks model page](https://fireworks.ai/models/fireworks/kimi-k3p1-pro)
  confirms serverless availability. All selected endpoints advertise tools, image
  input and 1,048,576 context tokens. This is not a full-window quality benchmark.
- `reasoning_history = "interleaved"` preserves within-turn reasoning for tools
  and clears it at the next user boundary. The previous disabled setting stripped
  it from every request. See [Fireworks history semantics](https://docs.fireworks.ai/api-reference/post-chatcompletions).
- Target `stream = true` was removed: live requests with explicit `stream: false`
  otherwise returned SSE, breaking JSON clients and all seven smoke checks.
  The gateway now derives streaming from the caller. Provider capability probes
  still explicitly exercise streaming.
- Low/medium/high probability gates changed from 0.80/0.70/0.60 to
  0.60/0.50/0.50. Instant/xhigh/max remain 0.90/0.55/0.50. These are policy
  thresholds, not calibrated probabilities of answer correctness.

## Measurements and limits

[compare.py](compare.py) tested 12 candidate model/effort combinations with two
synthetic tasks. [candidate-results.json](candidate-results.json) includes failures,
token usage and wall times. The 4,096-token cap exhausted reasoning budgets on
most constraint-solving attempts; this is not evidence that those models cannot
solve the task with adequate budgets. GLM Flash also timed out on extraction.
Fence stripping in the scorer measures answer correctness, not strict JSON compliance.

DeepSeek low extracted the supplied fields correctly in 0.364 seconds versus
2.277 seconds for Muse low in this run. Kimi max solved the six-job constrained
schedule with the independently enumerated optimum of 129 in 3,056 output tokens.
With a 16,384-token ceiling, DeepSeek high, Kimi low and Kimi high all solved it;
their actual completion-token counts were 8,583, 828 and 2,277 respectively.
See [candidate-followup.json](candidate-followup.json). Output budgets must leave
room for both reasoning and the final answer; no restrictive token cap was added
to production targets.

The same 20 classifier cases accepted 12 choices before and 18 after tuning.
Median discovery/tokenization/classification time fell from 208.4 ms to 145.65 ms;
the greeting input fell from 1,721 to 982 tokens. The unmatched short summary
still conservatively falls back to xhigh, as does the deliberately ambiguous task.
See [before](judge-before.json) and [after](judge-after.json).

An independent 12-case policy check matched the intended target range in 10 cases.
The two misses were unnecessary xhigh fallbacks for mechanical follow-through
and a short rewrite; all difficult/high-impact cases stayed in their intended
tiers. These misses are retained in [policy-validation.json](policy-validation.json),
not relabeled as successes. [validate_policy.py](validate_policy.py) exits nonzero
while those efficiency misses remain. The conservative fallback is deliberately
retained rather than lowering the low gate below 0.5 to fit two examples.

This is an evidence-guided operating configuration, not proof of a universally
optimal model ranking or production-wide cost savings. Samples are small,
provider latency and caching vary, and long-context workflows and real task
completion rates were not benchmarked. Probability tuning should next use actual
workload outcomes rather than additional fitting to these synthetic cases.

## Validation and rollback

The deployment gate tests all six exact provider configurations with streaming
text, automatic and forced tool calls, and consumption of fresh tool-result
markers. Local tests cover configuration, header rendering, capability checks,
deployment recovery and the route smoke harness (59 tests total).

The deployment keeps the existing image digest:
`sha256:0509fb5988d55490ae6db48928b67d018534fecd52c4c09e2d4d6fb94d73f073`.
Service and policy specs were compared with the live state before deployment.
NodePort 30260, LAN range 192.168.1.0/24 and internal port 4000 remain unchanged.
The pre-change release snapshot, including Secret and a restore script, is:

```bash
bash .deploy-backups/20261107T001238Z-switchyard-dw4yfk5m/restore.sh
```

To restore the local source separately, copy `before.toml` over
`routes_custom.toml`. The older checked-in `.bk` was not overwritten. Live client
checks use [verify_client.py](verify_client.py) from the non-node LAN machine
`daedalus-07` and the Hindsight API pod, covering all three API formats and a
nested tool call followed by a DeepSeek-to-Kimi continuation.

Final release: **Helm revision 199, deployed**. The running pod's mounted TOML
matches the rendered source exactly. All six final provider configurations passed
the gate, all seven public routes passed non-streaming Responses generation, and
all six client checks passed from each network source. Streaming Responses text
and automatic/forced tool round trips also passed for instant, xhigh and automatic
routing. The Service and NetworkPolicy specs equal the pre-change snapshots.

Evidence: [deployment summary](deployment-summary.json),
[final provider gate](deployment-final-capabilities.json),
[seven-route smoke](route-smoke.txt), [LAN client](lan-client.json),
[internal client](internal-client.json), and [gateway streaming](gateway-streaming.json).
