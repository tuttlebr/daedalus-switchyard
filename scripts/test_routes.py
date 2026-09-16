#!/usr/bin/env python3
"""Smoke-test configured route IDs through an already running Switchyard."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from http.client import HTTPException
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def load_routes(path: Path) -> dict[str, str]:
    with path.open("rb") as source:
        config = tomllib.load(source)
    routes = config.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise ValueError("TOML must contain at least one [routes.<name>] table")
    result = {}
    for name, route in routes.items():
        route_id = route.get("id") if isinstance(route, dict) else None
        if not isinstance(route_id, str) or not route_id.strip():
            raise ValueError(f"routes.{name}.id must be a nonempty string")
        result[name] = route_id
    return result


def responses_url(base_url: str) -> str:
    base_url = base_url.strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base URL must be an http:// or https:// URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain credentials, a query, or a fragment")
    if base_url.endswith("/responses"):
        return base_url
    if base_url.endswith("/v1"):
        return base_url + "/responses"
    return base_url + "/v1/responses"


def compact(value: object, limit: int = 240) -> str:
    # Keep server messages on one terminal line, without control sequences.
    text = " ".join(str(value).split())
    text = "".join(character for character in text if character.isprintable())
    return text if len(text) <= limit else text[:limit] + "..."


@contextmanager
def port_forward(context: str, namespace: str, service: str):
    command = ["kubectl"]
    if context:
        command += ["--context", context]
    command += [
        "--namespace", namespace, "port-forward", f"service/{service}",
        "0:4000", "--address=127.0.0.1", "--pod-running-timeout=15s",
    ]
    with tempfile.NamedTemporaryFile(prefix="switchyard-route-test-") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 20
            while True:
                output = Path(log.name).read_text(errors="replace")
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+) ->", output)
                if process.poll() is not None:
                    raise ValueError(f"kubectl port-forward failed: {compact(output)}")
                if match:
                    yield f"http://127.0.0.1:{match.group(1)}"
                    return
                if time.monotonic() >= deadline:
                    raise ValueError(f"kubectl port-forward did not start: {compact(output)}")
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def completed_text(body: object) -> str:
    if not isinstance(body, dict):
        raise ValueError("response is not a JSON object")
    if body.get("error"):
        error = body["error"]
        message = error.get("message", error) if isinstance(error, dict) else error
        raise ValueError(f"API error: {compact(message)}")
    if body.get("status") != "completed":
        detail = body.get("incomplete_details") or ""
        raise ValueError(f"response status is {body.get('status')!r} {compact(detail)}")
    parts = []
    output = body.get("output")
    if not isinstance(output, list):
        raise ValueError("response has no output array")
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    if not parts:
        raise ValueError("completed response has no assistant text")
    return " ".join(parts)


def http_error_message(error: HTTPError) -> str:
    try:
        body = json.loads(error.read())
    except (ValueError, OSError, HTTPException):
        return "non-JSON error response"
    if isinstance(body, dict):
        detail = body.get("error", body.get("detail", "request rejected"))
        if isinstance(detail, dict):
            detail = detail.get("message", detail)
        return compact(detail)
    return "request rejected"


def run_tests(routes: dict[str, str], url: str, timeout: float, max_tokens: int) -> int:
    failed = 0
    for name, route_id in routes.items():
        label = f"{name} ({route_id})"
        print(f"TEST {label}", flush=True)
        started = time.monotonic()
        status = ""
        try:
            request = Request(
                url,
                data=json.dumps({
                    "model": route_id,
                    "input": "Reply with exactly OK.",
                    "stream": False,
                    "max_output_tokens": max_tokens,
                }).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=timeout) as response:
                status = f"HTTP {response.status}; "
                text = completed_text(json.load(response))
                selected = response.headers.get("x-model-router-selected-model", "not reported")
            elapsed = time.monotonic() - started
            print(f"PASS {label}: {status}{elapsed:.2f}s; selected={compact(selected)}; {compact(text)}", flush=True)
        except HTTPError as error:
            failed += 1
            with error:
                detail = http_error_message(error)
            print(f"FAIL {label}: HTTP {error.code}; {time.monotonic() - started:.2f}s; {detail}", flush=True)
        except (OSError, URLError, HTTPException, ValueError) as error:
            failed += 1
            print(f"FAIL {label}: {status}{time.monotonic() - started:.2f}s; {compact(error)}", flush=True)
    print(f"\n{len(routes) - failed} passed, {failed} failed ({len(routes)} routes).", flush=True)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("routes_file", type=Path)
    parser.add_argument("--base-url", default="", help="running Switchyard URL; otherwise use kubectl port-forward")
    parser.add_argument("--context", default="", help="kubectl context (defaults to the current context)")
    parser.add_argument("--namespace", default="daedalus")
    parser.add_argument("--service", default="switchyard")
    parser.add_argument("--timeout", type=float, default=120, help="HTTP timeout in seconds per route")
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.max_output_tokens <= 0:
        parser.error("timeout and max-output-tokens must be positive and finite")
    try:
        routes = load_routes(args.routes_file)
        if args.base_url:
            responses_url(args.base_url)  # Validate before starting any requests.
            connection = nullcontext(args.base_url)
        else:
            print(f"Connecting to {args.namespace}/service/{args.service} via kubectl port-forward...", flush=True)
            connection = port_forward(args.context, args.namespace, args.service)
        with connection as base_url:
            url = responses_url(base_url)
            print(f"Testing {len(routes)} deployed routes from {args.routes_file} at {url}\n", flush=True)
            return run_tests(routes, url, args.timeout, args.max_output_tokens)
    except (OSError, ValueError) as error:
        print(f"Route test setup failed: {compact(error)}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nRoute tests interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
