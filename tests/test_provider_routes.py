"""Shared routing contract for every discovered provider configuration."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

from scripts.render_routes import dumps, load_config

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = {"instant", "low", "high", "max"}


def providers():
    paths = sorted(ROOT.glob("routes_*.toml"))
    if not paths:
        raise AssertionError("No provider route configurations discovered")
    return [(path.name, load_config(path)) for path in paths]


class ProviderRoutesTest(unittest.TestCase):
    def test_all_aliases_choices_fallbacks_and_clients_resolve(self):
        for name, config in providers():
            with self.subTest(provider=name):
                targets = config["targets"]
                self.assertEqual(len({t["id"] for t in targets.values()}), len(targets))
                routes = {r["id"]: r for r in config["routes"].values()}
                self.assertEqual(len(routes), len(config["routes"]))
                self.assertTrue({"daedalus"} | {"daedalus/" + t for t in CATEGORIES} <= set(routes))
                self.assertNotIn("daedalus/none", routes)
                for target in targets.values():
                    self.assertIn(target["llm_client"], config["llm_clients"])
                    self.assertTrue(target["model"])
                for route in routes.values():
                    if route["type"] == "passthrough":
                        self.assertIn(route["target"], targets)
                        self.assertNotIn("classifier", route)
                route = routes["daedalus"]
                self.assertEqual(route["type"], "decision_api")
                self.assertIn(route["default_target"], targets)
                classifier = route["classifier"]
                self.assertEqual(classifier["classify_trigger"], "user_turn")
                if classifier["base_url"] == "https://inference.baseten.co/v1":
                    self.assertEqual(classifier["model"], "decision-models/baseten-bison-latest")
                    self.assertEqual(classifier["baseten"]["response_model"], "decision-models/baseten-bison-31b-latest")
                    self.assertEqual(classifier["max_input_tokens"], 131072)
                    self.assertEqual(classifier["api_key_env"], "BASETEN_API_KEY")
                else:
                    self.assertNotIn("baseten", classifier)
                    self.assertEqual(classifier["max_input_tokens"], "auto")
                self.assertEqual(classifier["truncate_input"], classifier["model"] != "baselabs/bison-1")
                self.assertEqual(set(classifier["choices"]), CATEGORIES)
                for choice in classifier["choices"].values():
                    self.assertIn(choice["target"], targets)
                    self.assertTrue(0 <= choice["min_probability"] <= 1)

    def test_bison_fragment_selects_the_local_model_without_credentials(self):
        classifier = load_config(ROOT / "config/decision/bison.toml")["routes"]["stage"]["classifier"]
        self.assertEqual(classifier["base_url"], "http://decision-api.daedalus.svc.cluster.local:30000/v1")
        self.assertEqual(classifier["model"], "baselabs/bison-1")
        self.assertNotIn("baseten", classifier)
        self.assertNotIn("api_key_env", classifier)

    def test_classifier_prose_names_only_available_categories_and_fallback(self):
        import re

        for name, config in providers():
            with self.subTest(provider=name):
                route = config["routes"]["stage"]
                classifier = route["classifier"]
                text = classifier["instructions"] + " ".join(c["criteria"] for c in classifier["choices"].values())
                self.assertIsNone(re.search(r"\b(medium|xhigh)\b", text))
                self.assertIn("= " + route["default_target"], classifier["uncertain_criteria"])
                for target in config["targets"].values():
                    self.assertNotIn(target["model"], text)

    def test_payload_defaults_require_streaming(self):
        # Every shipped target opts into the required-streaming transport policy.
        for name, config in providers():
            for key, target in config["targets"].items():
                with self.subTest(provider=name, target=key):
                    self.assertIs(target.get("extra_body", {}).get("stream"), True)
                    if config["llm_clients"][target["llm_client"]]["format"] == "openai_chat":
                        self.assertNotIn("truncation", target.get("extra_body", {}))

    def test_optional_classifier_credential_is_discovered_for_every_provider(self):
        spec = importlib.util.spec_from_file_location("key_envs", ROOT / "scripts/list_api_key_envs.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        for name, config in providers():
            with self.subTest(provider=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "routes.toml"
                classifier = config["routes"]["stage"]["classifier"]
                classifier["api_key_env"] = "DECISION_API_KEY"
                path.write_text(dumps(config))
                self.assertEqual(helper.api_key_environment_names(path), sorted({"DECISION_API_KEY"} | {
                    client["api_key_env"] for client in config["llm_clients"].values() if "api_key_env" in client
                }))
                classifier["api_key_env"] = "invalid-key"
                path.write_text(dumps(config))
                with self.assertRaises(ValueError):
                    helper.api_key_environment_names(path)


if __name__ == "__main__":
    unittest.main()
