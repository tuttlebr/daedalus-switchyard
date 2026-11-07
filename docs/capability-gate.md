# Provider capability gate

`scripts/deploy.sh` snapshots the selected routes file and checks every distinct
provider/model/settings combination before changing Kubernetes Secrets or the
Helm release. A failed generation or tool check exits unsuccessfully and preserves
the active route. Context-window checks are advisory and do not block deployment.
The same snapshot is used for validation and deployment.

The gate also reads each target's `/models` catalog and reports its advertised
`context_window` in tokens. It matches the exact model ID and accepts the
`context_window`, `context_length`, `max_model_len`, and `max_context_length`
fields. If multiple limits are present, it uses the smallest, so a serving cap
takes precedence over a larger model limit. Discovery is cached per client/model
for the duration of the check, across reasoning profiles.

An optional `context_window` on a `[routes.<name>]` table is an expected minimum
for every completion target reachable through that route, including classifier
choices, fallback targets and subagents. The gate warns about a smaller advertised
limit with `context_window_too_small` and continues generation and tool checks.
For example:

```toml
[routes.local]
id = "daedalus/local"
type = "passthrough"
target = "local"
context_window = 32768
```

A missing model, missing limit, or unsupported `/models` endpoint (HTTP 404,
405 or 501) is reported as `context_window: null` with status `unknown` and a
reason. An undeclared window needs no warning, but a declared window that cannot
be verified warns with `context_window_unknown`. Malformed metadata and other
metadata lookup failures also produce sanitized warnings. These appear in the
target's `warning` field in console output and the JSON report; they do not set
`passed` to false or cause an unsuccessful exit. Invalid route declarations still
fail configuration validation. This reads provider metadata; it does not send a
full-window prompt, prove long-context quality, or change route declarations.

Each target must stream a completed text response, select a synthetic tool both
automatically and explicitly, and consume a fresh unpredictable value returned
by that tool. The gate checks arguments and call IDs. The tool is implemented
inside the checker and has no external effects.
The Chat stream parser accepts nullable `delta` and `tool_calls`, usage-only
chunks, and an empty chunk repeating the same finish reason, as emitted by
Mercury 3. A trailing tool-call fragment is also empty when it refers to an
existing call index and supplies no ID, name or argument text. Such fragments
are ignored without creating another call. A successful `stop` with tool-call
data is treated as `tool_calls`; IDs, arguments and result consumption still
have to pass. The parser requires `[DONE]` and rejects unsuccessful or
conflicting finish reasons, additional content/tool-call data after completion,
new call indexes after completion, and malformed tool-call collections. It does
not discard repeated text, IDs, names or arguments, or retry failed requests to
turn a failure into a pass.
`CAPABILITY_TIMEOUT` and `CAPABILITY_MAX_OUTPUT_TOKENS` set deployment probe
budgets; defaults are 60 seconds per request and 4096 output tokens.

Run the same bounded check independently after exporting the credentials named
in the TOML. It is suitable for a scheduled availability check; it is not a
Kubernetes readiness probe.

```bash
python3 scripts/check_capabilities.py routes_custom.toml \
  --timeout 60 --max-output-tokens 4096 --report /tmp/model-capabilities.json
python3 -m unittest discover -s tests -p test_capabilities.py
```

Cluster-local HTTP services are reached through a temporary localhost port
forward in the current Kubernetes context. Other provider endpoints are reached
directly. The report includes the routes digest, target settings digest, model
identity, advertised and required context windows, elapsed time and sanitized failure codes.
Failures additionally include `error_message` and, when available, `error_details`:

- `stage`: `text`, `automatic_tool_selection`, `automatic_tool_result`,
  `forced_tool_selection`, `forced_tool_result`, or `setup`.
- `request_id`: a bounded provider request ID from a response header, including
  HTTP error responses. Credentials are excluded.
- Stream diagnostics: the one-based JSON data-event index, previous/current
  finish reasons, content character count, tool-fragment count, and whether
  `[DONE]` was observed. Unknown finish reasons are reported as `other`.
- For `duplicate_terminal`, `violation` distinguishes `content_after_finish`,
  `tool_calls_after_finish`, and `conflicting_finish_reason`. The existing error
  code remains stable for consumers; the message explains the specific cause.

These fields do not include prompts, generated text, tool arguments or raw
provider errors. They describe the current failed request, without retaining
response bodies. A successful synthetic probe cannot establish what happened
in an earlier failed request whose stream was not recorded.

Report version 2 adds context-window results; `checks` includes `context_window`
only when a limit was found and meets any declared minimum. It omits keys and response
content. No automatic provider fallback or mutation replay is introduced.

This proves upstream behavior for the tested requests. Validate gateway route
translation and the actual backend image separately before accepting a release;
the existing route smoke remains useful for that gateway boundary. A later
provider outage can still occur after a successful check.

Each result retains `target` as its representative name and includes
`covered_targets` and `covered_aliases` listing all equivalent configured targets
and their explicit passthrough aliases. Identical client, model, request defaults
and required context window share one probe; different settings remain separate.
This checks provider capabilities, not the correctness of each routing decision.
