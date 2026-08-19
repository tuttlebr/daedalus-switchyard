from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest

from scripts import validate_routes


PROJECT_DIR = Path(__file__).resolve().parents[1]
ROUTES_TEXT = (PROJECT_DIR / "routes.toml").read_text(encoding="utf-8")
COMPLETION_TARGETS = ("strong", "weak")


class ValidateReasoningEffortsTest(unittest.TestCase):
    def validate_with_effort(self, target_name: str, effort: str) -> None:
        target_pattern = re.compile(
            rf'(\[targets\.{re.escape(target_name)}\][^\[]*?effort = ")[^"]+("\s*}})',
            re.DOTALL,
        )
        updated_routes, replacement_count = target_pattern.subn(
            rf"\g<1>{effort}\2", ROUTES_TEXT, count=1
        )
        self.assertEqual(replacement_count, 1)

        with tempfile.TemporaryDirectory() as temporary_directory:
            routes_path = Path(temporary_directory) / "routes.toml"
            routes_path.write_text(updated_routes, encoding="utf-8")
            validate_routes.validate(routes_path)

    def test_all_supported_efforts_are_allowed_for_completion_targets(self) -> None:
        for target_name in COMPLETION_TARGETS:
            for effort in validate_routes.ALLOWED_REASONING_EFFORTS:
                with self.subTest(target=target_name, effort=effort):
                    self.validate_with_effort(target_name, effort)

    def test_unknown_effort_is_rejected_for_completion_targets(self) -> None:
        for target_name in COMPLETION_TARGETS:
            with self.subTest(target=target_name):
                with self.assertRaisesRegex(ValueError, "must be one of"):
                    self.validate_with_effort(target_name, "minimal")


if __name__ == "__main__":
    unittest.main()
