"""Real SSE parser and roundtrip gate fault coverage, without provider credentials."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "capabilities", Path(__file__).parents[1] / "scripts/check_capabilities.py"
)
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


class Capabilities(unittest.TestCase):
    def exercise(
        self, api, fault=None, *, nullable_tool_calls=False,
        repeat_terminal=False, tool_finish_reason="tool_calls",
    ):
        seen = []

        class Peer(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(body)
                if fault == "unavailable":
                    self.send_response(404)
                    self.end_headers()
                    return
                chat = api == "openai_chat"
                history = body["messages" if chat else "input"]
                final = body.get("tool_choice") == "none"
                calling = "tools" in body and not final
                text = (
                    history[-1].get("content", history[-1].get("output"))
                    if final
                    else "CANARY_OK"
                )
                if fault == "invented_result" and final:
                    text = "guessed"
                args = json.dumps({"key": "wrong" if fault == "arguments" else "probe"})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if chat:
                    delta = (
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_probe",
                                    "type": "function",
                                    "function": {
                                        "name": "probe_read",
                                        "arguments": args,
                                    },
                                }
                            ]
                        }
                        if calling
                        else {"content": text}
                    )
                    if nullable_tool_calls:
                        delta.setdefault("tool_calls", None)
                    if fault == "invalid_tool_calls":
                        delta["tool_calls"] = "not-a-list"
                    event = {
                        "model": "fixture",
                        "choices": [
                            {
                                "index": 0,
                                "delta": delta,
                                "finish_reason": tool_finish_reason if calling else "stop",
                            }
                        ],
                    }
                    payload = f"data: {json.dumps(event)}\n\n".encode()
                    if repeat_terminal:
                        terminal = {
                            "model": "fixture",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": (
                                        {"content": "unexpected"}
                                        if fault == "post_terminal_content"
                                        else {"content": None, "tool_calls": None}
                                        if nullable_tool_calls
                                        else {}
                                    ),
                                    "finish_reason": (
                                        "length"
                                        if fault == "conflicting_terminal"
                                        else "tool_calls" if calling else "stop"
                                    ),
                                }
                            ],
                        }
                        payload += f"data: {json.dumps(terminal)}\n\n".encode()
                    if fault != "missing_terminal":
                        payload += b"data: [DONE]\n\n"
                    self.wfile.write(payload)
                elif fault != "missing_terminal":
                    item = (
                        {
                            "type": "function_call",
                            "call_id": "call_probe",
                            "name": "probe_read",
                            "arguments": args,
                        }
                        if calling
                        else {
                            "type": "message",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    )
                    event = {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "model": "fixture",
                            "output": [item],
                        },
                    }
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = probe.check_target(
                {
                    "format": api,
                    "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                },
                {"model": "fixture"},
                timeout=2,
            )
            self.assertEqual(result["reported_models"], ["fixture"])
            self.assertEqual(len(seen), 5)
            self.assertNotIn(
                seen[2]["messages" if api == "openai_chat" else "input"][-1].get(
                    "content", "unused"
                ),
                probe.PROMPT,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_both_transports_consume_fresh_results(self):
        for api in ("openai_chat", "openai_responses"):
            with self.subTest(api=api):
                self.exercise(api)

    def test_nullable_tool_calls_and_repeated_empty_chat_terminal(self):
        # Mercury can send tool_calls: null and repeat the finish reason in
        # an empty chunk after the last content/tool-call chunk.
        for nullable, repeated in ((True, False), (False, True), (True, True)):
            with self.subTest(nullable=nullable, repeated=repeated):
                self.exercise(
                    "openai_chat",
                    nullable_tool_calls=nullable,
                    repeat_terminal=repeated,
                )

    def test_chat_stream_validation_remains_strict(self):
        for fault, code in (
            ("invalid_tool_calls", "invalid_tool_calls"),
            ("conflicting_terminal", "duplicate_terminal"),
            ("post_terminal_content", "duplicate_terminal"),
            ("missing_terminal", "missing_terminal"),
        ):
            with self.subTest(fault=fault), self.assertRaisesRegex(probe.CheckFailed, code):
                self.exercise(
                    "openai_chat", fault,
                    nullable_tool_calls=True,
                    repeat_terminal=True,
                )

    def test_stop_can_finish_a_complete_tool_call(self):
        for repeated in (False, True):
            with self.subTest(repeated=repeated):
                self.exercise(
                    "openai_chat",
                    nullable_tool_calls=True,
                    repeat_terminal=repeated,
                    tool_finish_reason="stop",
                )
        for fault in ("arguments", "invented_result"):
            with self.subTest(fault=fault), self.assertRaises(probe.CheckFailed):
                self.exercise(
                    "openai_chat", fault,
                    nullable_tool_calls=True,
                    repeat_terminal=True,
                    tool_finish_reason="stop",
                )

    def test_failures_reject_promotion(self):
        for api in ("openai_chat", "openai_responses"):
            for fault in (
                "unavailable",
                "arguments",
                "missing_terminal",
                "invented_result",
            ):
                with self.subTest(api=api, fault=fault), self.assertRaises(Exception):
                    self.exercise(api, fault)

    def test_failed_gate_leaves_release_and_secrets_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "bin").mkdir()
            for name in ("deploy.sh", "list_api_key_envs.py", "check_capabilities.py"):
                shutil.copy(
                    Path(__file__).parents[1] / "scripts" / name,
                    root / "scripts" / name,
                )
            routes = root / "routes.toml"
            routes.write_text(
                '[llm_clients.fixture]\nformat="openai_chat"\nbase_url="http://127.0.0.1:1/v1"\napi_key_env="FIXTURE_KEY"\n[targets.low]\nmodel="fixture"\nllm_client="fixture"\n'
            )
            calls = root / "calls"
            for command in ("helm", "kubectl"):
                script = root / "bin" / command
                script.write_text(
                    '#!/bin/sh\nprintf "%s\\n" "'
                    + command
                    + ' $*" >> "$CALL_LOG"\nif [ "$1 $2" = "config current-context" ]; then echo fixture; fi\n'
                )
                script.chmod(0o755)
            result = subprocess.run(
                ["bash", str(root / "scripts/deploy.sh")],
                env={
                    **os.environ,
                    "PATH": str(root / "bin") + os.pathsep + os.environ["PATH"],
                    "CALL_LOG": str(calls),
                    "ROUTES_FILE": str(routes),
                    "EXPECTED_CONTEXT": "fixture",
                    "IMAGE_REPOSITORY": "example.test/fixture",
                    "FIXTURE_KEY": "synthetic",
                },
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertNotEqual(result.returncode, 0)
            observed = calls.read_text().splitlines()
            self.assertTrue(any(line.startswith("helm template") for line in observed))
            self.assertEqual(
                [line for line in observed if line.startswith("kubectl")],
                ["kubectl config current-context"],
            )
            self.assertFalse(any(line.startswith("helm upgrade") for line in observed))
            self.assertNotIn("synthetic", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
