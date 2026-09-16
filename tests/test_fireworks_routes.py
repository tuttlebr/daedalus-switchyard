"""Opt-in wire tests against the built image, without contacting Fireworks.

SWITCHYARD_TEST_IMAGE=switchyard:fireworks-schema-test python3 -m unittest discover -s tests -v
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import Queue
import socket
import subprocess
import tempfile
import threading
import time
import tomllib
import unittest
from urllib.request import Request, urlopen
import uuid


ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("SWITCHYARD_TEST_IMAGE")


@unittest.skipUnless(IMAGE, "set SWITCHYARD_TEST_IMAGE to the patched image")
class FireworksRoutesTest(unittest.TestCase):
    @contextmanager
    def router(self, picker="efficient_first"):
        captured = Queue()

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captured.put((self.path, body))
                response = {
                    "id": "resp_test", "object": "response", "created_at": 1,
                    "status": "completed", "model": body["model"],
                    "output": [{
                        "type": "message", "id": "msg_test", "status": "completed",
                        "role": "assistant", "content": [{
                            "type": "output_text", "text": "OK", "annotations": [],
                        }],
                    }],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
                if body.get("stream"):
                    events = [
                        {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                        {"type": "response.output_item.added", "output_index": 0, "item": {**response["output"][0], "content": []}},
                        {"type": "response.content_part.added", "item_id": "msg_test", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
                        {"type": "response.output_text.delta", "item_id": "msg_test", "output_index": 0, "content_index": 0, "delta": "OK"},
                        {"type": "response.output_item.done", "output_index": 0, "item": response["output"][0]},
                        {"type": "response.completed", "response": response},
                    ]
                    data = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
                    content_type = "text/event-stream"
                else:
                    data = json.dumps(response).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            source = (ROOT / "routes_fireworks.toml").read_text()
            config = tomllib.loads(source)
            provider_url = config["llm_clients"]["fireworks"]["base_url"]
            source = source.replace(provider_url, f"http://127.0.0.1:{upstream.server_port}/v1")
            source = source.replace('picker = "efficient_first"', f'picker = "{picker}"')
            with tempfile.TemporaryDirectory(prefix="switchyard-fireworks-test-") as directory:
                route_path = Path(directory) / "routes.toml"
                route_path.write_text(source)
                name = f"switchyard-fireworks-test-{uuid.uuid4().hex[:12]}"
                process = subprocess.Popen([
                    "docker", "run", "--rm", "--network", "host", "--name", name,
                    "-e", "FIREWORKS_API_KEY=unused", "-v", f"{route_path}:/config.toml:ro",
                    IMAGE, "--config", "/config.toml", "--host", "127.0.0.1", "--port", str(port),
                ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            with urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                                break
                        except OSError:
                            if process.poll() is not None:
                                self.fail(process.communicate()[0])
                            if time.monotonic() > deadline:
                                self.fail("router did not become ready")
                            time.sleep(0.05)
                    yield f"http://127.0.0.1:{port}", captured, config
                finally:
                    subprocess.run(["docker", "stop", "--time", "1", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    output, _ = process.communicate(timeout=10)
                    self.assertNotIn("only one target per id", output)
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)

    def request(self, base_url, body):
        request = Request(base_url + "/v1/responses", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            return response.headers, response.read().decode()

    def assert_target(self, captured, config, target_name, headers):
        path, body = captured.get(timeout=5)
        target = config["targets"][target_name]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(body["model"], target["model"])
        self.assertEqual(headers["x-model-router-selected-model"], target["id"])
        for name, value in target["extra_body"].items():
            self.assertEqual(body[name], value)
        self.assertNotIn("chat_template_kwargs", body)
        return body

    def test_both_tiers_use_their_own_defaults(self):
        for picker, target in [("efficient_first", "weak"), ("capable_first", "strong")]:
            with self.subTest(target=target), self.router(picker) as (url, captured, config):
                headers, output = self.request(url, {"model": "daedalus", "input": "Say OK.", "stream": False})
                self.assertEqual(json.loads(output)["output"][0]["content"][0]["text"], "OK")
                self.assert_target(captured, config, target, headers)

    def test_tool_error_escalates_while_streaming(self):
        history = [
            {"role": "user", "content": "Run the build and fix it if it fails."},
            {"type": "function_call", "call_id": "call_test", "name": "exec_command", "arguments": '{"command":"make"}'},
            {"type": "function_call_output", "call_id": "call_test", "output": "Out of memory"},
        ]
        tools = [{"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}]
        with self.router() as (url, captured, config):
            headers, output = self.request(url, {"model": "daedalus", "input": history, "tools": tools, "stream": True})
            body = self.assert_target(captured, config, "strong", headers)
            self.assertEqual(body["input"], history)
            self.assertEqual(body["tools"], tools)
            events = [json.loads(line[6:]) for line in output.splitlines() if line.startswith("data: {")]
            self.assertTrue(any(event.get("type") == "response.completed" for event in events))
            self.assertTrue(any(event.get("delta") == "OK" for event in events))

    def test_caller_reasoning_overrides_target_defaults(self):
        with self.router() as (url, captured, config):
            headers, _ = self.request(url, {"model": "daedalus", "input": "Say OK.", "reasoning": {"effort": "low"}})
            _, body = captured.get(timeout=5)
            self.assertEqual(body["reasoning"], {"effort": "low"})
            self.assertEqual(body["model"], config["targets"]["weak"]["model"])
            self.assertEqual(headers["x-model-router-selected-model"], "fireworks-weak")


if __name__ == "__main__":
    unittest.main()
