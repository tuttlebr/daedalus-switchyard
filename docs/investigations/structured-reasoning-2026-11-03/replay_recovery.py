"""Replay the repair-routing failure against a local Switchyard and mock providers.

Only synthetic inputs are used. This does not change or call production services.
Set SWITCHYARD_TEST_BINARY to the Switchyard executable to inspect.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[3]
if not os.environ.get("SWITCHYARD_TEST_BINARY"):
    raise SystemExit("Set SWITCHYARD_TEST_BINARY to the Switchyard executable")
os.environ["INCEPTION_API_KEY"] = "synthetic-fixture"
sys.path.insert(0, str(ROOT / "tests"))
from test_fireworks_chat_transport import FireworksChatTransportTest  # noqa: E402


def mock_inception_target(source: str) -> str:
    """Keep instant distinct from Fireworks while using the same local mock."""
    source = re.sub(
        r'(?ms)(^\[targets\.instant\]\n.*?model = )"[^"]+"',
        r'\1"mercury-3"',
        source,
    )
    # The existing harness replaces this URL with its loopback mock URL.
    return source.replace(
        "https://api.inceptionlabs.ai/v1", "https://api.fireworks.ai/inference/v1"
    )


def main() -> None:
    probabilities = {
        name: 0.001
        for name in ("max", "xhigh", "high", "medium", "low", "instant", "local", "uncertain")
    }
    probabilities["instant"] = 0.993
    verdict = {
        "model": "Cloudflare/clef-flash",
        "answers": {"effort": {
            "type": "choice", "choice": "instant", "confidence": 0.993,
            "probabilities": probabilities,
        }},
        "usage": {"input_tokens": 123, "output_tokens": 0},
    }
    history = [
        {"role": "user", "content": "Produce a synthetic briefing."},
        {"type": "reasoning", "id": "rs_fixture", "summary": [],
         "encrypted_content": "synthetic-provider-state"},
        {"type": "function_call", "call_id": "call_read", "name": "read_fixture", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_read", "output": "x" * 70000},
        {"type": "function_call", "call_id": "call_render", "name": "briefing_renderer_tool",
         "arguments": json.dumps({"edition": '{"schema_version":"daily-daedalus/v1"}'})},
        {"type": "function_call_output", "call_id": "call_render",
         "output": json.dumps({"status": "error", "executed": False,
                               "error": "invalid_tool_batch", "detail": "edition must be an object"})},
    ]
    notice = (
        "Invalid calls did not run. Correct invalid arguments once using the advertised "
        "schemas and fresh call IDs."
    )
    fixture = FireworksChatTransportTest()
    results = []
    for case in ("original", "tool_result_guidance", "plaintext_control", "small_context_control"):
        with fixture.router(
            composite="native_targets", verdict=verdict, source_transform=mock_inception_target
        ) as (url, captured, _config):
            headers = {"x-switchyard-session-id": "synthetic-recovery"}
            first = fixture.request(url, headers=headers, input="Produce a synthetic briefing.")
            captured.get(timeout=2)
            captured.get(timeout=2)
            followup = copy.deepcopy(history)
            if case == "tool_result_guidance":
                result = json.loads(followup[-1]["output"])
                result["recovery_instruction"] = notice
                followup[-1]["output"] = json.dumps(result)
            else:
                followup.append({"role": "user", "content": notice})
            if case == "plaintext_control":
                del followup[1]["encrypted_content"]
                followup[1]["summary"] = [{"type": "summary_text", "text": "Synthetic thought."}]
            if case == "small_context_control":
                followup[3]["output"] = "small synthetic result"
            row = {"case": case, "initial_target": first["x-model-router-selected-model"]}
            try:
                response = fixture.request(url, headers=headers, input=followup)
                row.update(status=200, selected_target=response["x-model-router-selected-model"])
            except HTTPError as error:
                row.update(status=error.code, error=json.loads(error.read()))
            paths = []
            opaque_preserved = False
            guidance_preserved = False
            while not captured.empty():
                path, body, _headers = captured.get(timeout=2)
                paths.append(path)
                for message in body.get("messages", []):
                    for detail in message.get("reasoning_details", []):
                        opaque_preserved |= detail.get("encrypted_content") == "synthetic-provider-state"
                    guidance_preserved |= notice in str(message.get("content", ""))
            row.update(outbound_paths=paths, opaque_state_preserved=opaque_preserved,
                       repair_guidance_preserved=guidance_preserved)
            results.append(row)
    assert results[0]["status"] == 400 and results[0]["outbound_paths"] == []
    assert results[1]["status"] == 200 and results[1]["selected_target"] == "instant"
    assert results[1]["opaque_state_preserved"] and results[1]["repair_guidance_preserved"]
    assert results[1]["outbound_paths"] == ["/v1/chat/completions"]
    assert results[2]["status"] == 200 and results[2]["selected_target"] == "xhigh"
    assert results[3]["status"] == 200 and results[3]["selected_target"] == "instant"
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
