# Renderer argument failure and incompatible reasoning replay

The incident was a real function call with an invalid nested argument, followed by a failed recovery. The model supplied `briefing_renderer_tool.edition` as a string. Daedalus correctly rejected it because the renderer requires an object. Its correction prompt then caused Switchyard to change providers while retaining Inception-specific opaque reasoning state. Switchyard rejected that state before contacting Fireworks.

Investigation date: 2026-11-03. Times below are UTC; 16:01 UTC is 11:01 EST.

## Evidence and sequence

The [sanitized incident log summary](incident-summary.json) records the agent and Switchyard events. Run: `3ce624f2-ff56-4989-ba31-257262cebdc2`. Phoenix trace: `b118ae6b737a07b4aced1092a9276aab`. The session matches the reported `a83e6794…e077cb`.

| Time | Observed behavior |
| --- | --- |
| 16:00:39 | Native classifier discovers `baselabs/elephant-1` and selects `instant`. Live configuration maps this to Inception `mercury-3`, with medium reasoning effort. |
| 16:00:40–16:01:11 | Agent completes 22 tool executions. Six model calls use `instant`; normal tool continuations retain the selection. |
| 16:01:31.827579 | Model call 6 completes with one tool call, no answer text, 199,758 reported input tokens and 8,079 output tokens. |
| 16:01:31.827653 | Daedalus rejects `briefing_renderer_tool`: `/properties/edition/type` expects `object`, received `string`. `recovery_exhausted=false`. The renderer does not execute. |
| 16:01:31.831716 | Agent starts model call 7 to repair the arguments. |
| 16:01:31.868098 | Switchyard's classifier payload exceeds its byte budget. It logs `input_too_large` and falls back to `xhigh` in 0 ms. |
| 16:01:31.873759 | Fireworks request normalization rejects structured reasoning details. HTTP 400 is returned before an upstream Fireworks request is sent. |
| 16:01:31.877171 | Agent marks the run failed. The error is nonretryable; the repair never reaches a model. |

The failure occurred once in the retained logs from midnight through the initial investigation, before our synthetic probes. The classifier had worked for this session immediately before the failure. This was a local input-budget rejection, not evidence of a decision-service outage.

## What the string error means

The tool call itself was recognized as a structured function call. The invalid part was its **nested `edition` value**. The renderer already advertises an object and explicitly says it must not be a JSON string in [briefing_renderer.py](/volume2/daedalus/datasets/daedalus-agent/builder/nat_helpers/src/nat_helpers/briefing_renderer.py:53).

After decoding the function's `arguments` JSON, the distinction is:

```json
{"edition": {"schema_version": "daily-daedalus/v1"}}
```

versus a string-valued field such as:

```json
{"edition": "{\"schema_version\":\"daily-daedalus/v1\"}"}
```

These are illustrative fragments, not valid complete editions or the captured rejected arguments. The outer function `arguments` field being a JSON-encoded string is normal for this API. A second encoding inside `edition` is not.

## Why a recoverable argument error became fatal

Daedalus appends the rejected call's tool result, then appends correction guidance with `Message::user(notice)` in [agent.rs](/volume2/daedalus/datasets/daedalus-agent/builder/runtime/src/agent.rs:1008). The current Switchyard `user_turn` trigger treats a trailing user message containing ordinary text as a new user turn. This synthetic instruction therefore reopens classification instead of retaining the ongoing task's target.

The classifier takes the opening user message, the latest six internal messages, client instructions and tool names. It preserves tool-output text and tool-call arguments within that slice; a message-count window is not a byte bound. The live cap is **65,536 serialized bytes**, with a separate **16,384 input-token** validation cap. Large retained results in this run included 69,942 bytes from the skill loader and roster results of 66,600 and 62,465 bytes. The zero-millisecond failure is consistent with the local byte check in [decision-api-classifier.patch](/volume2/daedalus/datasets/switchyard/patches/decision-api-classifier.patch:929), before model discovery or classification HTTP traffic.

The fallback `xhigh` resolves to Fireworks `accounts/fireworks/models/muse-spark-open`. Inception returns opaque reasoning state that Switchyard exposes as a Responses `reasoning` item with `encrypted_content`. Replaying it into Chat Completions creates `reasoning_details`. The Fireworks adapter deliberately rejects nonempty structured details in [fireworks-chat-reasoning.patch](/volume2/daedalus/datasets/switchyard/patches/fireworks-chat-reasoning.patch:40). Its guard predates this incident and preserves the distinction between transferable plaintext and opaque provider state.

