"""Exercise configured native Responses aliases against a built gateway."""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import tomllib
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from scripts.render_routes import compose_source

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get("SWITCHYARD_TEST_BINARY")


@unittest.skipUnless(BINARY, "set SWITCHYARD_TEST_BINARY to the built binary")
class ResponsesTransportTest(unittest.TestCase):
    @contextmanager
    def gateway(self, path):
        captured = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captured.append((self.path, body))
                response = {
                    "id": "resp_fixture", "object": "response", "created_at": 1,
                    "status": "completed", "model": body["model"],
                    "output": [{"id": "msg_fixture", "type": "message",
                                "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": "OK",
                                             "annotations": []}]}],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
                if body.get("stream"):
                    events = [
                        {"type": "response.output_text.delta", "sequence_number": 0,
                         "item_id": "msg_fixture", "output_index": 0,
                         "content_index": 0, "delta": "OK"},
                        {"type": "response.completed", "sequence_number": 1,
                         "response": response},
                    ]
                    raw = "".join(
                        f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n'
                        for event in events
                    ).encode()
                    content_type = "text/event-stream"
                else:
                    raw = json.dumps(response).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        process = None
        try:
            source = compose_source(path)
            config = tomllib.loads(source)
            environment = os.environ.copy()
            for client in config["llm_clients"].values():
                source = source.replace(
                    client["base_url"], f"http://127.0.0.1:{upstream.server_port}/v1"
                )
                if client.get("api_key_env"):
                    environment[client["api_key_env"]] = "synthetic-key"
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            with tempfile.TemporaryDirectory() as directory:
                routes = Path(directory) / "routes.toml"
                routes.write_text(source)
                with open(Path(directory) / "gateway.log", "w+") as log:
                    process = subprocess.Popen(
                        [BINARY, "--config", str(routes), "--host", "127.0.0.1",
                         "--port", str(port)], env=environment,
                        stdout=log, stderr=subprocess.STDOUT,
                    )
                    url = f"http://127.0.0.1:{port}"
                    opener = build_opener(ProxyHandler({}))
                    for _ in range(100):
                        try:
                            opener.open(url + "/health", timeout=0.2).close()
                            break
                        except OSError:
                            if process.poll() is not None:
                                log.seek(0)
                                self.fail(log.read())
                            time.sleep(0.05)
                    else:
                        self.fail("Gateway did not become ready")
                    yield url, captured, config, opener
        finally:
            if process is not None:
                process.terminate()
                process.wait(timeout=8)
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)

    def test_native_aliases_preserve_streaming_effort_and_tool_continuations(self):
        exercised = 0
        for path in sorted(ROOT.glob("routes_*.toml")):
            config = tomllib.loads(compose_source(path))
            aliases = [
                route for route in config["routes"].values()
                if route["type"] == "passthrough"
                and config["llm_clients"][config["targets"][route["target"]]["llm_client"]]["format"]
                == "openai_responses"
            ]
            if not aliases:
                continue
            with self.gateway(path) as (url, captured, config, opener):
                for route in aliases:
                    target = config["targets"][route["target"]]
                    for stream in (False, True):
                        for override in (False, True):
                            with self.subTest(provider=path.name, route=route["id"],
                                              stream=stream, override=override):
                                history = [
                                    {"role": "user", "content": "Read the marker."},
                                    {"type": "function_call", "call_id": "call_fixture",
                                     "name": "lookup", "arguments": "{}"},
                                    {"type": "function_call_output", "call_id": "call_fixture",
                                     "output": "OK"},
                                ]
                                payload = {"model": route["id"], "input": history,
                                           "stream": stream, "max_output_tokens": 32}
                                if override:
                                    payload["reasoning"] = {"effort": "low"}
                                request = Request(
                                    url + "/v1/responses", data=json.dumps(payload).encode(),
                                    headers={"Content-Type": "application/json"},
                                )
                                with opener.open(request, timeout=5) as response:
                                    raw = response.read()
                                    if stream:
                                        events = [json.loads(line[6:]) for line in raw.splitlines()
                                                  if line.startswith(b"data: {")]
                                        self.assertTrue(any(event["type"] == "response.completed"
                                                            for event in events))
                                    else:
                                        self.assertEqual(json.loads(raw)["status"], "completed")
                                endpoint, body = captured[-1]
                                self.assertEqual(endpoint, "/v1/responses")
                                self.assertEqual(body["model"], target["model"])
                                self.assertEqual(body["stream"], stream)
                                self.assertEqual(body["input"][-2:], history[-2:])
                                self.assertEqual(body["reasoning"], payload.get(
                                    "reasoning", target["extra_body"]["reasoning"]))
                                self.assertEqual(body["max_output_tokens"], 32)
                                exercised += 1
        self.assertGreater(exercised, 0, "No native Responses aliases discovered")


if __name__ == "__main__":
    unittest.main()
