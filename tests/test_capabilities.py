"""Real SSE parser and roundtrip gate fault coverage, without provider credentials."""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "capabilities", Path(__file__).parents[1] / "scripts/check_capabilities.py"
)
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


@contextmanager
def model_catalog(payload, status=200):
    requests = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            client={"format": "openai_chat", "base_url": f"http://127.0.0.1:{server.server_port}/v1"},
            requests=requests,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ContextWindows(unittest.TestCase):
    def test_provider_fields_and_serving_cap(self):
        for field in ("context_window", "context_length", "max_model_len", "max_context_length"):
            with self.subTest(field=field), model_catalog(
                {"data": [{"id": "fixture", field: 131072}]}
            ) as peer:
                with patch.dict(os.environ, {"FIXTURE_KEY": "synthetic"}):
                    result = probe.discover_context_window(
                        {**peer.client, "api_key_env": "FIXTURE_KEY"}, "fixture", timeout=2
                    )
                self.assertEqual(result.tokens, 131072)
                self.assertEqual(result.field, field)
                self.assertEqual(peer.requests, [("/v1/models", "Bearer synthetic")])
        with model_catalog({"data": [{
            "id": "fixture", "context_window": None, "context_length": 1048576, "max_model_len": 32768,
        }]}) as peer:
            result = probe.discover_context_window(peer.client, "fixture", timeout=2)
            self.assertEqual(result.tokens, 32768)
            self.assertEqual(result.field, "max_model_len")

    def test_missing_metadata_is_unknown_and_model_match_is_exact(self):
        for payload, status, reason in (
            ({"data": [{"id": "fixture", "context_window": None}]}, 200, "context_window_not_reported"),
            ({"data": [{"id": "other/fixture", "context_length": 131072}]}, 200, "model_not_listed"),
            ({}, 404, "models_endpoint_unavailable"),
            ({}, 405, "models_endpoint_unavailable"),
            ({}, 501, "models_endpoint_unavailable"),
        ):
            with self.subTest(reason=reason, status=status), model_catalog(payload, status) as peer:
                result = probe.discover_context_window(peer.client, "fixture", timeout=2)
                self.assertIsNone(result.tokens)
                self.assertEqual(result.reason, reason)

    def test_malformed_metadata_and_auth_errors_are_not_silently_skipped(self):
        for value in (0, -1, True, "131072", 131072.5):
            with self.subTest(value=value), model_catalog(
                {"data": [{"id": "fixture", "context_length": value}]}
            ) as peer:
                with self.assertRaisesRegex(probe.CheckFailed, "invalid_context_window_metadata"):
                    probe.discover_context_window(peer.client, "fixture", timeout=2)
        for payload in ([], {}, {"data": ["fixture"]}):
            with self.subTest(payload=payload), model_catalog(payload) as peer:
                with self.assertRaisesRegex(probe.CheckFailed, "invalid_models_metadata"):
                    probe.discover_context_window(peer.client, "fixture", timeout=2)
        with model_catalog({}, 401) as peer:
            with self.assertRaises(probe.HTTPError) as failure:
                probe.discover_context_window(peer.client, "fixture", timeout=2)
            self.assertEqual(failure.exception.code, 401)
            failure.exception.close()

    def test_route_requirements_include_choices_fallbacks_and_subagents(self):
        config = {
            "targets": {name: {} for name in ("strong", "weak", "local", "worker", "judge")},
            "routes": {
                "fixed": {"type": "passthrough", "target": "strong", "context_window": 65536},
                "composite": {
                    "type": "composite", "context_window": 32768,
                    "stage": {"capable_target": "strong", "efficient_target": "weak"},
                    "classifier": {"type": "decision_api", "choices": {"private": {"target": "local"}}},
                    "subagents": {
                        "type": "llm_classifier", "mode": "custom",
                        "models": {"any": ["worker"], "judge": ["judge"]},
                    },
                },
            },
        }
        self.assertEqual(probe.context_requirements(config), {
            "strong": 65536, "weak": 32768, "local": 32768, "worker": 32768,
        })
        for value in (0, -1, True, "32768", 2**32):
            config["routes"]["fixed"]["context_window"] = value
            with self.subTest(value=value), self.assertRaisesRegex(probe.CheckFailed, "invalid_context_window"):
                probe.context_requirements(config)

    def test_gate_reports_limits_and_enforces_declarations_before_generation(self):
        for advertised, required, expected_error in (
            (32768, None, None),
            (32768, 16384, None),
            (32768, 32768, None),
            (32768, 65536, "context_window_too_small"),
            (None, None, None),
            (None, 32768, "context_window_unknown"),
        ):
            with self.subTest(advertised=advertised, required=required), model_catalog(
                {"data": [{"id": "fixture", "context_length": advertised}]}
            ) as peer, tempfile.TemporaryDirectory() as directory:
                routes = Path(directory) / "routes.toml"
                report = Path(directory) / "report.json"
                routes.write_text(
                    '[llm_clients.fixture]\nformat="openai_responses"\n'
                    f'base_url="{peer.client["base_url"]}"\n'
                    '[targets.low]\nmodel="fixture"\nllm_client="fixture"\n'
                    '[routes.low]\ntype="passthrough"\ntarget="low"\n'
                    + (f'context_window={required}\n' if required is not None else "")
                )
                with patch.object(sys, "argv", ["probe", str(routes), "--report", str(report)]), patch.object(
                    probe, "check_target", return_value={"checks": ["text"]}
                ) as generation, redirect_stdout(io.StringIO()):
                    exit_code = probe.main()
                data = json.loads(report.read_text())
                row = data["targets"][0]
                self.assertEqual(exit_code, 1 if expected_error else 0)
                self.assertEqual(data["passed"], not bool(expected_error))
                self.assertEqual(row["context_window"], advertised)
                self.assertEqual(row["required_context_window"], required)
                self.assertEqual(row["context_window_status"], "unknown" if advertised is None else "reported")
                self.assertEqual(row.get("error"), expected_error)
                self.assertEqual(generation.call_count, 0 if expected_error else 1)
                if not expected_error:
                    self.assertEqual("context_window" in row["checks"], advertised is not None)

    def test_duplicate_targets_cannot_hide_a_stricter_route_requirement(self):
        with model_catalog({"data": [{"id": "fixture", "context_length": 32768}]}) as peer:
            with tempfile.TemporaryDirectory() as directory:
                routes = Path(directory) / "routes.toml"
                report = Path(directory) / "report.json"
                routes.write_text(
                    '[llm_clients.fixture]\nformat="openai_chat"\n'
                    f'base_url="{peer.client["base_url"]}"\n'
                    '[targets.low]\nmodel="fixture"\nllm_client="fixture"\n'
                    '[targets.high]\nmodel="fixture"\nllm_client="fixture"\n'
                    '[routes.low]\ntype="passthrough"\ntarget="low"\ncontext_window=16384\n'
                    '[routes.high]\ntype="passthrough"\ntarget="high"\ncontext_window=65536\n'
                )
                with patch.object(sys, "argv", ["probe", str(routes), "--report", str(report)]), patch.object(
                    probe, "check_target", return_value={"checks": ["text"]}
                ), redirect_stdout(io.StringIO()):
                    self.assertEqual(probe.main(), 1)
                rows = json.loads(report.read_text())["targets"]
                self.assertEqual([row["passed"] for row in rows], [True, False])
                self.assertEqual(rows[1]["error"], "context_window_too_small")
                self.assertEqual(len(peer.requests), 1)