The live `xhigh` target already has `reasoning_history="disabled"`. This does not bypass the adapter's local validation. Fireworks documents a plaintext `reasoning_content` field; it is not a conversion mechanism for another provider's opaque state. See the [Fireworks Chat API](https://docs.fireworks.ai/api-reference/post-chatcompletions).

## Reproduction and proof of concept

The locally tested executable has the **same SHA-256 as the running executable**:
`a7b4909c2cf9c622fc62b081306e4983aa7fbb6d226230aa00a6cb33fe74b3d0`.
The deployed image digest is `sha256:0088a733e2e37579ddd9e853ee463f5ca6c96549cc15f3c50d0477ba9354d9de`.

[replay_recovery.py](replay_recovery.py) runs that binary against loopback mock classifier and model endpoints, with synthetic reasoning state, a large tool result and an invalid renderer call. It does not execute tools or send private data to a model. [Recorded results](replay-results.json):

| Case | Result | What it establishes |
| --- | --- | --- |
| Large context + opaque state + correction as user message | 400; no outbound classifier/model call | Reproduces the reported failure. |
| Same input, correction embedded in the tool-error result | 200; retained `instant`; opaque state and correction text preserved | The repair can remain a continuation without losing state or instructions. |
| Large context + plaintext reasoning + user correction | 200; fallback `xhigh` | The byte fallback alone does not cause the 400. |
| Small context + opaque state + user correction | 200; classifier chooses `instant` | The fatal combination is provider switching plus incompatible retained state. |

Run again with:

```bash
SWITCHYARD_TEST_BINARY=/path/to/switchyard-server \
  python3 docs/investigations/structured-reasoning-2026-11-03/replay_recovery.py
```

A separate **live synthetic tool round trip** checked actual provider behavior. `daedalus/instant` returned one function call and an opaque reasoning item with an 88-character encrypted payload. Continuing the exact output plus a synthetic tool result on `daedalus/instant` completed and returned the fixture value `42`. Sending that same history to `daedalus/xhigh` produced the exact reported 400. Only synthetic fixture content was used. See [live probe results](live-probe-results.json).

## Recommended changes

1. **Fix agent recovery semantics first.** Put schema-correction guidance in the corresponding tool-error result rather than appending a fabricated user turn. Preserve call/result ordering, successful calls in mixed batches, the existing repair budget, and disabled-tool behavior. The proof of concept verifies the wire-level effect; it is not an integrated runtime patch. Audit the other runtime-generated user messages for the same distinction between actual user requests and internal continuation instructions.
2. **Make provider transitions compatible with retained state.** Track the source of opaque reasoning and restrict candidate/fallback targets accordingly. Preserve a compatible existing target during repair. A provider switch needs an explicit, tested context-transfer policy; forwarding or silently dropping encrypted state is not a general solution. Cover both classifier fallback and stage escalation, not just this renderer path.
3. **Bound classification context deliberately.** Build a classifier-specific representation containing the current real user task, material routing/privacy constraints, progress and concise tool-result summaries within byte and token budgets. Keep full evidence available to the answering model. The opening historical greeting plus a recent-message slice is not a durable representation of the current task. Do not truncate away the instructions needed to choose a privacy-appropriate route. Record payload size and fallback reason without logging bodies.
4. **Keep renderer validation strict.** The schema already states the right contract. Verify a built-agent regression in which the model returns a string-valued edition, receives one correction, returns a valid object, and the renderer executes exactly once. Enforce provider-supported schema constraints where available; do not globally parse arbitrary strings into tool argument objects or weaken the renderer schema.

Raising the classifier limit alone or deleting the Fireworks reasoning guard would not resolve the underlying provider-state mismatch. Repeatedly retrying this HTTP 400 unchanged cannot repair it.

Before deployment, add integrated tests for mixed valid/invalid tool batches, exhausted recovery, a genuine later user turn, other maintenance messages, opaque-state compatibility and large classifier context. Validate through the built agent and gateway, including a non-node LAN client and application/API behavior. Preserve HTTP NodePort `30260`, the ClusterIP listener on `4000`, existing routes and credentials; back up live and durable configuration with rollback if deployment changes are made.

## Limits and changes made

Historical provider request/response bodies were not available in the reviewed logs or Phoenix trace. Redis retained the submitted conversation and successful tool steps, but not the rejected renderer arguments or complete intermediate model reasoning. The original `edition` string's contents and exact rejected classifier payload size therefore remain unverified. The causal sequence is supported by the correlated logs and code, and independently reproduced with synthetic inputs and actual Inception output.

The mock replay proves routing and data preservation, not that a particular model will repair a real edition correctly. The live probe proves same-provider continuation and cross-provider rejection, not full briefing generation.

Only this investigation directory was added. Existing working-tree edits were preserved. No agent code, routes, deployments, credentials, service exposure, or network policies were changed. Temporary port-forwards and local mock servers were stopped.
