# Provider capability gate

`scripts/deploy.sh` snapshots the selected routes file and checks every distinct
provider/model/settings combination before changing Kubernetes Secrets or the
Helm release. A failed check exits unsuccessfully and preserves the active route.
The same snapshot is used for validation and deployment.

Each target must stream a completed text response, select a synthetic tool both
automatically and explicitly, and consume a fresh unpredictable value returned
by that tool. The gate checks arguments and call IDs. The tool is implemented
inside the checker and has no external effects.
The Chat stream parser accepts nullable `tool_calls` and an empty chunk repeating
the same finish reason, as emitted by Mercury 3. A successful `stop` with tool-call
data is treated as `tool_calls`; IDs, arguments and result consumption still have
to pass. The parser requires `[DONE]` and rejects unsuccessful or conflicting
finish reasons, additional content/tool calls after completion, and malformed
tool-call collections.
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
identity, elapsed time and sanitized failure codes. It omits keys and response
content. No automatic provider fallback or mutation replay is introduced.

This proves upstream behavior for the tested requests. Validate gateway route
translation and the actual backend image separately before accepting a release;
the existing route smoke remains useful for that gateway boundary. A later
provider outage can still occur after a successful check.
