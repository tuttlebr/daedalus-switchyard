"""Exercise the deployed-route smoke command against a local HTTP server."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from scripts.test_routes import completed_text, responses_url


ROOT = Path(__file__).resolve().parents[1]


def success():
    return {
        "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "OK"},
        ]}],
    }


@contextmanager
def server(replies):
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append((self.path, body))
            status, payload = replies[body["model"]]
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("x-model-router-selected-model", "selected-upstream")
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", captured
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class RouteSmokeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="switchyard-smoke-fixture-")
        self.addCleanup(self.directory.cleanup)
        self.directory_path = Path(self.directory.name)
        self.routes = self.directory_path / "routes.toml"

    def write_routes(self, names):
        self.routes.write_text("\n".join(
            f'[routes.{name}]\nid = "daedalus/{name}"\ntype = "passthrough"\ntarget = "unused"\n'
            for name in names
        ))

    def run_command(self, command, **kwargs):
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15, **kwargs)

    def test_make_positional_and_variable_use_every_route_id(self):
        names = ["stage", "deep", "deep_max", "cheap"]
        self.write_routes(names)
        replies = {f"daedalus/{name}": (200, success()) for name in names}
        with server(replies) as (url, captured):
            for selector in [str(self.routes), f"ROUTES_FILE={self.routes}"]:
                with self.subTest(selector=selector):
                    result = self.run_command([
                        "make", "--no-print-directory", "test", selector,
                        f"TEST_BASE_URL={url}/v1", "TEST_MAX_OUTPUT_TOKENS=512",
                    ])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("4 passed, 0 failed", result.stdout)
                    self.assertIn("selected=selected-upstream", result.stdout)
            self.assertEqual([body["model"] for _, body in captured], list(replies) * 2)
            for path, body in captured:
                self.assertEqual(path, "/v1/responses")
                self.assertFalse(body["stream"])
                self.assertEqual(body["max_output_tokens"], 512)
                self.assertNotIn("reasoning", body)

    def test_failures_do_not_stop_remaining_routes(self):
        names = ["rejected", "invalid_json", "incomplete", "empty", "api_error", "good"]
        self.write_routes(names)
        replies = {
            "daedalus/rejected": (404, {"error": {"message": "route not deployed"}}),
            "daedalus/invalid_json": (200, b"not JSON"),
            "daedalus/incomplete": (200, {**success(), "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}),
            "daedalus/empty": (200, {"status": "completed", "output": []}),
            "daedalus/api_error": (200, {"error": {"message": "upstream unavailable"}}),
            "daedalus/good": (200, success()),
        }
        with server(replies) as (url, captured):
            result = self.run_command([
                "make", "--no-print-directory", "test", str(self.routes), f"TEST_BASE_URL={url}",
            ])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(captured), 6)
        self.assertIn("HTTP 404", result.stdout)
        self.assertIn("route not deployed", result.stdout)
        self.assertIn("max_output_tokens", result.stdout)
        self.assertIn("1 passed, 5 failed", result.stdout)

    def test_invalid_config_fails_before_connection(self):
        for contents in ["", "[routes.bad]\ntype='noop'", "[routes.bad"]:
            with self.subTest(contents=contents):
                self.routes.write_text(contents)
                result = self.run_command(["make", "test", str(self.routes)])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Route test setup failed", result.stderr)
                self.assertNotIn("Connecting", result.stdout)

    def test_make_reports_missing_file_and_rejects_multiple_files(self):
        missing = self.directory_path / "missing.toml"
        result = self.run_command(["make", "test", str(missing)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No such file", result.stderr)
        result = self.run_command(["make", "test", str(missing), str(self.routes)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Use one TOML file", result.stderr)

    def test_provider_selection_is_required_and_discovered(self):
        for target in ("test", "validate", "deploy"):
            result = self.run_command(["make", target, "ROUTES_FILE="])
            self.assertNotEqual(result.returncode, 0)
            for path in ROOT.glob("routes_*.toml"):
                self.assertIn(path.name, result.stderr)
        env = {**os.environ, "ROUTES_FILE": ""}
        result = self.run_command(["bash", "scripts/deploy.sh"], env=env)
        self.assertEqual(result.returncode, 64)
        self.assertIn("Set ROUTES_FILE", result.stderr)

    def test_port_forward_arguments_and_cleanup(self):
        self.write_routes(["stage"])
        # A real child process makes tunnel cleanup observable without a cluster.
        kubectl = self.directory_path / "kubectl"
        stopped = self.directory_path / "stopped"
        arguments = self.directory_path / "arguments.json"
        with server({"daedalus/stage": (200, success())}) as (url, _):
            port = url.rsplit(":", 1)[1]
            kubectl.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, signal, sys\n"
                f"pathlib.Path({str(arguments)!r}).write_text(json.dumps(sys.argv[1:]))\n"
                "def stop(*args):\n"
                f"    pathlib.Path({str(stopped)!r}).touch()\n"
                "    sys.exit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                f"print('Forwarding from 127.0.0.1:{port} -> 4000', flush=True)\n"
                "signal.pause()\n"
            )
            kubectl.chmod(0o755)
            result = self.run_command([
                "make", "test", str(self.routes), "TEST_BASE_URL=",
                "EXPECTED_CONTEXT=fixture-context", "TEST_NAMESPACE=fixture-ns", "TEST_SERVICE=fixture-service",
            ], env={**os.environ, "PATH": f"{self.directory_path}:{os.environ['PATH']}"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(stopped.exists(), "kubectl child must be terminated")
        self.assertEqual(json.loads(arguments.read_text()), [
            "--context", "fixture-context", "--namespace", "fixture-ns", "port-forward",
            "service/fixture-service", "0:4000", "--address=127.0.0.1", "--pod-running-timeout=15s",
        ])

    def test_response_validation_rejects_noncompletion_shapes(self):
        for body in [[], {"status": "queued"}, {"status": "completed", "output": None},
                     {"status": "completed", "output": [{"type": "message", "content": None}]}]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                completed_text(body)

    def test_base_urls_accept_root_v1_and_responses_path(self):
        for url in ["http://localhost:4000", "http://localhost:4000/v1/", "http://localhost:4000/v1/responses"]:
            self.assertEqual(responses_url(url), "http://localhost:4000/v1/responses")
        for url in ["file:///tmp/routes", "http://user:password@localhost:4000", "http://localhost/?key=value"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                responses_url(url)


if __name__ == "__main__":
    unittest.main()
