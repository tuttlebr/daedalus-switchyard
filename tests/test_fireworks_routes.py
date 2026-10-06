"""Deployment invariants; the built-binary wire tests live alongside these."""

from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FireworksRoutesTest(unittest.TestCase):
    def test_agent_profiles_resolve_to_distinct_target_identities(self):
        config = tomllib.loads((ROOT / "routes_fireworks.toml").read_text())
        targets = config["targets"]
        self.assertEqual(len({t["id"] for t in targets.values()}), len(targets))
        routes = {r["id"]: r for r in config["routes"].values()}
        for alias in ("daedalus", "daedalus/deep", "daedalus/xhigh", "daedalus/max"):
            self.assertIn(alias, routes)
        for route in routes.values():
            for key in ("target", "capable_target", "efficient_target"):
                if key in route:
                    self.assertIn(route[key], targets)

    def test_no_shared_session_identity_or_responses_only_fireworks_defaults(self):
        config = tomllib.loads((ROOT / "routes_fireworks.toml").read_text())
        for client in config["llm_clients"].values():
            self.assertFalse(
                any("session" in h.lower() for h in client.get("extra_headers", {}))
            )
        self.assertEqual(config["llm_clients"]["fireworks"]["format"], "openai_chat")
        for target in config["targets"].values():
            if target["llm_client"] == "fireworks":
                self.assertNotIn("truncation", target.get("extra_body", {}))
                self.assertNotEqual(target["extra_body"]["reasoning_effort"], "none")


if __name__ == "__main__":
    unittest.main()
