"""Deployment invariants; the built-binary wire tests live alongside these."""

import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FireworksRoutesTest(unittest.TestCase):
    def test_public_routes_and_all_target_references_resolve(self):
        for name in ("routes_custom.toml", "routes_openai.toml"):
            with self.subTest(profile=name):
                config = tomllib.loads((ROOT / name).read_text())
                targets = config["targets"]
                categories = {"instant", "low", "medium", "high", "xhigh", "max"}
                tiers = {"instant", "low", "high", "max"} if name == "routes_custom.toml" else categories
                self.assertEqual(set(targets), tiers)
                self.assertEqual(len({t["id"] for t in targets.values()}), len(targets))
                routes = {r["id"]: r for r in config["routes"].values()}
                self.assertEqual(set(routes), {"daedalus"} | {"daedalus/" + t for t in tiers})
                for tier in tiers:
                    route = routes["daedalus/" + tier]
                    self.assertEqual(route["type"], "passthrough")
                    self.assertEqual(route["target"], tier)
                    self.assertTrue(route["tool_calling"])
                    self.assertTrue(route["vision"])
                route = routes["daedalus"]
                self.assertEqual(route["type"], "decision_api")
                self.assertIn(route["default_target"], targets)
                self.assertNotIn("stage", route)
                classifier = route["classifier"]
                self.assertEqual(classifier["classify_trigger"], "every_request")
                self.assertEqual(classifier["max_input_tokens"], "auto")
                self.assertTrue(classifier["truncate_input"])
                self.assertEqual(set(classifier["choices"]), categories)
                for choice in classifier["choices"].values():
                    self.assertIn(choice["target"], targets)
                    self.assertTrue(0 <= choice["min_probability"] <= 1)

    def test_custom_profiles_are_distinct_and_match_provider_effort(self):
        config = tomllib.loads((ROOT / "routes_custom.toml").read_text())
        expected = {"instant": "none", "low": "low", "high": "high", "max": "max"}
        self.assertEqual({name: target["extra_body"]["reasoning_effort"]
                          for name, target in config["targets"].items()}, expected)
        choices = config["routes"]["stage"]["classifier"]["choices"]
        self.assertEqual({name: choice["target"] for name, choice in choices.items()}, {
            "instant": "instant", "low": "low", "medium": "high", "high": "high",
            "xhigh": "high", "max": "max",
        })
        self.assertEqual(config["routes"]["stage"]["default_target"], "high")

    def test_custom_targets_preserve_serverless_capabilities(self):
        import json

        config = tomllib.loads((ROOT / "routes_custom.toml").read_text())
        catalog = json.loads((ROOT / "docs/investigations/routes-optimization-2026-11-06/serverless-catalog.json").read_text())
        models = {m["id"]: m for m in catalog["data"]}
        self.assertEqual(set(config["llm_clients"]), {"fireworks"})
        client = config["llm_clients"]["fireworks"]
        self.assertEqual(client["base_url"], "https://api.fireworks.ai/inference/v1")
        self.assertEqual(client["format"], "openai_chat")
        self.assertEqual(client["extra_headers"], {
            "x-multi-turn-session-id": "${GLOBAL_SESSION_ID}",
            "x-session-affinity": "${GLOBAL_SESSION_ID}",
        })
        for name, target in config["targets"].items():
            with self.subTest(target=name):
                self.assertEqual(target["llm_client"], "fireworks")
                model = models[target["model"]]
                self.assertTrue(model["supports_tools"])
                self.assertTrue(model["supports_image_input"])
                self.assertGreaterEqual(model["context_length"], config["routes"][name]["context_window"])
                self.assertNotIn("truncation", target["extra_body"])
                self.assertEqual(target["extra_body"]["reasoning_history"], "interleaved")
                self.assertNotIn("stream", target["extra_body"])
                # Muse Open documents four distinct tiers, not max or none.
                if target["model"].endswith("/muse-spark-open"):
                    self.assertIn(target["extra_body"]["reasoning_effort"], {"low", "medium", "high", "xhigh"})

    def test_classifier_does_not_advertise_stale_models_or_fallback(self):
        config = tomllib.loads((ROOT / "routes_custom.toml").read_text())
        route = config["routes"]["stage"]
        classifier = route["classifier"]
        self.assertIn("= " + route["default_target"], classifier["uncertain_criteria"])
        self.assertNotIn("accounts/fireworks/", classifier["instructions"])
        self.assertNotIn("US router", str(classifier))
        self.assertNotIn("router at high", str(classifier))

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
