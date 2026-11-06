# Muse Glimmer streaming tool failure

Investigated on 2026-11-01 in Kubernetes context `kubernetes-admin@kubernetes`, namespace `daedalus-dynamo`.

The deployment gate fails because the installed Dynamo frontend loses Muse's tool-call framing when streaming transitions from reasoning into a tool call. The model selects the tool, but the endpoint emits partial ATEM tool markup as assistant text instead of an OpenAI `tool_calls` object. This is a serving integration defect, not a context-window failure or an incorrect test expectation.

## Live evidence

All three configured profiles failed the original streaming capability check. The same checks with only the transport switched to non-streaming passed ordinary text generation, automatic tool selection, forced tool selection, and consumption of a fresh random tool-result marker.

| Target | Reasoning effort | Streaming | Non-streaming round trips |
| --- | --- | --- | --- |
| `hindsight_local` | `xhigh` | `tool_not_selected` | Passed |
| `local` | `high` | `tool_not_selected` | Passed |
| `none` | `medium` | `tool_not_selected` | Passed |

Results: [roundtrip-results.json](roundtrip-results.json).

Separate requests through both the authenticated internal gateway and the model's frontend Service reproduced the same distinction. Automatic and forced streaming tool requests returned no structured calls and finished with `stop`. For example, the gateway returned this ordinary content:

```text
to=probe_read<atem:function_calls>
<atem:invoke name="probe_read">
<atem:parameter name
```

The non-streaming response contained `probe_read` with arguments `{"key":"probe"}` and finish reason `tool_calls`. These comparisons isolate the failure to the frontend, before the gateway. See [endpoint-probes.json](endpoint-probes.json).

The frontend, worker, and ModelExpress pods were ready with zero restarts. The observed runtime was Dynamo **1.6.1**, vLLM **0.31.0**, image `nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.6.1`, digest `sha256:5899e7bdd3528ab09a17ec87e2abe1949f8a0386a0a49f3b24dbd52d8211dc5d`. Model revision: `47818374517751c48c55cde2621594926b1888b6`. The worker uses DFlash with 15 speculative tokens.

## Exact failure mechanism

The gate in [check_capabilities.py](../../../scripts/check_capabilities.py) sets `stream=True` at line 333, tests automatic tool choice before forced choice at line 349, and requires exactly one structured call at line 367. Each reported failure happens on the automatic call, after the plain-text check has passed.

The frontend already has `--dyn-chat-processor vllm`, `--enable-auto-tool-choice`, `--tool-call-parser muse_glimmer`, and `--reasoning-parser muse_glimmer`. These are also present in the durable Muse tuning file in `daedalus-dynamo/values/meta/muse-glimmer-30b-nvfp4/_tuning.yaml`. The NVIDIA [model card](https://huggingface.co/nvidia/Muse-Glimmer-30B-NVFP4#usage) recommends the Muse parsers; adding the same flags again will not fix this deployment.

In the installed runtime:

1. `vllm/reasoning/muse_glimmer_reasoning_parser.py` hands the tool channel, including its ATEM header, to the downstream tool parser when reasoning ends.
2. `dynamo/frontend/prepost.py:1218`, `_tool_start_markers()`, discovers markers from parser attributes such as `tool_call_start_token` or parser-engine terminals. Muse exposes none of those, so this returns an empty tuple.
3. `StreamingPostProcessor.process_output()` clears accumulated text at the transition, then checks whether the handed-off text contains a recognized tool marker. With no recognized marker, it emits the text as ordinary content instead of buffering it for tool parsing.
4. The later tool parser receives only the remaining suffix. With larger chunks, the discarded handoff includes the opening invocation, leaving no complete call to extract. Smaller chunks can produce a call but still leak the header as content.

Non-streaming requests use `_process_non_streaming_tool_output()` and parse the complete accumulated text, avoiding this broken handoff. [Source hashes](source-hashes.json) identify the inspected files.

## Candidate correction and validation

[candidate-parser-handoff.patch](candidate-parser-handoff.patch) adds the Muse opening marker as `tool_call_start_token`. This activates Dynamo's existing transition buffer and retains the tool block until generation finishes. It is an **unapplied candidate**, validated in an independent CPU-only Python process using the installed parser classes and cached tokenizer. No serving process or container file was changed.

The [replay script](replay_parser.py) supplies identical synthetic ATEM output in one-token chunks, 15-token chunks, and a single whole-response chunk. The original streaming path fails all three clean-output assertions; the candidate passes all three. Buffered parsing passes all three both before and after the candidate. See [parser-replay-results.json](parser-replay-results.json).

An end-marker attribute must not be added naively: an additional replay showed that early flushing at `</atem:function_calls>` can cause the current Dynamo adapter to replay stale text and emit a duplicate tool call with one-token chunks. The supplied candidate deliberately waits for `finish_reason`.

To rerun the isolated replay from this directory:

```bash
kubectl -n daedalus-dynamo exec -i \
  deployment/mx-muse-glimmer-30b-nvfp4-l40s-tp1-84d1be-frontend \
  -- python3 - < replay_parser.py
```

The next remediation is a versioned frontend runtime containing the correction, with a durable model-specific image override and rollback to the recorded image digest. Before promotion, validate live streamed text and automatic/forced tool round trips for all three profiles, multiple tool calls, and representative application requests. The candidate has not yet undergone that live validation. Keep the streaming deployment gate intact: non-streaming success does not establish compatibility with Switchyard's streaming upstream transport.

## Separate configuration observation

The live worker has `--language-model-only`, while the three local routes in `routes_custom.toml` declare `vision=true`. Image behavior was not tested; the route declarations should not be treated as evidence of image support. This is separate from the text-only tool failure above.

## Change scope

Only this local investigation directory was added. Existing edits in both repositories were preserved. No deployment, gateway route, authentication, Service, NodePort, or network policy was changed. The current gateway exposes ports `32359` and `32360`, with an internal ClusterIP listener on `8000`; these remain intact.
