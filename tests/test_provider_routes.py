"""Shared routing contract for every discovered provider configuration."""

import importlib.util
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = {"instant", "low", "high", "max"}


def providers():
    paths = sorted(ROOT.glob("routes_*.toml"))
    if not paths:
        raise AssertionError("No provider route configurations discovered")
    return [(path.name, tomllib.loads(path.read_text())) for path in paths]


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
                self.assertEqual(classifier["classify_trigger"], "every_request")
                self.assertEqual(classifier["max_input_tokens"], "auto")
                self.assertTrue(classifier["truncate_input"])
                self.assertEqual(set(classifier["choices"]), CATEGORIES)
                for choice in classifier["choices"].values():
                    self.assertIn(choice["target"], targets)
                    self.assertTrue(0 <= choice["min_probability"] <= 1)

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

    def test_chat_payload_defaults_keep_caller_streaming_choice(self):
        for name, config in providers():
            for key, target in config["targets"].items():
                if config["llm_clients"][target["llm_client"]]["format"] != "openai_chat":
                    continue
                with self.subTest(provider=name, target=key):
                    self.assertNotIn("stream", target.get("extra_body", {}))
                    self.assertNotIn("truncation", target.get("extra_body", {}))

    def test_optional_classifier_credential_is_discovered_for_every_provider(self):
        spec = importlib.util.spec_from_file_location("key_envs", ROOT / "scripts/list_api_key_envs.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        for name, config in providers():
            with self.subTest(provider=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "routes.toml"
                source = (ROOT / name).read_text()
                path.write_text(source.replace(
                    '[routes.stage.classifier]\ntype = "decision_api"',
                    '[routes.stage.classifier]\ntype = "decision_api"\napi_key_env = "DECISION_API_KEY"',
                ))
                self.assertEqual(helper.api_key_environment_names(path), sorted({"DECISION_API_KEY"} | {
                    client["api_key_env"] for client in config["llm_clients"].values() if "api_key_env" in client
                }))
                path.write_text(path.read_text().replace('api_key_env = "DECISION_API_KEY"', 'api_key_env = "invalid-key"'))
                with self.assertRaises(ValueError):
                    helper.api_key_environment_names(path)


if __name__ == "__main__":
    unittest.main()
