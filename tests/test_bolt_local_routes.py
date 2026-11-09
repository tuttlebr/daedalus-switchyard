"""Check retired aliases and synthetic local Chat/Responses compatibility."""

import json
import os
import tomllib
import unittest
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
PROFILES = tuple(path.name for path in sorted(ROOT.glob("routes_*.toml")))
BOLT_MODEL = "nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB"


class BoltLocalRoutesTest(unittest.TestCase):
    def test_retired_none_route_is_absent_from_all_providers(self):
        for profile in PROFILES:
            config = tomllib.loads((ROOT / profile).read_text())
            with self.subTest(profile=profile):
                self.assertNotIn("none", config["targets"])
                self.assertNotIn("daedalus/none", {r["id"] for r in config["routes"].values()})


@unittest.skipUnless(
    os.environ.get("SWITCHYARD_TEST_BINARY"),
    "set SWITCHYARD_TEST_BINARY to actual built binary",
)
class BoltLocalWireTest(unittest.TestCase):
    def router(self):
        from test_fireworks_chat_transport import FireworksChatTransportTest

        def bolt_defaults(source):
            return source.replace(
                'extra_body = { reasoning_effort = "medium", chat_template_kwargs = { enable_thinking = true } }',
                'extra_body = { chat_template_kwargs = { enable_thinking = true, medium_effort = true }, nvext = { max_thinking_tokens = 256 } }',
            )

        return FireworksChatTransportTest().router(source_transform=bolt_defaults)

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
                for alias in ("daedalus/hindsight-local",):
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

    def test_hindsight_preserves_synthetic_image_input_on_local_chat_path(self):
        image_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        content = [
            {"type": "text", "text": "Synthetic image transport only."},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
        with self.router() as (base, captured, _):
            self.request(
                base, "daedalus/hindsight-local", messages=[{"role": "user", "content": content}]
            )
            _, body, _ = captured.get(timeout=3)
            self.assertEqual(body["model"], BOLT_MODEL)
            self.assertEqual(body["messages"][-1]["content"], content)
            self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})


if __name__ == "__main__":
    unittest.main()