class StreamDiagnostics(unittest.TestCase):
    def response(self, deltas, request_id="req_diagnostic", finish_reasons=None):
        response = io.BytesIO()
        response.headers = Message()
        response.headers["Content-Type"] = "text/event-stream"
        response.headers["X-Request-Id"] = request_id
        for index, delta in enumerate(deltas):
            event = {
                "model": "fixture",
                "choices": [{
                    "index": 0, "delta": delta,
                    "finish_reason": finish_reasons[index] if finish_reasons else "stop",
                }],
            }
            response.write(f"data: {json.dumps(event)}\n\n".encode())
        response.write(b"data: [DONE]\n\n")
        response.seek(0)
        return response

    def test_console_and_report_explain_failure_without_logging_content_or_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            routes = Path(directory) / "routes.toml"
            report = Path(directory) / "report.json"
            routes.write_text(
                '[llm_clients.fixture]\nformat="openai_chat"\nbase_url="http://fixture"\n'
                'api_key_env="FIXTURE_KEY"\n[targets.low]\nmodel="fixture"\nllm_client="fixture"\n'
            )
            with (
                patch.object(sys, "argv", ["probe", str(routes), "--report", str(report)]),
                patch.dict(os.environ, {"FIXTURE_KEY": "sensitive-credential"}),
                patch.object(probe, "discover_context_window", return_value=probe.ContextWindow()),
                patch.object(probe, "urlopen", return_value=self.response(
                    [{"content": "CANARY_OK"}, {"content": "private-output"}],
                    finish_reasons=["stop", "sensitive-provider-value"],
                )),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(probe.main(), 1)
            row = json.loads(output.getvalue())
            self.assertEqual(row, json.loads(report.read_text())["targets"][0])
            self.assertEqual(row["error"], "duplicate_terminal")
            self.assertIn("additional text", row["error_message"])
            self.assertEqual(row["error_details"], {
                "stage": "text", "request_id": "req_diagnostic", "event_index": 2,
                "previous_finish_reason": "stop", "finish_reason": "other",
                "content_chars": 14, "tool_call_parts": 0, "done_seen": False,
                "violation": "content_after_finish",
            })
            for sensitive in ("CANARY_OK", "private-output", "sensitive-credential", "sensitive-provider-value"):
                self.assertNotIn(sensitive, output.getvalue() + report.read_text())

    def test_request_ids_are_bounded_and_do_not_echo_credentials(self):
        for value in ("sensitive-credential", "req-sensitive-credential", "x" * 201, "bad\nheader"):
            with self.subTest(value=value), patch.object(probe, "urlopen", return_value=self.response(
                [{"content": "CANARY_OK"}, {"content": "extra"}], request_id=value,
            )), self.assertRaises(probe.CheckFailed) as caught:
                probe.exchange("http://fixture", "sensitive-credential", {}, "openai_chat", 2)
            self.assertIsNone(caught.exception.details.request_id)
            self.assertNotIn(value, json.dumps(asdict(caught.exception.details)))

    def test_malformed_deltas_and_tool_fragments_still_fail(self):
        for delta, code in (
            ([], "invalid_delta"),
            ({"content": 5}, "invalid_delta"),
            ({"tool_calls": [None]}, "invalid_tool_calls"),
            ({"tool_calls": [{"index": True}]}, "invalid_tool_calls"),
            ({"tool_calls": [{"index": 0, "function": []}]}, "invalid_tool_calls"),
            ({"tool_calls": [{"index": 0, "function": {"arguments": 1}}]}, "invalid_tool_calls"),
        ):
            with self.subTest(delta=delta), patch.object(probe, "urlopen", return_value=self.response(
                [{"content": "CANARY_OK"}, delta],
            )), self.assertRaises(probe.CheckFailed) as caught:
                probe.exchange("http://fixture", "", {}, "openai_chat", 2)
            self.assertEqual(str(caught.exception), code)


class Capabilities(unittest.TestCase):
    def exercise(
        self, api, fault=None, *, nullable_tool_calls=False,
        repeat_terminal=False, tool_finish_reason="tool_calls", terminal_variant=None,
        terminal_tool_parts=None, fault_at_request=None,
    ):
        seen = []

        class Peer(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(body)
                active_fault = fault if fault_at_request in (None, len(seen)) else None
                if active_fault == "unavailable":
                    self.send_response(404)
                    self.send_header("X-Request-Id", "req_fixture")
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
                if active_fault == "invented_result" and final:
                    text = "guessed"
                args = json.dumps({"key": "wrong" if active_fault == "arguments" else "probe"})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("X-Request-Id", "req_fixture")
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
                    if active_fault == "invalid_tool_calls":
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
                                        if active_fault == "post_terminal_content"
                                        else {"content": None, "tool_calls": None}
                                        if nullable_tool_calls
                                        else {}
                                    ),
                                    "finish_reason": (
                                        "length"
                                        if active_fault == "conflicting_terminal"
                                        else "tool_calls" if calling else "stop"
                                    ),
                                }
                            ],
                        }
                        if terminal_variant == "null_delta":
                            terminal["choices"][0]["delta"] = None
                        elif terminal_variant == "empty_tool_fragment" and calling:
                            terminal["choices"][0]["delta"] = {
                                "tool_calls": [{"index": 0, "type": "function", "function": {
                                    "name": "", "arguments": "",
                                }}],
                            }
                        elif terminal_variant == "usage_only":
                            terminal["choices"] = []
                            terminal["usage"] = {"total_tokens": 12}
                        if terminal_tool_parts is not None and calling:
                            terminal["choices"][0]["delta"] = {"tool_calls": terminal_tool_parts}
                        payload += f"data: {json.dumps(terminal)}\n\n".encode()
                    if active_fault != "missing_terminal":
                        payload += b"data: [DONE]\n\n"
                    self.wfile.write(payload)
                elif active_fault != "missing_terminal":
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

    def test_harmless_terminal_variants_complete_both_tool_roundtrips(self):
        for variant in ("null_delta", "empty_tool_fragment", "usage_only"):
            with self.subTest(variant=variant):
                self.exercise("openai_chat", repeat_terminal=True, terminal_variant=variant)

        for part in ({"index": 0}, {"index": 0, "id": None, "function": None}):
            with self.subTest(part=part):
                self.exercise("openai_chat", repeat_terminal=True, terminal_tool_parts=[part])

    def test_terminal_tolerance_never_discards_new_tool_data(self):
        for part in (
            {"index": 1},
            {"index": 0, "id": "call_probe"},
            {"index": 0, "function": {"name": "probe_read"}},
            {"index": 0, "function": {"arguments": "}"}},
            {"index": 0, "type": "custom"},
        ):
            with self.subTest(part=part), self.assertRaises(probe.CheckFailed) as caught:
                self.exercise("openai_chat", repeat_terminal=True, terminal_tool_parts=[part])
            self.assertEqual(str(caught.exception), "duplicate_terminal")
            self.assertEqual(caught.exception.details.violation, "tool_calls_after_finish")
            self.assertEqual(caught.exception.details.stage, "automatic_tool_selection")
            self.assertEqual(caught.exception.details.request_id, "req_fixture")

    def test_empty_fragments_do_not_hide_invalid_arguments_or_incomplete_streams(self):
        for fault, code in (("arguments", "invalid_arguments"), ("missing_terminal", "missing_terminal")):
            with self.subTest(fault=fault), self.assertRaises(probe.CheckFailed) as caught:
                self.exercise(
                    "openai_chat", fault, repeat_terminal=True,
                    terminal_variant="empty_tool_fragment", fault_at_request=2,
                )
            self.assertEqual(str(caught.exception), code)

    def test_failure_diagnostics_identify_each_stage_for_both_apis(self):
        stages = ("text", "automatic_tool_selection", "automatic_tool_result",
                  "forced_tool_selection", "forced_tool_result")
        for api in ("openai_chat", "openai_responses"):
            for index, stage in enumerate(stages, 1):
                with self.subTest(api=api, stage=stage), self.assertRaises(probe.CheckFailed) as caught:
                    self.exercise(api, "unavailable", fault_at_request=index)
                self.assertEqual(str(caught.exception), "http_404")
                self.assertEqual(caught.exception.details.stage, stage)
                self.assertEqual(caught.exception.details.request_id, "req_fixture")

        for api in ("openai_chat", "openai_responses"):
            with self.subTest(api=api), self.assertRaises(probe.CheckFailed) as caught:
                self.exercise(api, "invented_result", fault_at_request=5)
            self.assertEqual(str(caught.exception), "result_not_consumed")
            self.assertEqual(caught.exception.details.stage, "forced_tool_result")
            self.assertEqual(caught.exception.details.request_id, "req_fixture")

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
        with model_catalog({"data": [{"id": "fixture", "context_length": 32768}]}) as peer:
            self.failed_deployment(peer.client["base_url"], "context_window_too_small", "context_window=65536\n")
        self.failed_deployment("http://127.0.0.1:1/v1", "URLError")

    def failed_deployment(self, base_url, error, declaration=""):
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
                f'[llm_clients.fixture]\nformat="openai_chat"\nbase_url="{base_url}"\n'
                'api_key_env="FIXTURE_KEY"\n[targets.low]\nmodel="fixture"\nllm_client="fixture"\n'
                '[routes.low]\ntype="passthrough"\ntarget="low"\n' + declaration
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
            self.assertIn(error, result.stdout)
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
