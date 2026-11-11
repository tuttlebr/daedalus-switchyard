"""Caller streaming choice and mid-stream failure framing for every provider file.

Callers reach each target through Chat or Responses, whatever the provider's
own wire format. The gateway must answer in the mode the caller asked for and
report an upstream connection lost mid-stream as a terminal transport error.

SWITCHYARD_TEST_BINARY=/path/to/switchyard-server \
  python3 -m unittest discover -s tests -p test_stream_contract.py -v
"""

import json
import os
import re
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
USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def sse(events, named=False):
    return "".join(
        (f'event: {event["type"]}\n' if named else "") + f"data: {json.dumps(event)}\n\n"
        for event in events
    )


def chat_reply(body):
    message = {"role": "assistant", "content": "OK"}
    envelope = {"id": "chatcmpl-fixture", "created": 1, "model": body["model"]}
    if not body.get("stream"):
        return "application/json", json.dumps({
            **envelope, "object": "chat.completion", "usage": USAGE,
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        })
    envelope["object"] = "chat.completion.chunk"
    return "text/event-stream", sse([
        {**envelope, "choices": [{"index": 0, "delta": message, "finish_reason": None}]},
        {**envelope, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": USAGE},
    ]) + "data: [DONE]\n\n"


def responses_reply(body):
    item = {"id": "msg_fixture", "type": "message", "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "OK", "annotations": []}]}
    response = {"id": "resp_fixture", "object": "response", "created_at": 1,
                "status": "completed", "model": body["model"], "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    if not body.get("stream"):
        return "application/json", json.dumps(response)
    events = [
        {"type": "response.created",
         "response": {**response, "status": "in_progress", "output": []}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {**item, "status": "in_progress", "content": []}},
        {"type": "response.output_text.delta", "item_id": "msg_fixture",
         "output_index": 0, "content_index": 0, "delta": "OK"},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence
    return "text/event-stream", sse(events, named=True)


@unittest.skipUnless(BINARY, "set SWITCHYARD_TEST_BINARY to the built binary")
class StreamContractTest(unittest.TestCase):
    @contextmanager
    def gateway(self, path):
        state = {"captured": [], "abort": False}
        config = tomllib.loads(compose_source(path))
        choices = list(config["routes"]["stage"]["classifier"]["choices"])

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/systemone"):
                    content_type, raw = "application/json", json.dumps({
                        "model": "fixture-judge",
                        "answers": {"effort": {
                            "type": "choice", "choice": "low", "confidence": 1.0,
                            "probabilities": {
                                name: float(name == "low")
                                for name in (*choices, "uncertain")
                            },
                        }},
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    })
                else:
                    state["captured"].append((self.path, body))
                    reply = chat_reply if self.path.endswith("/chat/completions") else responses_reply
                    content_type, raw = reply(body)
                    if state["abort"]:
                        # One complete event, then drop the connection without
                        # the terminating chunk.
                        first = raw.split("\n\n")[0].encode() + b"\n\n"
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(first), first))
                        self.wfile.flush()
                        time.sleep(0.05)
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.close_connection = True
                        return
                raw = raw.encode()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        process = None
        try:
            base = f"http://127.0.0.1:{upstream.server_port}/v1"
            source = compose_source(path)
            environment = {"PATH": os.environ.get("PATH", "")}
            for client in config["llm_clients"].values():
                source = source.replace(client["base_url"], base)
                if client.get("api_key_env"):
                    environment[client["api_key_env"]] = "synthetic-key"
            classifier = config["routes"]["stage"]["classifier"]
            source = source.replace(classifier["base_url"], base)
            # Affinity placeholders and judge discovery are not under test.
            source = re.sub(r"(?m)^extra_headers = .*\n", "", source)
            source = source.replace('model = "auto"', 'model = "fixture-judge"')
            source = source.replace('max_input_tokens = "auto"', "max_input_tokens = 16384")
            source = source.replace("truncate_input = true", "truncate_input = false")
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
                    for _ in range(200):
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

                    def call(model, inbound, stream):
                        payload = {"model": model}
                        if stream is not None:
                            payload["stream"] = stream
                        if inbound == "chat":
                            payload["messages"] = [{"role": "user", "content": "Reply OK."}]
                            endpoint = "/v1/chat/completions"
                        else:
                            payload["input"] = [{"role": "user", "content": "Reply OK."}]
                            endpoint = "/v1/responses"
                        state["captured"].clear()
                        request = Request(
                            url + endpoint, data=json.dumps(payload).encode(),
                            headers={"Content-Type": "application/json"},
                        )
                        with opener.open(request, timeout=10) as response:
                            return (response.headers.get("Content-Type", ""),
                                    response.read().decode())

                    yield call, state, config
        finally:
            if process is not None:
                process.terminate()
                process.wait(timeout=8)
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)

    def test_every_route_answers_in_the_mode_the_caller_requested(self):
        exercised = 0
        for path in sorted(ROOT.glob("routes_*.toml")):
            with self.gateway(path) as (call, state, config):
                for route in config["routes"].values():
                    for inbound in ("chat", "responses"):
                        # Omitting stream is the API's non-streaming default.
                        for stream in (False, None, True):
                            with self.subTest(provider=path.name, route=route["id"],
                                              inbound=inbound, stream=stream):
                                content_type, raw = call(route["id"], inbound, stream)
                                _endpoint, sent = state["captured"][-1]
                                self.assertEqual(bool(sent.get("stream")), bool(stream))
                                if stream:
                                    self.assertTrue(content_type.startswith("text/event-stream"))
                                    self.assertIn("data: ", raw)
                                else:
                                    self.assertTrue(content_type.startswith("application/json"))
                                    self.assertIn("OK", json.dumps(json.loads(raw)))
                                exercised += 1
        self.assertGreater(exercised, 0, "No provider routes discovered")

    def test_midstream_upstream_abort_is_a_terminal_transport_error(self):
        # Consumers retry on this exact envelope; other gateway failures keep
        # their own messages and must not be mistaken for a lost connection.
        for path in sorted(ROOT.glob("routes_*.toml")):
            with self.gateway(path) as (call, state, _config):
                state["abort"] = True
                for inbound in ("chat", "responses"):
                    with self.subTest(provider=path.name, inbound=inbound):
                        content_type, raw = call("daedalus", inbound, True)
                        self.assertTrue(content_type.startswith("text/event-stream"))
                        self.assertEqual(len(state["captured"]), 1)
                        frames = [frame for frame in raw.split("\n\n") if frame.strip()]
                        data = [line[6:] for line in frames[-1].splitlines()
                                if line.startswith("data: ")]
                        error = json.loads(data[-1])["error"]
                        self.assertEqual(error["type"], "SwitchyardError")
                        self.assertTrue(
                            error["message"].startswith("upstream transport error: "),
                            error["message"],
                        )
                        self.assertNotIn("[DONE]", raw)
                        self.assertNotIn("response.completed", raw)


if __name__ == "__main__":
    unittest.main()
