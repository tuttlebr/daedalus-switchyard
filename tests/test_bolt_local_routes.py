"""Resolve fixed local routes and exercise their Chat/Responses wire contract."""

import json
import os
import re
import tomllib
import unittest
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ("routes_openai.toml", "routes_custom.toml")
ALIASES = ("daedalus/local", "daedalus/none", "daedalus/hindsight-local")
BOLT_MODEL = "nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB"
BOLT_URL = (
    "http://nemotron35bolt-vllm-gb10-frontend.daedalus-dynamo.svc.cluster.local:8000/v1"
)


def resolved(config, alias):
    routes = [route for route in config["routes"].values() if route["id"] == alias]
    if len(routes) != 1 or routes[0]["type"] != "passthrough":
        raise AssertionError("local aliases require one fixed passthrough route")
    target = config["targets"][routes[0]["target"]]
    return routes[0], target, config["llm_clients"][target["llm_client"]]


class BoltLocalRoutesTest(unittest.TestCase):
    def test_all_local_aliases_use_one_bolt_destination_in_both_profiles(self):
        for profile in PROFILES:
            config = tomllib.loads((ROOT / profile).read_text())
            for alias in ALIASES:
                with self.subTest(profile=profile, alias=alias):
                    route, target, client = resolved(config, alias)
                    self.assertNotIn("classifier", route)
                    self.assertNotIn("capable_target", route)
                    self.assertEqual(target["model"], BOLT_MODEL)
                    self.assertEqual(target["llm_client"], "local")
                    self.assertEqual(client["base_url"], BOLT_URL)
                    self.assertEqual(client["format"], "openai_chat")
                    self.assertEqual(client["api_key_env"], "LOCAL_API_KEY")

    def test_local_reasoning_controls_are_typed_and_nonthinking_aliases_keep_defaults(
        self,
    ):
        for profile in PROFILES:
            config = tomllib.loads((ROOT / profile).read_text())
            with self.subTest(profile=profile):
                _, local, _ = resolved(config, "daedalus/local")
                self.assertEqual(
                    local["extra_body"],
                    {
                        "chat_template_kwargs": {
                            "enable_thinking": True,
                            "medium_effort": True,
                        },
                        "nvext": {"max_thinking_tokens": 256},
                    },
                )
                self.assertIs(
                    type(local["extra_body"]["nvext"]["max_thinking_tokens"]), int
                )
                self.assertNotIn("reasoning_effort", local["extra_body"])
                for alias in ("daedalus/none", "daedalus/hindsight-local"):
                    _, target, _ = resolved(config, alias)
                    self.assertEqual(
                        target["extra_body"],
                        {"chat_template_kwargs": {"enable_thinking": False}},
                    )

    def test_none_is_local_even_when_openai_instant_target_changes(self):
        config = tomllib.loads((ROOT / "routes_openai.toml").read_text())
        config["targets"]["instant"]["model"] = "unrelated-cloud-model"
        route, target, client = resolved(config, "daedalus/none")
        self.assertEqual(route["target"], "none")
        self.assertEqual(target["model"], BOLT_MODEL)
        self.assertEqual(client["base_url"], BOLT_URL)


@unittest.skipUnless(
    os.environ.get("SWITCHYARD_TEST_BINARY"),
    "set SWITCHYARD_TEST_BINARY to actual built binary",
)
class BoltLocalWireTest(unittest.TestCase):
    def router(self):
        from test_fireworks_chat_transport import FireworksChatTransportTest

        def preserve_local_none(source):
            # Restore /none after the shared legacy cloud fixture rewrites it.
            # The fixture subsequently replaces the local client with loopback.
            return re.sub(
                r'(?ms)(^\[routes\.none\]\n.*?target = ")low(")', r"\g<1>none\2", source
            )

        return FireworksChatTransportTest().router(source_transform=preserve_local_none)

    def request(self, base, alias, api="chat", **extra):
        body = {"model": alias, "stream": True}
        if api == "chat":
            body.update(
                messages=[{"role": "user", "content": "Synthetic transport marker."}],
                max_tokens=8192,
            )
            path = "/v1/chat/completions"
        else:
            body.update(
                input=[{"role": "user", "content": "Synthetic transport marker."}],
                max_output_tokens=8192,
            )
            path = "/v1/responses"
        body.update(extra)
        request = Request(
            base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            response.read()

    def test_local_chat_and_responses_bridge_preserve_bolt_defaults_after_encoding(
        self,
    ):
        with self.router() as (base, captured, _):
            for api in ("chat", "responses"):
                with self.subTest(api=api):
                    self.request(base, "daedalus/local", api)
                    path, body, _ = captured.get(timeout=3)
                    self.assertEqual(path, "/v1/chat/completions")
                    self.assertEqual(body["model"], BOLT_MODEL)
                    self.assertEqual(
                        body["chat_template_kwargs"],
                        {"enable_thinking": True, "medium_effort": True},
                    )
                    self.assertEqual(body["nvext"], {"max_thinking_tokens": 256})
                    self.assertIs(type(body["nvext"]["max_thinking_tokens"]), int)
                    self.assertNotIn("reasoning_effort", body)

    def test_nonthinking_chat_and_responses_aliases_share_bolt_without_budget(self):
        with self.router() as (base, captured, _):
            for api in ("chat", "responses"):
                for alias in ("daedalus/none", "daedalus/hindsight-local"):
                    with self.subTest(api=api, alias=alias):
                        self.request(base, alias, api)
                        path, body, _ = captured.get(timeout=3)
                        self.assertEqual(path, "/v1/chat/completions")
                        self.assertEqual(body["model"], BOLT_MODEL)
                        self.assertEqual(
                            body["chat_template_kwargs"], {"enable_thinking": False}
                        )
                        self.assertNotIn("nvext", body)
                        self.assertNotIn("reasoning_effort", body)

    def test_trusted_caller_keeps_whole_namespace_precedence(self):
        with self.router() as (base, captured, _):
            self.request(base, "daedalus/local", nvext={"max_thinking_tokens": 1024})
            _, body, _ = captured.get(timeout=3)
            self.assertEqual(body["nvext"], {"max_thinking_tokens": 1024})

    def test_none_preserves_synthetic_image_input_on_local_chat_path(self):
        image_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        content = [
            {"type": "text", "text": "Synthetic image transport only."},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
        with self.router() as (base, captured, _):
            self.request(
                base, "daedalus/none", messages=[{"role": "user", "content": content}]
            )
            _, body, _ = captured.get(timeout=3)
            self.assertEqual(body["model"], BOLT_MODEL)
            self.assertEqual(body["messages"][-1]["content"], content)
            self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})


if __name__ == "__main__":
    unittest.main()
