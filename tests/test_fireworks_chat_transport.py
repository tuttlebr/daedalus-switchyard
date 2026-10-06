"""Exercise Responses translation against a built Switchyard binary and mock Chat API.

SWITCHYARD_TEST_BINARY=/path/to/switchyard-server python3 -m unittest discover -s tests -p test_fireworks_chat_transport.py -v
"""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from urllib.request import Request, urlopen

import tomllib

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get("SWITCHYARD_TEST_BINARY")


@unittest.skipUnless(BINARY, "set SWITCHYARD_TEST_BINARY to the built binary")
class FireworksChatTransportTest(unittest.TestCase):
    @contextmanager
    def router(self, picker=None):
        captured = Queue()

        class ChatUpstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captured.put((self.path, body, dict(self.headers)))
                chunk = {
                    "id": "chatcmpl-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": body["model"],
                }
                events = [
                    {
                        **chunk,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "OK"},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        **chunk,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                ]
                if body.get("stream"):
                    data = (
                        "".join(f"data: {json.dumps(event)}\n\n" for event in events)
                        + "data: [DONE]\n\n"
                    ).encode()
                    content_type = "text/event-stream"
                else:
                    data = json.dumps(
                        {
                            **chunk,
                            "object": "chat.completion",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": "OK"},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        }
                    ).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ChatUpstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        process = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                source = (ROOT / "routes_fireworks.toml").read_text()
                config = tomllib.loads(source)
                if picker is not None:
                    source = source.replace(
                        f'picker = "{config["routes"]["stage"]["picker"]}"',
                        f'picker = "{picker}"',
                    )
                provider_url = config["llm_clients"]["fireworks"]["base_url"]
                source = source.replace(
                    provider_url, f"http://127.0.0.1:{upstream.server_port}/v1"
                )
                path = Path(directory) / "routes.toml"
                path.write_text(source)
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                env = {
                    **os.environ,
                    "FIREWORKS_API_KEY": "test",
                    "LOCAL_API_KEY": "test",
                }
                process = subprocess.Popen(
                    [
                        BINARY,
                        "--config",
                        str(path),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                url = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 10
                while True:
                    try:
                        urlopen(url + "/health", timeout=0.2).close()
                        break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Switchyard did not become ready")
                        time.sleep(0.02)
                yield url, captured, config
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)

    def request(self, url, headers=None, **payload):
        payload = {
            "model": "daedalus",
            "input": "Say OK.",
            "stream": True,
            "truncation": "auto",
            "store": False,
            "max_output_tokens": 128,
            **payload,
        }
        request = Request(
            url + "/v1/responses",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urlopen(request, timeout=5) as response:
            if not payload["stream"]:
                result = json.load(response)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["output"][0]["content"][0]["text"], "OK")
                return response.headers
            events = [
                json.loads(line[6:])
                for line in response.read().decode().splitlines()
                if line.startswith("data: {")
            ]
        self.assertTrue(
            any(event.get("type") == "response.completed" for event in events)
        )
        self.assertTrue(any(event.get("delta") == "OK" for event in events))
        return response.headers

    def test_effort_routes_translate_to_native_chat_parameters(self):
        with self.router() as (url, captured, config):
            for route in config["routes"].values():
                target_name = route.get("target", route.get("capable_target"))
                target = config["targets"][target_name]
                if target["llm_client"] != "fireworks":
                    continue
                with self.subTest(route=route["id"]):
                    self.request(url, model=route["id"])
                    path, body, _headers = captured.get(timeout=5)
                    self.assertEqual(path, "/v1/chat/completions")
                    self.assertEqual(body["model"], target["model"])
                    self.assertEqual(
                        body["reasoning_effort"],
                        target["extra_body"]["reasoning_effort"],
                    )
                    self.assertEqual(body["max_completion_tokens"], 128)
                    self.assertNotIn("truncation", body)
                    self.assertFalse(body["store"])

    def test_streaming_preserves_tool_history_schema_and_caller_effort(self):
        history = [
            {"role": "user", "content": "Read the integer."},
            {
                "type": "function_call",
                "call_id": "call_test",
                "name": "read_integer",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "call_test", "output": "41"},
        ]
        parameters = {
            "type": "object",
            "properties": {
                "metadata": {"type": "object", "additionalProperties": True}
            },
        }
        with self.router() as (url, captured, _config):
            self.request(
                url,
                input=history,
                instructions="Report the tool result.",
                reasoning={"effort": "low"},
                parallel_tool_calls=True,
                tools=[
                    {
                        "type": "function",
                        "name": "read_integer",
                        "parameters": parameters,
                    }
                ],
                tool_choice="none",
            )
            path, body, _headers = captured.get(timeout=5)
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(body["reasoning_effort"], "low")
            self.assertEqual(
                body["messages"][0],
                {"role": "system", "content": "Report the tool result."},
            )
            self.assertEqual(
                body["messages"][-1],
                {"role": "tool", "tool_call_id": "call_test", "content": "41"},
            )
            self.assertEqual(body["messages"][-2]["tool_calls"][0]["id"], "call_test")
            self.assertEqual(body["tools"][0]["function"]["parameters"], parameters)
            self.assertEqual(body["tool_choice"], "none")
            self.assertTrue(body["parallel_tool_calls"])

    def test_conversation_affinity_survives_translation_without_static_headers(self):
        with self.router() as (url, captured, _config):
            for route in ("daedalus", "daedalus/deep"):
                for stream in (False, True):
                    for key in ("a" * 64, "b" * 64, "a" * 64):
                        self.request(url, model=route, user=key, stream=stream)
                        _, body, headers = captured.get(timeout=5)
                        self.assertEqual(body["user"], key)
                        self.assertFalse(
                            any("session" in name.lower() for name in headers)
                        )

    def test_explicit_affinity_headers_are_forwarded_without_a_shared_override(self):
        with self.router() as (url, captured, _config):
            for session in ("synthetic-session-a", "synthetic-session-b"):
                self.request(
                    url,
                    headers={
                        "x-session-affinity": session,
                        "x-multi-turn-session-id": session,
                    },
                )
                _, _, headers = captured.get(timeout=5)
                lowered = {k.lower(): v for k, v in headers.items()}
                self.assertEqual(lowered["x-session-affinity"], session)
                self.assertEqual(lowered["x-multi-turn-session-id"], session)

    def test_stage_fallback_and_critical_error_override(self):
        history = [
            {"role": "user", "content": "Read the fixture."},
            {
                "type": "function_call",
                "call_id": "call_failure",
                "name": "mcp_read",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_failure",
                "output": "Connection refused",
            },
        ]
        for picker, target in (("capable_first", "high"), ("efficient_first", "low")):
            with self.subTest(picker=picker), self.router(picker=picker) as (
                url,
                captured,
                _config,
            ):
                headers = self.request(url)
                self.assertEqual(headers["x-model-router-selected-model"], target)
                captured.get(timeout=5)
                headers = self.request(url, input=history)
                self.assertEqual(headers["x-model-router-selected-model"], "high")
                captured.get(timeout=5)

    def test_none_alias_keeps_low_effort_and_in_turn_history(self):
        with self.router() as (url, captured, _config):
            headers = self.request(url, model="daedalus/none")
            _, body, _ = captured.get(timeout=5)
            self.assertEqual(headers["x-model-router-selected-model"], "low")
            self.assertEqual(body["reasoning_effort"], "low")
            self.assertEqual(body["reasoning_history"], "interleaved")

    def test_deep_alias_matches_the_deployed_agent_profile(self):
        with self.router() as (url, captured, _config):
            headers = self.request(url, model="daedalus/deep")
            _, body, _ = captured.get(timeout=5)
            self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
            self.assertEqual(body["reasoning_effort"], "xhigh")
