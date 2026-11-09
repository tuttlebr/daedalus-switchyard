"""Native decision contract against the release binary, with local HTTP fixtures.

SWITCHYARD_TEST_BINARY=/path/to/switchyard-server python3 -m unittest discover -s tests -p test_decision_api.py -v
"""

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
BINARY = os.environ.get("SWITCHYARD_TEST_BINARY")
spec = importlib.util.spec_from_file_location("judge_probe", ROOT / "scripts/test_decision_judge.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def verdict():
    return json.loads((ROOT / "tests/fixtures/elephant_choice_response.json").read_text())


class ProbeSchemaTest(unittest.TestCase):
    def test_probe_cli_reads_standalone_default_target(self):
        for path in sorted(ROOT.glob("routes_*.toml")):
            with self.subTest(profile=path.name), patch("sys.argv", ["probe", str(path)]), patch.object(probe, "probe", return_value=[]) as run:
                self.assertEqual(probe.main(), 0)
                self.assertEqual(run.call_args.args[2], tomllib.loads(path.read_text())["routes"]["stage"]["default_target"])
                self.assertEqual(run.call_args.args[0]["classify_trigger"], "every_request")

    def test_optional_metadata_and_clef_response(self):
        answer = verdict()["answers"]["effort"]
        probe.validate_answer_fields(answer)
        del answer["x_label_mass"]
        probe.validate_answer_fields(answer)

    def test_invalid_metadata_and_unknown_fields_fail(self):
        for mass in (-0.1, 1.1, float("nan"), float("inf"), "0.5", True, {}):
            with self.subTest(mass=mass), self.assertRaises(ValueError):
                answer = verdict()["answers"]["effort"]
                answer["x_label_mass"] = mass
                probe.validate_answer_fields(answer)
        answer = verdict()["answers"]["effort"]
        answer["unexpected"] = 1
        with self.assertRaises(ValueError):
            probe.validate_answer_fields(answer)


@unittest.skipUnless(BINARY, "set SWITCHYARD_TEST_BINARY to the built binary")
class DecisionGatewayTest(unittest.TestCase):
    @contextmanager
    def gateway(self, response=None, model="baselabs/elephant-1", timeout_ms=2000, base_suffix="/v1", standalone=False, truncate=False):
        state = {"response": response or verdict(), "judges": [], "providers": [],
                 "models": [{"id": "baselabs/elephant-1", "max_model_len": 4096}], "discoveries": [],
                 "discovery_status": 200, "discovery_delay": 0, "judge_delay": 0,
                 "provider_status": {}, "tokenizations": [], "tokenizer_status": 200,
                 "tokenizer_delay": 0, "usage_offset": 0, "token_scale": 1}

        def prompt_text(body):
            question = body["questions"]["effort"]
            rendered_state = body["state"] if isinstance(body["state"], str) else json.dumps(body["state"], ensure_ascii=False, separators=(",", ":"))
            return (rendered_state
                    + "\n\nQuestion: " + question["instructions"]
                    + "".join(f"\n{chr(65+i)}: {name} - {value}" for i, (name, value) in enumerate(question["criteria"].items()))
                    + "\nAnswer with the letter of one option only.")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                state["discoveries"].append(self.path)
                time.sleep(state["discovery_delay"])
                raw = json.dumps({"object": "list", "data": state["models"]}).encode()
                self.send_response(state["discovery_status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                status = 200
                if self.path == "/v1/tokenize":
                    state["tokenizations"].append(body)
                    time.sleep(state["tokenizer_delay"])
                    # Deliberately dense fixture tokenizer: one token per character.
                    # Its architecture limit differs from the serving limit.
                    count = (len(body["messages"][0]["content"]) + state["token_scale"] - 1) // state["token_scale"]
                    result = {"count": count, "tokens": [], "max_model_len": 262144}
                    status = state["tokenizer_status"]
                elif self.path == "/v1/systemone":
                    state["judges"].append(body)
                    time.sleep(state["judge_delay"])
                    result = state["response"]
                    if truncate:
                        count = (len(prompt_text(body)) + state["token_scale"] - 1) // state["token_scale"]
                        if count >= state["models"][0]["max_model_len"]:
                            status = 400
                        result = {**result, "usage": {"input_tokens": count + state["usage_offset"], "output_tokens": 0}}
                else:
                    state["providers"].append(body)
                    status = state["provider_status"].get(body["model"], 200)
                    result = {
                        "id": "chatcmpl-fixture", "object": "chat.completion", "created": 0,
                        "model": body["model"], "choices": [{"index": 0,
                        "message": {"role": "assistant", "content": "16"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
                    }
                raw = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        process = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "routes.toml"
                source = f'''schema_version = 1
[llm_clients.fixture]
format = "openai_chat"
base_url = "http://127.0.0.1:{upstream.server_port}/v1"
max_retries = 0
'''
                for target in ("low", "high", "xhigh", "max"):
                    source += f'''
[targets.{target}]
id = "{target}"
model = "fixture-{target}"
llm_client = "fixture"
'''
                source += f'''
[routes.stage]
id = "daedalus"
type = "{'decision_api' if standalone else 'composite'}"
{'default_target = "max"' if standalone else ''}
tool_calling = true
[routes.stage.classifier]
type = "decision_api"
model = "{model}"
base_url = "http://127.0.0.1:{upstream.server_port}{base_suffix}"
timeout_ms = {timeout_ms}
{'max_input_tokens = "auto"' if truncate else ''}
{'truncate_input = true' if truncate else ''}
classify_trigger = "{'every_request' if standalone else 'user_turn'}"
instructions = "Choose sufficient effort for the whole task."
uncertain_criteria = "Cannot identify the task."
'''
                for target, threshold in (("low", 0.9), ("high", 0.9), ("xhigh", 0.55), ("max", 0.0)):
                    source += f'''
[routes.stage.classifier.choices.{target}]
target = "{target}"
criteria = "Tasks requiring {target} effort."
min_probability = {threshold}
'''
                if not standalone:
                    source += '''
[routes.stage.stage]
capable_target = "max"
efficient_target = "high"
confidence_threshold = 0.5
'''
                source += '''
[routes.low]
id = "daedalus/low"
type = "passthrough"
target = "low"
'''
                config.write_text(source)
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                with open(Path(directory) / "gateway.log", "w+") as log:
                    process = subprocess.Popen(
                        [BINARY, "--config", str(config), "--host", "127.0.0.1", "--port", str(port)],
                        stdout=log, stderr=subprocess.STDOUT,
                    )
                    url = f"http://127.0.0.1:{port}"
                    for _ in range(100):
                        try:
                            urlopen(url + "/health", timeout=0.2).close()
                            break
                        except OSError:
                            if process.poll() is not None:
                                log.seek(0)
                                self.fail(log.read())
                            time.sleep(0.05)
                    else:
                        self.fail("Gateway did not become ready")
                    yield url, state
        finally:
            if process is not None:
                process.terminate()
                process.wait(timeout=8)
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)

    def request(self, url, session="fixture", model="daedalus", messages=None, endpoint="chat/completions"):
        messages = messages or [{"role": "user", "content": "What is 7 + 9?"}]
        body = {"model": model, "stream": False,
                "input" if endpoint == "responses" else "messages": messages}
        req = Request(url + "/v1/" + endpoint, data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "x-switchyard-session-id": session})
        with urlopen(req, timeout=5) as response:
            result = json.load(response)
            if endpoint == "responses":
                self.assertEqual(result["status"], "completed")
            else:
                self.assertEqual(result["choices"][0]["message"]["content"], "16")
            return response.headers["x-model-router-selected-model"]

    def test_real_elephant_response_selects_low_through_both_apis(self):
        with self.gateway() as (url, state):
            for endpoint in ("chat/completions", "responses"):
                self.assertEqual(self.request(url, endpoint=endpoint), "low")
            self.assertEqual(len(state["judges"]), 2)
            self.assertEqual({x["model"] for x in state["providers"]}, {"fixture-low"})

    def test_auto_follows_loaded_model_changes_through_both_apis(self):
        with self.gateway(model="auto") as (url, state):
            for model in ("baselabs/elephant-1", "Cloudflare/clef-flash", "baselabs/elephant-1"):
                state["models"] = [{"id": model}]
                state["response"]["model"] = model
                if "clef" in model:
                    state["response"]["answers"]["effort"].pop("x_label_mass", None)
                for endpoint in ("chat/completions", "responses"):
                    self.assertEqual(self.request(url, endpoint=endpoint), "low")
                    self.assertEqual(state["judges"][-1]["model"], model)
            self.assertEqual(state["discoveries"], ["/v1/models"] * 6)

    def test_auto_discovery_handles_each_supported_base_url(self):
        for suffix in ("", "/v1", "/v1/systemone"):
            with self.subTest(suffix=suffix), self.gateway(model="auto", base_suffix=suffix) as (url, state):
                self.assertEqual(self.request(url), "low")
                self.assertEqual(state["discoveries"], ["/v1/models"])

    def test_auto_rejects_ambiguous_or_invalid_model_lists(self):
        with self.gateway(model="auto") as (url, state):
            for models in ([], [{"id": "a"}, {"id": "b"}], [{"id": ""}],
                           [{"id": " a "}], [{"id": "a\nb"}], [{"id": "a" * 513}],
                           [{"id": None}], [{"not_id": "a"}]):
                with self.subTest(models=models):
                    state["models"] = models
                    self.assertEqual(self.request(url), "max")
            self.assertEqual(state["judges"], [])

    def test_auto_rejects_response_identity_mismatch_and_discovery_failure(self):
        with self.gateway(model="auto") as (url, state):
            state["response"]["model"] = "wrong-model"
            self.assertEqual(self.request(url), "max")
            state["discovery_status"] = 503
            self.assertEqual(self.request(url), "max")
            self.assertEqual(len(state["judges"]), 1)

    def test_auto_shares_deadline_between_discovery_and_scoring(self):
        with self.gateway(model="auto", timeout_ms=300) as (url, state):
            state["discovery_delay"] = 0.2
            state["judge_delay"] = 0.2
            self.assertEqual(self.request(url), "max")
            self.assertEqual(len(state["judges"]), 1)

    def test_auto_continuations_reuse_target_and_explicit_routes_bypass_discovery(self):
        continuation = [
            {"role": "user", "content": "What is 7 + 9?"},
            {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "calculator", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "16"},
        ]
        with self.gateway(model="auto") as (url, state):
            self.assertEqual(self.request(url), "low")
            state["models"] = [{"id": "Cloudflare/clef-flash"}]
            self.assertEqual(self.request(url, messages=continuation), "low")
            self.assertEqual(self.request(url, model="daedalus/low"), "low")
            self.assertEqual(len(state["discoveries"]), 1)
            self.assertEqual(len(state["judges"]), 1)

    def test_optional_label_mass_is_not_a_routing_threshold(self):
        with self.gateway() as (url, state):
            for mass in (None, 0.0, 0.1, 1.0):
                with self.subTest(mass=mass):
                    answer = state["response"]["answers"]["effort"]
                    answer.pop("x_label_mass", None)
                    if mass is not None:
                        answer["x_label_mass"] = mass
                    self.assertEqual(self.request(url), "low")

    def test_named_choices_thresholds_and_uncertainty(self):
        with self.gateway() as (url, state):
            for choice, probability, expected in (
                ("low", 0.9, "low"), ("low", 0.89, "max"), ("high", 0.96, "high"),
                ("xhigh", 0.55, "xhigh"), ("xhigh", 0.54, "max"),
                ("max", 0.96, "max"), ("uncertain", 0.96, "max"),
            ):
                with self.subTest(choice=choice, probability=probability):
                    answer = state["response"]["answers"]["effort"]
                    answer["choice"] = choice
                    answer["probabilities"] = {k: probability if k == choice else (1 - probability) / 4
                                               for k in ("low", "high", "xhigh", "max", "uncertain")}
                    self.assertEqual(self.request(url), expected)

    def test_invalid_response_still_falls_back(self):
        with self.gateway() as (url, state):
            for change in ("metadata_type", "metadata_range", "unknown_field", "model", "tokens", "distribution"):
                with self.subTest(change=change):
                    state["response"] = verdict()
                    answer = state["response"]["answers"]["effort"]
                    if change == "metadata_type":
                        answer["x_label_mass"] = "0.5"
                    elif change == "metadata_range":
                        answer["x_label_mass"] = 1.1
                    elif change == "unknown_field":
                        answer["unexpected"] = 1
                    elif change == "model":
                        state["response"]["model"] = "wrong-model"
                    elif change == "tokens":
                        state["response"]["usage"]["output_tokens"] = 1
                    else:
                        answer["probabilities"]["low"] = 0.5
                    self.assertEqual(self.request(url), "max")

    def test_session_reuse_reclassification_and_explicit_bypass(self):
        continuation = [
            {"role": "user", "content": "What is 7 + 9?"},
            {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "calculator", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "16"},
        ]
        with self.gateway() as (url, state):
            self.assertEqual(self.request(url), "low")
            self.assertEqual(self.request(url, messages=continuation), "low")
            self.assertEqual(self.request(url, session="unknown", messages=continuation), "max")
            self.assertEqual(self.request(url, model="daedalus/low"), "low")
            self.assertEqual(len(state["judges"]), 1)
            self.assertEqual(self.request(url), "low")
            self.assertEqual(len(state["judges"]), 2)

    @staticmethod
    def choose(state, choice, probability=0.97):
        state["response"] = verdict()
        answer = state["response"]["answers"]["effort"]
        answer["choice"] = choice
        answer["probabilities"] = {
            key: probability if key == choice else (1 - probability) / 4
            for key in ("low", "high", "xhigh", "max", "uncertain")
        }

    @staticmethod
    def continuation(endpoint, result="Healthy tool result: checks passed"):
        user = {"role": "user", "content": "Investigate this problem and validate the fix."}
        if endpoint == "responses":
            return [user,
                    {"type": "function_call", "call_id": "call-1", "name": "check", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "call-1", "output": result}]
        return [user,
                {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function",
                 "function": {"name": "check", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call-1", "content": result}]

    def test_standalone_rejudges_tools_down_and_up_on_both_apis(self):
        for endpoint in ("chat/completions", "responses"):
            with self.subTest(endpoint=endpoint), self.gateway(standalone=True, model="auto") as (url, state):
                for choice in ("xhigh", "low", "high", "low"):
                    self.choose(state, choice)
                    messages = self.continuation(endpoint, f"Latest result for {choice}")
                    self.assertEqual(self.request(url, endpoint=endpoint, messages=messages), choice)
                    self.assertIn(f"Latest result for {choice}", json.dumps(state["judges"][-1]["state"]))
                    self.assertIn(f"Latest result for {choice}", json.dumps(state["providers"][-1]))
                self.assertEqual(len(state["judges"]), 4)
                self.assertEqual(len(state["discoveries"]), 4)
                # A missing/unknown session still classifies tool continuations.
                self.assertEqual(self.request(url, session="", endpoint=endpoint, messages=messages), "low")
                self.assertEqual(len(state["judges"]), 5)
                self.assertEqual(self.request(url, model="daedalus/low", endpoint=endpoint), "low")
                self.assertEqual(len(state["judges"]), 5)

    def test_standalone_failures_do_not_pin_the_next_tool_request(self):
        with self.gateway(standalone=True, timeout_ms=100) as (url, state):
            messages = self.continuation("chat/completions")
            for failure in ("below_gate", "uncertain", "invalid", "timeout"):
                with self.subTest(failure=failure):
                    self.choose(state, "low", 0.89 if failure == "below_gate" else 0.97)
                    if failure == "uncertain":
                        self.choose(state, "uncertain")
                    elif failure == "invalid":
                        state["response"]["model"] = "invalid-model"
                    elif failure == "timeout":
                        state["judge_delay"] = 0.2
                    self.assertEqual(self.request(url, messages=messages), "max")
                    state["judge_delay"] = 0
                    self.choose(state, "low")
                    self.assertEqual(self.request(url, messages=messages), "low")
            self.assertEqual(len(state["judges"]), 8)

    def test_standalone_provider_fallback_only_tries_the_default(self):
        with self.gateway(standalone=True) as (url, state):
            state["provider_status"]["fixture-low"] = 503
            self.assertEqual(self.request(url), "max")
            self.assertEqual([p["model"] for p in state["providers"]], ["fixture-low", "fixture-max"])
            state["providers"].clear()
            state["provider_status"]["fixture-max"] = 503
            with self.assertRaises(HTTPError) as error:
                self.request(url)
            error.exception.close()
            self.assertEqual([p["model"] for p in state["providers"]], ["fixture-low", "fixture-max"])

    def test_truncation_uses_hosted_limit_and_preserves_full_answer_context(self):
        for endpoint in ("chat/completions", "responses"):
            with self.subTest(endpoint=endpoint), self.gateway(standalone=True, truncate=True, model="auto") as (url, state):
                large = "BEGIN latest tool\n" + "結果🙂12345 " * 15000 + "\nEND failed: preserve rollback"
                messages = self.continuation(endpoint, large)
                for limit in (4096, 8192):
                    state["models"][0]["max_model_len"] = limit
                    self.assertEqual(self.request(url, messages=messages, endpoint=endpoint), "low")
                    judge = state["judges"][-1]
                    self.assertTrue(json.loads(judge["state"])["context_truncated"])
                    self.assertIn("BEGIN latest tool", json.dumps(judge, ensure_ascii=False))
                    self.assertIn("END failed: preserve rollback", json.dumps(judge, ensure_ascii=False))
                    self.assertLess(len(state["tokenizations"][-1]["messages"][0]["content"]), limit)
                    self.assertIn(large, json.dumps(state["providers"][-1], ensure_ascii=False).replace('\\n', '\n'))
                self.assertEqual(len(state["judges"]), 2)
                self.assertEqual(len(state["discoveries"]), 2)

    def test_truncation_keeps_short_requests_intact_and_pinned_models_discover_limits(self):
        with self.gateway(standalone=True, truncate=True) as (url, state):
            self.assertEqual(self.request(url), "low")
            self.assertNotIn("context_truncated", state["judges"][0]["state"])
            self.assertEqual(len(state["tokenizations"]), 1)
            self.assertEqual(len(state["discoveries"]), 1)
            self.assertEqual(self.request(url, model="daedalus/low"), "low")
            self.assertEqual(len(state["tokenizations"]), 1)

    def test_truncation_missing_limit_or_tokenizer_fails_before_scoring(self):
        with self.gateway(standalone=True, truncate=True, model="auto") as (url, state):
            for limit in (None, 0, -1, "16384"):
                with self.subTest(limit=limit):
                    state["models"][0]["max_model_len"] = limit
                    self.assertEqual(self.request(url), "max")
            state["models"][0]["max_model_len"] = 4096
            state["tokenizer_status"] = 503
            self.assertEqual(self.request(url), "max")
            self.assertEqual(state["judges"], [])
            state["tokenizer_status"] = 200
            self.assertEqual(self.request(url), "low")

    def test_hosted_tokens_replace_the_old_64k_byte_guard(self):
        with self.gateway(standalone=True, truncate=True) as (url, state):
            state["token_scale"] = 40
            large = "read this " * 9000
            self.assertEqual(self.request(url, messages=[{"role": "user", "content": large}]), "low")
            self.assertNotIn("context_truncated", state["judges"][-1]["state"])
            self.assertIn(large, state["judges"][-1]["state"])

    def test_truncation_detects_changed_prompt_renderer_and_shares_deadline(self):
        with self.gateway(standalone=True, truncate=True, model="auto", timeout_ms=100) as (url, state):
            state["usage_offset"] = 1
            self.assertEqual(self.request(url), "max")
            state["usage_offset"] = 0
            state["tokenizer_delay"] = 0.2
            self.assertEqual(self.request(url), "max")
            self.assertEqual(len(state["judges"]), 1)

    def test_truncation_preserves_latest_user_outside_recent_tool_window(self):
        with self.gateway(standalone=True, truncate=True) as (url, state):
            messages = [{"role": "user", "content": "Original task"},
                        {"role": "user", "content": "CURRENT TASK: validate privacy and rollback"}]
            for i in range(8):
                messages.extend(self.continuation("chat/completions", "result " * 1000)[1:])
            self.assertEqual(self.request(url, messages=messages), "low")
            self.assertIn("CURRENT TASK: validate privacy and rollback", json.dumps(state["judges"][-1]))

    def test_truncation_bounds_system_instructions_and_accepts_last_input_position(self):
        with self.gateway(standalone=True, truncate=True) as (url, state):
            self.assertEqual(self.request(url), "low")
            count = len(state["tokenizations"][-1]["messages"][0]["content"])
            state["models"][0]["max_model_len"] = count + 1
            self.assertEqual(self.request(url), "low")
            self.assertNotIn("context_truncated", state["judges"][-1]["state"])
            state["models"][0]["max_model_len"] = 4096
            system = "SYSTEM START " + "instruction " * 20000 + " SYSTEM END"
            messages = [{"role": "system", "content": system}, {"role": "user", "content": "Calculate 7 + 9."}]
            self.assertEqual(self.request(url, messages=messages), "low")
            self.assertIn("SYSTEM START", json.dumps(state["judges"][-1]))
            self.assertIn("SYSTEM END", json.dumps(state["judges"][-1]))
            self.assertIn(system, json.dumps(state["providers"][-1]))


if __name__ == "__main__":
    unittest.main()
