"""Deployment invariants; the built-binary wire tests live alongside these."""

import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FireworksRoutesTest(unittest.TestCase):
    def test_agent_profiles_resolve_to_distinct_target_identities(self):
        config = tomllib.loads((ROOT / "routes_custom.toml").read_text())
        targets = config["targets"]
        self.assertEqual(len({t["id"] for t in targets.values()}), len(targets))
        routes = {r["id"]: r for r in config["routes"].values()}
        for alias in ("daedalus", "daedalus/deep", "daedalus/xhigh", "daedalus/max"):
            self.assertIn(alias, routes)
        for route in routes.values():
            for key in ("target", "default_target", "capable_target", "efficient_target"):
                if key in route:
                    self.assertIn(route[key], targets)
            if route["type"] == "composite":
                if route["classifier"].get("type", "llm") == "llm":
                    self.assertIn(route["classifier"]["target"], targets)
                for key in ("capable_target", "efficient_target"):
                    self.assertIn(route["stage"][key], targets)
                for choice in route["classifier"].get("choices", {}).values():
                    self.assertIn(choice["target"], targets)

    def test_automatic_profiles_use_standalone_every_request_decisions(self):
        for name in ("routes_custom.toml", "routes_openai.toml"):
            with self.subTest(profile=name):
                config = tomllib.loads((ROOT / name).read_text())
                route = config["routes"]["stage"]
                self.assertEqual(route["id"], "daedalus")
                self.assertEqual(route["type"], "decision_api")
                self.assertEqual(route["default_target"], "xhigh")
                self.assertNotIn("stage", route)
                self.assertEqual(route["classifier"]["classify_trigger"], "every_request")
                self.assertEqual(route["classifier"]["max_input_tokens"], "auto")
                self.assertTrue(route["classifier"]["truncate_input"])
                self.assertEqual(set(route["classifier"]["choices"]), {
                    "local", "instant", "low", "medium", "high", "xhigh", "max",
                })
                for choice in route["classifier"]["choices"].values():
                    self.assertIn(choice["target"], config["targets"])

    def test_no_shared_session_identity_or_responses_only_fireworks_defaults(self):
        config = tomllib.loads((ROOT / "routes_custom.toml").read_text())
        for client in config["llm_clients"].values():
            self.assertFalse(
                any("session" in h.lower() for h in client.get("extra_headers", {}))
            )
        self.assertEqual(config["llm_clients"]["fireworks"]["format"], "openai_chat")
        for target in config["targets"].values():
            if target["llm_client"] == "fireworks":
                self.assertNotIn("truncation", target.get("extra_body", {}))
                self.assertNotEqual(target["extra_body"]["reasoning_effort"], "none")

    def test_hindsight_alias_has_one_local_destination_and_nonthinking_default(self):
        for name in ("routes_openai.toml", "routes_custom.toml"):
            with self.subTest(profile=name):
                config = tomllib.loads((ROOT / name).read_text())
                route = config["routes"]["hindsight_local"]
                self.assertEqual(
                    route,
                    {
                        "id": "daedalus/hindsight-local",
                        "type": "passthrough",
                        "target": "hindsight_local",
                        "tool_calling": True,
                        "reasoning": False,
                    },
                )
                target = config["targets"][route["target"]]
                self.assertEqual(target["model"], config["targets"]["local"]["model"])
                self.assertEqual(target["llm_client"], "local")
                self.assertEqual(
                    target["extra_body"],
                    {
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )
                self.assertEqual(
                    config["targets"]["local"]["extra_body"],
                    {
                        "chat_template_kwargs": {
                            "enable_thinking": True,
                            "medium_effort": True,
                        },
                        "nvext": {"max_thinking_tokens": 256},
                    },
                )
                self.assertEqual(config["routes"]["none"]["target"], "none")

    def test_optional_native_credential_is_discovered_for_deployment(self):
        import importlib.util
        import tempfile

        spec = importlib.util.spec_from_file_location(
            "key_envs", ROOT / "scripts/list_api_key_envs.py"
        )
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routes.toml"
            source = (ROOT / "routes_custom.toml").read_text()
            path.write_text(
                source.replace(
                    '[routes.stage.classifier]\ntype = "decision_api"',
                    '[routes.stage.classifier]\ntype = "decision_api"\napi_key_env = "DECISION_API_KEY"',
                )
            )
            self.assertEqual(
                helper.api_key_environment_names(path),
                sorted({"DECISION_API_KEY"} | {
                    client["api_key_env"] for client in tomllib.loads(source)["llm_clients"].values()
                    if "api_key_env" in client
                }),
            )
            path.write_text(
                path.read_text().replace(
                    'api_key_env = "DECISION_API_KEY"', 'api_key_env = "invalid-key"'
                )
            )
            with self.assertRaises(ValueError):
                helper.api_key_environment_names(path)


if __name__ == "__main__":
    unittest.main()
