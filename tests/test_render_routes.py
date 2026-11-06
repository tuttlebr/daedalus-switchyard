"""Header rendering, environment precedence and validation-path regressions."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("render_routes", ROOT / "scripts/render_routes.py")
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)

SOURCE = '''# A prompt and a header can contain the same placeholder.
schema_version = 1
[llm_clients.fixture]
api_key_env = "FIXTURE_API_KEY"
extra_headers = { x-multi-turn-session-id = "${GLOBAL_SESSION_ID}", x-session-affinity = "prefix-${GLOBAL_SESSION_ID}" }
[targets.fixture]
extra_body = { prompt = "Keep ${GLOBAL_SESSION_ID} and ${OTHER} literal", stream = true }
[routes.stage]
instructions = """A multiline prompt.
With a second line and ${OTHER}."""
'''


class Rendering(unittest.TestCase):
    def test_only_headers_change_and_values_cannot_inject_toml(self):
        value = 'quoted "value" \\ path ${OTHER}'
        expected = tomllib.loads(SOURCE)
        expected['llm_clients']['fixture']['extra_headers'] = {
            'x-multi-turn-session-id': value,
            'x-session-affinity': 'prefix-' + value,
        }
        self.assertEqual(tomllib.loads(renderer.render(SOURCE, {'GLOBAL_SESSION_ID': value})), expected)
        self.assertEqual(renderer.environment_names(tomllib.loads(SOURCE)), ['GLOBAL_SESSION_ID'])

    def test_literal_configs_remain_byte_identical(self):
        source = SOURCE.replace('${GLOBAL_SESSION_ID}', 'literal-session')
        self.assertEqual(renderer.render(source, {}), source)
        source = (ROOT / 'routes_openai.toml').read_text()
        self.assertEqual(renderer.render(source, {}), source)

    def test_missing_empty_and_control_character_values_fail_without_echoing_values(self):
        for value in (None, '', 'private\r\nInjected: value', 'private\x00value', 'private\tvalue', 'private\x7fvalue'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as error:
                    renderer.render(SOURCE, {} if value is None else {'GLOBAL_SESSION_ID': value})
                self.assertIn('GLOBAL_SESSION_ID', str(error.exception))
                self.assertNotIn('private', str(error.exception))

    def test_malformed_placeholder_and_nonstring_headers_fail(self):
        for source in (SOURCE.replace('${GLOBAL_SESSION_ID}', '${BROKEN-NAME}'),
                       SOURCE.replace('${GLOBAL_SESSION_ID}', '${UNCLOSED'),
                       SOURCE.replace('"${GLOBAL_SESSION_ID}"', '123')):
            with self.subTest(source=source), self.assertRaises(ValueError):
                renderer.render(source, {})

    def test_table_form_and_other_toml_types_survive_rendering(self):
        source = '''dates = [1979-05-27, 07:32:00, 1979-05-27T07:32:00Z]
numbers = [1, 1.5, inf, -inf]
[[targets.example.entries]]
name = "entry"
[llm_clients.fixture.extra_headers]
"x-session-affinity" = '${GLOBAL_SESSION_ID}'
'''
        expected = tomllib.loads(source)
        expected['llm_clients']['fixture']['extra_headers']['x-session-affinity'] = 'session'
        self.assertEqual(tomllib.loads(renderer.render(source, {'GLOBAL_SESSION_ID': 'session'})), expected)

    def test_unicode_and_escaped_del_in_prompts_survive_rendering(self):
        source = SOURCE + 'text = "Unicode: 🦉; control: \\u007f"\n'
        rendered = tomllib.loads(renderer.render(source, {'GLOBAL_SESSION_ID': 'session'}))
        self.assertEqual(rendered['routes']['stage']['text'], 'Unicode: 🦉; control: \x7f')


class ShellRendering(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'scripts').mkdir()
        for name in ('render_routes.py', 'render_routes.sh', 'list_api_key_envs.py'):
            shutil.copy(ROOT / 'scripts' / name, self.root / 'scripts' / name)
        self.source = self.root / 'input routes.toml'
        self.source.write_text(SOURCE)
        self.output = self.root / 'rendered.toml'
        self.environment = {key: value for key, value in os.environ.items() if key != 'GLOBAL_SESSION_ID'}

    def run_render(self):
        return subprocess.run(
            ['bash', str(self.root / 'scripts/render_routes.sh'), str(self.source), str(self.output)],
            env=self.environment, capture_output=True, text=True, timeout=10,
        )

    def test_dotenv_and_caller_precedence_including_explicit_empty(self):
        (self.root / '.env').write_text('GLOBAL_SESSION_ID=from-dotenv\n')
        for override, expected in ((None, 'from-dotenv'), ('from-caller', 'from-caller')):
            with self.subTest(override=override):
                if override is not None:
                    self.environment['GLOBAL_SESSION_ID'] = override
                result = self.run_render()
                self.assertEqual(result.returncode, 0, result.stderr)
                config = tomllib.loads(self.output.read_text())
                self.assertEqual(config['llm_clients']['fixture']['extra_headers']['x-multi-turn-session-id'], expected)
                self.assertEqual(self.source.read_text(), SOURCE)
        self.environment['GLOBAL_SESSION_ID'] = ''
        before = self.output.read_text()
        result = self.run_render()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(), before)

    def test_missing_variable_does_not_create_output(self):
        result = self.run_render()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('GLOBAL_SESSION_ID', result.stderr)
        self.assertFalse(self.output.exists())

    def test_source_cannot_be_overwritten(self):
        self.output = self.source
        self.environment['GLOBAL_SESSION_ID'] = 'session'
        result = self.run_render()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.source.read_text(), SOURCE)

    def test_make_validation_renders_before_helm_and_stops_on_missing_variable(self):
        shutil.copy(ROOT / 'Makefile', self.root / 'Makefile')
        (self.root / 'bin').mkdir()
        (self.root / 'bin/helm').write_text('''#!/usr/bin/env python3
import os, pathlib, sys, tomllib
path = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('config.routesToml='))
config = tomllib.loads(pathlib.Path(path).read_text())
assert config['llm_clients']['fixture']['extra_headers']['x-multi-turn-session-id'] == 'from-dotenv'
assert 'apiKeySecret.keys=["FIXTURE_API_KEY"]' in sys.argv
with open(os.environ['CALL_LOG'], 'a') as log:
    log.write(sys.argv[1] + '\\n')
''')
        (self.root / 'bin/kubectl').write_text('#!/bin/sh\nexit 0\n')
        for path in (self.root / 'bin').iterdir():
            path.chmod(0o755)
        log = self.root / 'calls'
        self.environment.update(PATH=str(self.root / 'bin') + os.pathsep + self.environment['PATH'], CALL_LOG=str(log))
        for target in ('helm-lint', 'helm-template'):
            with self.subTest(target=target):
                command = ['make', target, f'ROUTES_FILE={self.source}']
                result = subprocess.run(command, cwd=self.root, env=self.environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(log.exists())
                (self.root / '.env').write_text('GLOBAL_SESSION_ID=from-dotenv\n')
                result = subprocess.run(command, cwd=self.root, env=self.environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(log.exists())
                log.unlink()
                (self.root / '.env').unlink()


if __name__ == '__main__':
    unittest.main()
