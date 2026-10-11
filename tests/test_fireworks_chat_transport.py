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
    def router(
        self,
        picker=None,
        forced_effort=False,
        composite=False,
        verdict=None,
        judge_status=200,
        stage_threshold=None,
        judge_delay=0,
        judge_body_delay=0,
        decision_auth=False,
        decision_suffix="/v1",
        source_transform=None,
    ):
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
                if self.path.endswith("/systemone"):
                    time.sleep(judge_delay)
                    result = (
                        verdict
                        if verdict is not None
                        else {
                            "model": "Cloudflare/clef-flash",
                            "answers": {
                                "effort": {
                                    "type": "choice",
                                    "choice": "efficient",
                                    "confidence": 0.96,
                                    "probabilities": {
                                        "efficient": 0.96,
                                        "capable": 0.03,
                                        "uncertain": 0.01,
                                    },
                                }
                            },
                            "usage": {"input_tokens": 123, "output_tokens": 0},
                        }
                    )
                    data = (
                        result if isinstance(result, str) else json.dumps(result)
                    ).encode()
                    self.send_response(judge_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    time.sleep(judge_body_delay)
                    try:
                        self.wfile.write(data)
                    except OSError:
                        pass
                    return
                if body.get("response_format"):
                    time.sleep(judge_delay)
                    result = verdict or {
                        "crux": "bounded fixture",
                        "primary_rule": "SUP-1",
                        "capability_boundary": "supported",
                        "p_solve": 0.9,
                    }
                    data = json.dumps(
                        {
                            **chunk,
                            "object": "chat.completion",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": json.dumps(result),
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    ).encode()
                    self.send_response(judge_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    try:
                        self.wfile.write(data)
                    except OSError:
                        pass
                    return
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
                if composite is True:
                    # Freeze the earlier two-target contract independently of the current policy.
                    start = source.index("[routes.stage.classifier]")
                    end = source.index("[routes.stage.stage]", start)
                    native = config["routes"]["stage"]["classifier"]
                    source = (
                        source[:start]
                        + (
                            '[routes.stage.classifier]\ntype = "decision_api"\n'
                            f'base_url = "{native["base_url"]}"\nmodel = "{native["model"]}"\n'
                            'classify_trigger = "user_turn"\nefficient_threshold = 0.9\n'
                            "timeout_ms = 2000\nrecent_turn_window = 6\nmax_request_bytes = 12000\nmax_input_tokens = 16384\n"
                            'instructions = "Judge the whole task and exact output constraints."\n'
                            'efficient_criteria = "Routine tasks at medium."\n'
                            'capable_criteria = "Complex tasks at xhigh."\n'
                            'uncertain_criteria = "Missing or ambiguous context."\n\n'
                        )
                        + source[end:]
                    )
                if composite != "native_targets":
                    # Keep legacy stage assertions tied to their original medium/xhigh pair.
                    source = source.replace(
                        'efficient_target = "high"', 'efficient_target = "medium"'
                    )
                if source_transform:
                    source = source_transform(source)
                config = tomllib.loads(source)
                # Retain standalone-stage compatibility checks as a separate fixture.
                if not composite:
                    stage = config["routes"]["stage"]["stage"]
                    start = source.index("[routes.stage]")
                    end = source.index("[routes.max]", start)
                    source = (
                        source[:start]
                        + (
                            '[routes.stage]\nid = "daedalus"\ntype = "stage_router"\n'
                            f'capable_target = "{stage["capable_target"]}"\n'
                            f'efficient_target = "{stage["efficient_target"]}"\n'
                            f'picker = "{picker or "capable_first"}"\n'
                            "confidence_threshold = 0.5\nrecent_turn_window = 3\n"
                            "tool_calling = true\nreasoning = true\n\n"
                        )
                        + source[end:]
                    )
                    config = tomllib.loads(source)
                if composite == "llm":
                    # The native default must keep the previous LLM configuration compatible.
                    start = source.index("[routes.stage.classifier]")
                    end = source.index("[routes.stage.stage]", start)
                    provider = config["llm_clients"]["fireworks"]["base_url"]
                    source = (
                        source[:start]
                        + (
                            '[routes.stage.classifier]\ntarget = "fixture_judge"\n'
                            "base_threshold = 0.5\nthreshold_step = 0.1\n"
                            'classify_trigger = "user_turn"\nrecent_turn_window = 6\n\n'
                        )
                        + source[end:]
                    )
                    source += (
                        '[llm_clients.fixture_judge]\nformat = "openai_chat"\n'
                        f'base_url = "{provider}"\napi_key_env = "FIREWORKS_API_KEY"\n'
                        "timeout_ms = 5000\nmax_retries = 0\n\n"
                        '[targets.fixture_judge]\nid = "fixture-judge"\n'
                        'model = "accounts/fireworks/routers/glm-5p3-fast"\n'
                        'llm_client = "fixture_judge"\nreasoning_effort = "low"\n'
                    )
                    config = tomllib.loads(source)
                elif composite:
                    provider = config["routes"]["stage"]["classifier"]["base_url"]
                    source = source.replace(
                        provider,
                        f"http://127.0.0.1:{upstream.server_port}{decision_suffix}",
                    )
                    if decision_auth:
                        source = source.replace(
                            'type = "decision_api"',
                            'type = "decision_api"\napi_key_env = "NATIVE_DECISION_TEST_KEY"',
                        )
                if judge_delay or judge_body_delay:
                    source = source.replace(
                        "timeout_ms = 5000", "timeout_ms = 40"
                    ).replace("timeout_ms = 2000", "timeout_ms = 40")
                if stage_threshold is not None:
                    source = source.replace(
                        "confidence_threshold = 0.5",
                        f"confidence_threshold = {stage_threshold}",
                    )
                if forced_effort:
                    for effort in ("max", "xhigh", "high", "medium", "low"):
                        source = source.replace(
                            f'extra_body = {{ reasoning_effort = "{effort}", reasoning_history = "interleaved" }}',
                            f'reasoning_effort = "{effort}"\nextra_body = {{ reasoning_history = "interleaved" }}',
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
                    "NATIVE_DECISION_TEST_KEY": "native-decision-secret",
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

    def test_provider_auth_does_not_forward_caller_affinity_headers(self):
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
                self.assertNotIn("x-session-affinity", lowered)
                self.assertNotIn("x-multi-turn-session-id", lowered)

    def test_tool_continuation_keeps_reasoning_in_fireworks_native_field(self):
        history = [
            {"role": "user", "content": "Read the fixture."},
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Read before answering."}],
            },
            {
                "type": "function_call",
                "call_id": "call_reasoning",
                "name": "mcp_read",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_reasoning",
                "output": "41",
            },
        ]
        with self.router() as (url, captured, _config):
            for streaming in (False, True):
                self.request(url, input=history, stream=streaming)
                _, body, _ = captured.get(timeout=5)
                assistant = body["messages"][-2]
                self.assertEqual(
                    assistant["reasoning_content"], "Read before answering."
                )
                self.assertNotIn("reasoning", assistant)
                self.assertEqual(assistant["tool_calls"][0]["id"], "call_reasoning")

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
                "output": "out of memory",
            },
        ]
        for picker, tier in (
            ("capable_first", "capable_target"),
            ("efficient_first", "efficient_target"),
        ):
            with (
                self.subTest(picker=picker),
                self.router(picker=picker) as (
                    url,
                    captured,
                    config,
                ),
            ):
                stage = config["routes"]["stage"]
                headers = self.request(url)
                self.assertEqual(headers["x-model-router-selected-model"], stage[tier])
                captured.get(timeout=5)
                headers = self.request(url, input=history)
                self.assertEqual(
                    headers["x-model-router-selected-model"], stage["capable_target"]
                )
                captured.get(timeout=5)

    def test_repeated_hard_failures_escalate_efficient_first(self):
        history = [{"role": "user", "content": "Read the fixture."}]
        with self.router(picker="efficient_first") as (url, captured, config):
            stage = config["routes"]["stage"]
            for attempt, tier in (
                (1, "efficient_target"),
                (2, "capable_target"),
            ):
                call_id = f"call_failure_{attempt}"
                history.extend(
                    [
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": "mcp_read",
                            "arguments": "{}",
                        },
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": "Connection refused",
                        },
                    ]
                )
                headers = self.request(url, input=history)
                self.assertEqual(headers["x-model-router-selected-model"], stage[tier])
                captured.get(timeout=5)

    def test_native_target_effort_can_enforce_the_selected_tier(self):
        with self.router(forced_effort=True) as (url, captured, config):
            headers = self.request(url, reasoning={"effort": "low"})
            _, body, _ = captured.get(timeout=5)
            target = config["routes"]["stage"]["capable_target"]
            self.assertEqual(headers["x-model-router-selected-model"], target)
            self.assertEqual(body["reasoning_effort"], target)
            self.assertEqual(body["reasoning_history"], "interleaved")

    def test_decision_resolves_provider_model_without_calling_it(self):
        with self.router() as (url, captured, config):
            request = Request(
                url + "/v1/decision",
                data=json.dumps(
                    {
                        "input_format": "openai_responses",
                        "request": {"model": "daedalus", "input": "Say OK."},
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=5) as response:
                result = json.load(response)
            target = config["routes"]["stage"]["capable_target"]
            self.assertEqual(result["selected"]["target"], target)
            self.assertEqual(
                result["selected"]["model"], config["targets"][target]["model"]
            )
            self.assertEqual(
                result["selected"]["extra_body"]["reasoning_effort"], target
            )
            self.assertTrue(captured.empty())

    def continuation(
        self, name="perplexity_search_tool", output="fixture found", repeats=1
    ):
        history = [{"role": "user", "content": "Read the fixture."}]
        for n in range(repeats):
            history.extend(
                [
                    {
                        "type": "function_call",
                        "call_id": f"call_{n}",
                        "name": name,
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": f"call_{n}",
                        "output": output,
                    },
                ]
            )
        return history

    def test_composite_reuses_verdict_across_tool_rounds_and_rejudges_user(self):
        with self.router(composite="llm") as (url, captured, _):
            session = {"x-switchyard-session-id": "scoped-fixture-a"}
            headers = self.request(url, headers=session)
            self.assertEqual(headers["x-model-router-selected-model"], "medium")
            _, judge, _ = captured.get(timeout=5)
            self.assertEqual(judge["reasoning_effort"], "low")
            self.assertEqual(judge["response_format"]["type"], "json_schema")
            _, answer, _ = captured.get(timeout=5)
            self.assertEqual(answer["reasoning_effort"], "medium")
            for stream in (False, True):
                self.request(
                    url, headers=session, input=self.continuation(), stream=stream
                )
                _, answer, outbound = captured.get(timeout=5)
                self.assertNotIn("response_format", answer)
                self.assertFalse(any("session" in k.lower() for k in outbound))
                self.assertTrue(captured.empty())
            history = self.continuation() + [
                {"role": "user", "content": "Now compare conflicting evidence."}
            ]
            self.request(url, headers=session, input=history)
            _, judge, _ = captured.get(timeout=5)
            self.assertIn("response_format", judge)
            self.assertIn(
                "Now compare conflicting evidence.", json.dumps(judge["messages"])
            )
            captured.get(timeout=5)
            self.assertTrue(captured.empty())

    def test_composite_sessions_do_not_share_retained_tiers(self):
        strong = {
            "crux": "complex fixture",
            "primary_rule": "UNC-2",
            "capability_boundary": "uncertain",
            "p_solve": 0.2,
        }
        with self.router(composite="llm", verdict=strong) as (url, captured, _):
            self.request(url, headers={"x-switchyard-session-id": "user-a"})
            captured.get(timeout=5)
            captured.get(timeout=5)
            headers = self.request(
                url,
                input=self.continuation(),
                headers={"x-switchyard-session-id": "user-a"},
            )
            self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
            captured.get(timeout=5)
            for session in ("user-b", None):
                headers = self.request(
                    url,
                    input=self.continuation(),
                    headers={"x-switchyard-session-id": session} if session else {},
                )
                self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
                captured.get(timeout=5)
                self.assertTrue(captured.empty())

    def test_composite_invalid_or_inconsistent_judge_defaults_capable(self):
        for verdict in (
            {"bad": "verdict"},
            {
                "crux": "invalid",
                "primary_rule": "SUP-1",
                "capability_boundary": "unsupported",
                "p_solve": 0.99,
            },
        ):
            with (
                self.subTest(verdict=verdict),
                self.router(composite="llm", verdict=verdict) as (url, captured, _),
            ):
                headers = self.request(url)
                self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
                captured.get(timeout=5)
                captured.get(timeout=5)
                self.assertTrue(captured.empty())

    def test_composite_transport_failure_defaults_capable(self):
        with self.router(composite="llm", judge_status=500) as (url, captured, _):
            headers = self.request(url, stream=False)
            self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
            _, judge, _ = captured.get(timeout=5)
            self.assertIn("response_format", judge)
            _, answer, _ = captured.get(timeout=5)
            self.assertEqual(answer["reasoning_effort"], "xhigh")
            self.assertTrue(captured.empty())

    def test_composite_judge_deadline_defaults_capable_and_retains_it(self):
        with self.router(composite="llm", judge_delay=0.2) as (url, captured, _):
            session = {"x-switchyard-session-id": "deadline-fixture"}
            headers = self.request(url, headers=session, stream=False)
            self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
            captured.get(timeout=5)
            captured.get(timeout=5)
            headers = self.request(url, headers=session, input=self.continuation())
            self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
            _, answer, _ = captured.get(timeout=5)
            self.assertNotIn("response_format", answer)
            self.assertTrue(captured.empty())

    def test_explicit_profiles_bypass_composite_judge(self):
        with self.router(composite="llm", judge_status=500) as (url, captured, config):
            for route in config["routes"].values():
                if route["type"] != "passthrough" or route.get("target") == "local":
                    continue
                headers = self.request(url, model=route["id"])
                _, answer, _ = captured.get(timeout=5)
                self.assertEqual(
                    headers["x-model-router-selected-model"], route["target"]
                )
                self.assertNotIn("response_format", answer)
                self.assertTrue(captured.empty())

    def test_composite_critical_failure_overrides_retained_medium(self):
        with self.router(composite="llm") as (url, captured, _):
            session = {"x-switchyard-session-id": "recovery-fixture"}
            self.request(url, headers=session)
            captured.get(timeout=5)
            captured.get(timeout=5)
            headers = self.request(
                url, headers=session, input=self.continuation(output="out of memory")
            )
            self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
            captured.get(timeout=5)
            self.assertTrue(captured.empty())

    def test_custom_mutations_and_new_activity_change_stage_selection(self):
        # Lower the fixture threshold to isolate one signal; production stays 0.5.
        with self.router(composite="llm", stage_threshold=0.3) as (url, captured, _):
            # Without a retained verdict, capable is the safe default. A mapped
            # mutation supplies production evidence; an unknown wrapper does not.
            for name, expected in (
                ("add_memory", "medium"),
                ("hue_mcp_server__set_light_state", "medium"),
                ("mixed_dispatcher", "xhigh"),
            ):
                headers = self.request(url, input=self.continuation(name))
                self.assertEqual(headers["x-model-router-selected-model"], expected)
                captured.get(timeout=5)
            session = {"x-switchyard-session-id": "new-activity-fixture"}
            self.request(url, headers=session)
            captured.get(timeout=5)
            captured.get(timeout=5)
            for name, expected in (
                ("content_distiller_tool", "medium"),
                ("unknown_fixture", "xhigh"),
            ):
                history = self.continuation(name, repeats=8)
                # Different arguments/results avoid confusing repeated activity
                # with the stagnation rule under test.
                for index, item in enumerate(history):
                    if item.get("type") == "function_call":
                        item["arguments"] = json.dumps({"value": index})
                    if item.get("type") == "function_call_output":
                        item["output"] = f"fixture result {index}"
                headers = self.request(url, headers=session, input=history)
                self.assertEqual(headers["x-model-router-selected-model"], expected)
                captured.get(timeout=5)

    @staticmethod
    def native_verdict(
        efficient=0.96, capable=0.03, uncertain=0.01, choice="efficient"
    ):
        probabilities = {
            "efficient": efficient,
            "capable": capable,
            "uncertain": uncertain,
        }
        return {
            "model": "Cloudflare/clef-flash",
            "answers": {
                "effort": {
                    "type": "choice",
                    "choice": choice,
                    "confidence": probabilities.get(choice, 0.0),
                    "probabilities": probabilities,
                }
            },
            "usage": {"input_tokens": 123, "output_tokens": 0},
        }

    def test_native_judge_uses_systemone_and_reuses_session_on_tool_rounds(self):
        with self.router(composite=True) as (url, captured, _):
            session = {"x-switchyard-session-id": "native-session-a"}
            headers = self.request(
                url, headers=session, instructions="Preserve all constraints."
            )
            self.assertEqual(headers["x-model-router-selected-model"], "medium")
            path, judge, outbound = captured.get(timeout=5)
            self.assertEqual(path, "/v1/systemone")
            self.assertEqual(judge["model"], "Cloudflare/clef-flash")
            self.assertEqual(judge["questions"]["effort"]["type"], "choice")
            self.assertEqual(
                set(judge["questions"]["effort"]["criteria"]),
                {"efficient", "capable", "uncertain"},
            )
            self.assertIn("Preserve all constraints.", json.dumps(judge["state"]))
            self.assertFalse(
                {
                    "messages",
                    "response_format",
                    "reasoning_effort",
                    "max_tokens",
                    "max_completion_tokens",
                }
                & set(judge)
            )
            self.assertNotIn("authorization", {k.lower() for k in outbound})
            captured.get(timeout=5)
            for stream in (False, True):
                self.request(
                    url, headers=session, input=self.continuation(), stream=stream
                )
                path, body, _ = captured.get(timeout=5)
                self.assertEqual(path, "/v1/chat/completions")
                self.assertEqual(body["reasoning_effort"], "medium")
                self.assertTrue(captured.empty())
            self.request(
                url,
                headers=session,
                input=self.continuation()
                + [{"role": "user", "content": "Now assess conflicting sources."}],
            )
            path, judge, _ = captured.get(timeout=5)
            self.assertEqual(path, "/v1/systemone")
            self.assertIn("Now assess conflicting sources.", json.dumps(judge["state"]))
            self.assertIn("Read the fixture.", json.dumps(judge["state"]))
            captured.get(timeout=5)

    def test_native_choice_threshold_uncertainty_and_session_isolation(self):
        for e, c, u, choice, expected in (
            (0.90, 0.08, 0.02, "efficient", "medium"),
            (0.89, 0.08, 0.03, "efficient", "xhigh"),
            (0.01, 0.96, 0.03, "capable", "xhigh"),
            (0.01, 0.03, 0.96, "uncertain", "xhigh"),
        ):
            with (
                self.subTest(choice=choice, p=e),
                self.router(
                    composite=True, verdict=self.native_verdict(e, c, u, choice)
                ) as (url, captured, _),
            ):
                session = {"x-switchyard-session-id": "native-user-a"}
                headers = self.request(url, headers=session)
                self.assertEqual(headers["x-model-router-selected-model"], expected)
                captured.get(timeout=5)
                captured.get(timeout=5)
                headers = self.request(url, headers=session, input=self.continuation())
                self.assertEqual(headers["x-model-router-selected-model"], expected)
                captured.get(timeout=5)
                headers = self.request(
                    url,
                    headers={"x-switchyard-session-id": "native-user-b"},
                    input=self.continuation(),
                )
                self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
                captured.get(timeout=5)
                self.assertTrue(captured.empty())

    def test_native_invalid_distributions_and_wrong_model_default_capable(self):
        invalid = [
            {},
            "not JSON",
            self.native_verdict(0.96, 0.03, 0.01, "unconfigured"),
        ]
        for change in (
            "missing_option",
            "sum",
            "range",
            "confidence",
            "choice",
            "wrong_model",
            "wrong_type",
            "wrong_question",
            "generation",
            "context_limit",
        ):
            value = self.native_verdict()
            answer = value["answers"]["effort"]
            if change == "missing_option":
                del answer["probabilities"]["uncertain"]
            elif change == "sum":
                answer["probabilities"]["efficient"] = 0.5
            elif change == "range":
                answer["probabilities"]["uncertain"] = -0.01
            elif change == "confidence":
                answer["confidence"] = 1.1
            elif change == "choice":
                answer["choice"] = "capable"
                answer["confidence"] = 0.03
            elif change == "wrong_model":
                value["model"] = "different-model"
            elif change == "wrong_type":
                answer["type"] = "score"
            elif change == "wrong_question":
                value["answers"] = {"other": answer}
            elif change == "generation":
                value["usage"]["output_tokens"] = 12
            elif change == "context_limit":
                value["usage"]["input_tokens"] = 16384
            invalid.append(value)
        for value in invalid:
            with (
                self.subTest(value=value),
                self.router(composite=True, verdict=value) as (url, captured, _),
            ):
                headers = self.request(url)
                self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
                captured.get(timeout=5)
                _, answer, _ = captured.get(timeout=5)
                self.assertEqual(answer["reasoning_effort"], "xhigh")
                self.assertTrue(captured.empty())

    def test_native_probability_rounding_and_endpoint_normalization(self):
        verdict = self.native_verdict(0.9333, 0.0333, 0.0333)
        for suffix in ("", "/v1/", "/v1/systemone"):
            with (
                self.subTest(suffix=suffix),
                self.router(
                    composite=True, verdict=verdict, decision_suffix=suffix
                ) as (url, captured, _),
            ):
                headers = self.request(url)
                self.assertEqual(headers["x-model-router-selected-model"], "medium")
                path, _, _ = captured.get(timeout=5)
                self.assertEqual(path, "/v1/systemone")
                captured.get(timeout=5)

    def test_native_normalized_confidence_is_distinct_from_option_probability(self):
        verdict = self.native_verdict()
        verdict["answers"]["effort"]["confidence"] = (3 * 0.96 - 1) / 2
        with self.router(composite=True, verdict=verdict) as (url, captured, _):
            headers = self.request(url)
            self.assertEqual(headers["x-model-router-selected-model"], "medium")
            captured.get(timeout=5)
            captured.get(timeout=5)
            self.assertTrue(captured.empty())

    def test_native_errors_and_body_deadline_retain_capable_fallback(self):
        for options in (
            {"judge_status": 503},
            {"judge_delay": 0.2},
            {"judge_body_delay": 0.2},
        ):
            with (
                self.subTest(options=options),
                self.router(composite=True, **options) as (url, captured, _),
            ):
                session = {"x-switchyard-session-id": "native-error-session"}
                headers = self.request(url, headers=session, stream=False)
                self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
                captured.get(timeout=5)
                captured.get(timeout=5)
                headers = self.request(url, headers=session, input=self.continuation())
                self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
                captured.get(timeout=5)
                self.assertTrue(captured.empty())

    def test_native_auth_is_server_owned_and_caller_headers_stay_at_gateway(self):
        with self.router(composite=True, decision_auth=True) as (url, captured, _):
            self.request(
                url,
                headers={
                    "Authorization": "Bearer caller-secret",
                    "x-switchyard-session-id": "private-session",
                    "x-request-id": "private-request",
                },
                user="private-user-hint",
            )
            _, body, headers = captured.get(timeout=5)
            lowered = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(lowered["authorization"], "Bearer native-decision-secret")
            for name in ("x-switchyard-session-id", "x-request-id", "x-user-id"):
                self.assertNotIn(name, lowered)
            self.assertNotIn("private-session", json.dumps(body))
            self.assertNotIn("private-user-hint", json.dumps(body))
            captured.get(timeout=5)

    def test_native_input_budget_and_media_do_not_truncate_answer_request(self):
        for content in (
            "x" * 14000,
            [
                {"type": "input_text", "text": "Inspect the image."},
                {"type": "input_image", "image_url": "https://example.test/image.png"},
            ],
        ):
            with (
                self.subTest(content_type=type(content).__name__),
                self.router(composite=True) as (url, captured, _),
            ):
                history = [{"role": "user", "content": content}]
                headers = self.request(url, input=history)
                self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
                path, body, _ = captured.get(timeout=5)
                self.assertEqual(path, "/v1/chat/completions")
                self.assertEqual(
                    body["messages"][-1]["content"],
                    content
                    if isinstance(content, str)
                    else [
                        {"type": "text", "text": "Inspect the image."},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.test/image.png"},
                        },
                    ],
                )
                self.assertTrue(captured.empty())

    def test_native_explicit_profiles_and_critical_errors_keep_their_policy(self):
        with self.router(composite=True, judge_status=503) as (url, captured, config):
            for route in config["routes"].values():
                if route["type"] == "passthrough" and route.get("target") != "local":
                    headers = self.request(url, model=route["id"])
                    path, _, _ = captured.get(timeout=5)
                    self.assertEqual(path, "/v1/chat/completions")
                    self.assertEqual(
                        headers["x-model-router-selected-model"], route["target"]
                    )
                    self.assertTrue(captured.empty())
        with self.router(composite=True) as (url, captured, _):
            session = {"x-switchyard-session-id": "native-critical-session"}
            self.request(url, headers=session)
            captured.get(timeout=5)
            captured.get(timeout=5)
            headers = self.request(
                url, headers=session, input=self.continuation(output="out of memory")
            )
            self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
            captured.get(timeout=5)
            self.assertTrue(captured.empty())

    def test_native_live_probe_uses_the_configured_question_contract(self):
        import importlib.util
        import io
        from contextlib import redirect_stdout

        spec = importlib.util.spec_from_file_location(
            "decision_probe", ROOT / "scripts/test_decision_judge.py"
        )
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        with self.router(composite=True) as (url, captured, config):
            self.request(url)
            _, expected, native_headers = captured.get(timeout=5)
            captured.get(timeout=5)
            with redirect_stdout(io.StringIO()):
                results = probe.probe(
                    config["routes"]["stage"]["classifier"],
                    "http://"
                    + {k.lower(): v for k, v in native_headers.items()}["host"],
                )
            self.assertEqual(len(results), 4)
            for _ in results:
                path, request, _ = captured.get(timeout=5)
                self.assertEqual(path, "/v1/systemone")
                self.assertEqual(request["questions"], expected["questions"])
                self.assertEqual(request["model"], expected["model"])

    def test_none_alias_keeps_low_effort_and_in_turn_history(self):
        with self.router() as (url, captured, _config):
            headers = self.request(url, model="daedalus/none")
            _, body, _ = captured.get(timeout=5)
            self.assertEqual(headers["x-model-router-selected-model"], "low")
            self.assertEqual(body["reasoning_effort"], "low")
            self.assertEqual(body["reasoning_history"], "interleaved")

    @staticmethod
    def target_verdict(choice, probability=0.96):
        options = ("low", "medium", "high", "xhigh", "uncertain")
        probabilities = {name: (1 - probability) / 4 for name in options}
        probabilities[choice] = probability
        return {
            "model": "Cloudflare/clef-flash",
            "answers": {
                "effort": {
                    "type": "choice",
                    "choice": choice,
                    "confidence": probability,
                    "probabilities": probabilities,
                }
            },
            "usage": {"input_tokens": 123, "output_tokens": 0},
        }

    def test_named_choices_select_four_targets_and_retain_them(self):
        for target in ("low", "medium", "high", "xhigh"):
            with (
                self.subTest(target=target),
                self.router(
                    composite="native_targets", verdict=self.target_verdict(target)
                ) as (url, captured, config),
            ):
                session = {"x-switchyard-session-id": "named-task"}
                headers = self.request(url, headers=session)
                self.assertEqual(headers["x-model-router-selected-model"], target)
                _, judge, _ = captured.get(timeout=5)
                self.assertEqual(
                    set(judge["questions"]["effort"]["criteria"]),
                    {"low", "medium", "high", "xhigh", "uncertain"},
                )
                _, body, _ = captured.get(timeout=5)
                self.assertEqual(body["model"], config["targets"][target]["model"])
                self.assertEqual(body["reasoning_effort"], target)
                headers = self.request(url, headers=session, input=self.continuation())
                self.assertEqual(headers["x-model-router-selected-model"], target)
                captured.get(timeout=5)
                self.assertTrue(captured.empty())

    def test_named_choice_probability_gate_and_uncertainty_use_capable(self):
        for choice, probability in (
            ("low", 0.89),
            ("medium", 0.89),
            ("uncertain", 0.96),
        ):
            with (
                self.subTest(choice=choice),
                self.router(
                    composite="native_targets",
                    verdict=self.target_verdict(choice, probability),
                ) as (url, captured, _),
            ):
                session = {"x-switchyard-session-id": "named-uncertainty"}
                headers = self.request(url, headers=session)
                self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
                captured.get(timeout=5)
                captured.get(timeout=5)
                headers = self.request(url, headers=session, input=self.continuation())
                self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
                captured.get(timeout=5)
                self.assertTrue(captured.empty())

    def test_named_choices_apply_independent_probability_thresholds(self):
        def thresholds(source):
            return source.replace(
                'target = "low"\nmin_probability = 0.90',
                'target = "low"\nmin_probability = 0.80',
            ).replace(
                'target = "medium"\nmin_probability = 0.90',
                'target = "medium"\nmin_probability = 0.95',
            )

        for choice, probability, expected in (
            ("low", 0.85, "low"),
            ("medium", 0.93, "xhigh"),
        ):
            with (
                self.subTest(choice=choice),
                self.router(
                    composite="native_targets",
                    verdict=self.target_verdict(choice, probability),
                    source_transform=thresholds,
                ) as (url, captured, _),
            ):
                headers = self.request(url)
                self.assertEqual(headers["x-model-router-selected-model"], expected)
                captured.get(timeout=5)
                captured.get(timeout=5)

    def test_named_choices_recovery_hold_returns_to_selected_floor(self):
        with self.router(
            composite="native_targets", verdict=self.target_verdict("low")
        ) as (url, captured, _):
            session = {"x-switchyard-session-id": "named-recovery"}
            self.request(url, headers=session)
            captured.get(timeout=5)
            captured.get(timeout=5)
            headers = self.request(
                url, headers=session, input=self.continuation(output="out of memory")
            )
            self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
            captured.get(timeout=5)
            for expected in ("xhigh", "xhigh", "low"):
                headers = self.request(url, headers=session, input=self.continuation())
                self.assertEqual(headers["x-model-router-selected-model"], expected)
                captured.get(timeout=5)
            self.assertTrue(captured.empty())

    def test_named_option_and_target_alias_are_independent(self):
        verdict = self.target_verdict("high")
        answer = verdict["answers"]["effort"]
        answer["choice"] = "balanced"
        answer["probabilities"]["balanced"] = answer["probabilities"].pop("high")

        def rename(source):
            return source.replace(
                "classifier.choices.high]", "classifier.choices.balanced]"
            ).replace(
                '[targets.high]\nid = "high"', '[targets.high]\nid = "high-alias"'
            )

        with self.router(
            composite="native_targets", verdict=verdict, source_transform=rename
        ) as (url, captured, config):
            headers = self.request(url)
            self.assertEqual(headers["x-model-router-selected-model"], "high-alias")
            _, judge, _ = captured.get(timeout=5)
            self.assertIn("balanced", judge["questions"]["effort"]["criteria"])
            _, body, _ = captured.get(timeout=5)
            self.assertEqual(body["model"], config["targets"]["high"]["model"])
            self.assertEqual(body["reasoning_effort"], "high")

    def test_named_choices_reject_stale_binary_labels_and_unknown_options(self):
        invalid = [self.native_verdict(), self.target_verdict("low")]
        invalid[1]["answers"]["effort"]["probabilities"]["unconfigured"] = 0.0
        for verdict in invalid:
            with (
                self.subTest(verdict=verdict),
                self.router(composite="native_targets", verdict=verdict) as (
                    url,
                    captured,
                    _,
                ),
            ):
                headers = self.request(url)
                self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
                captured.get(timeout=5)
                captured.get(timeout=5)

    def test_named_choices_unknown_sessions_and_service_failures_use_capable(self):
        with self.router(composite="native_targets", judge_status=503) as (
            url,
            captured,
            _,
        ):
            session = {"x-switchyard-session-id": "named-unavailable"}
            headers = self.request(url, headers=session)
            self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
            captured.get(timeout=5)
            captured.get(timeout=5)
            headers = self.request(
                url,
                headers={"x-switchyard-session-id": "other-user"},
                input=self.continuation(),
            )
            self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
            captured.get(timeout=5)
            self.assertTrue(captured.empty())

    def test_named_live_probe_matches_the_gateway_question_and_target_policy(self):
        import importlib.util
        import io
        from contextlib import redirect_stdout

        spec = importlib.util.spec_from_file_location(
            "named_probe", ROOT / "scripts/test_decision_judge.py"
        )
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        with self.router(
            composite="native_targets", verdict=self.target_verdict("medium")
        ) as (url, captured, config):
            self.request(url)
            _, expected, native_headers = captured.get(timeout=5)
            captured.get(timeout=5)
            route = config["routes"]["stage"]
            with redirect_stdout(io.StringIO()):
                results = probe.probe(
                    route["classifier"],
                    "http://"
                    + {k.lower(): v for k, v in native_headers.items()}["host"],
                    route["stage"]["capable_target"],
                )
            for row in results:
                self.assertEqual(row["selected_target"], "medium")
                _, request, _ = captured.get(timeout=5)
                self.assertEqual(request["questions"], expected["questions"])

    def test_named_decisions_only_fall_back_to_capable_and_rejudge_user_turns(self):
        for target in ("low", "xhigh"):
            with (
                self.subTest(target=target),
                self.router(
                    composite="native_targets", verdict=self.target_verdict(target)
                ) as (url, captured, _),
            ):

                def decide(history):
                    request = Request(
                        url + "/v1/decision",
                        data=json.dumps(
                            {
                                "input_format": "openai_responses",
                                "request": {"model": "daedalus", "input": history},
                            }
                        ).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "x-switchyard-session-id": "named-decision",
                        },
                    )
                    with urlopen(request, timeout=5) as response:
                        return json.load(response)

                result = decide("Say OK.")
                self.assertEqual(result["selected"]["target"], target)
                self.assertEqual(
                    [row["target"] for row in result["fallbacks"]],
                    ["xhigh"] if target != "xhigh" else [],
                )
                captured.get(timeout=5)
                result = decide(self.continuation(name="add_memory"))
                self.assertEqual(result["selected"]["target"], target)
                self.assertTrue(captured.empty())
                decide([{"role": "user", "content": "Now consider a new task."}])
                path, _, _ = captured.get(timeout=5)
                self.assertEqual(path, "/v1/systemone")
                self.assertTrue(captured.empty())

    def test_deep_alias_matches_the_deployed_agent_profile(self):
        with self.router() as (url, captured, _config):
            headers = self.request(url, model="daedalus/deep")
            _, body, _ = captured.get(timeout=5)
            self.assertEqual(headers["x-model-router-selected-model"], "xhigh")
            self.assertEqual(body["reasoning_effort"], "xhigh")
