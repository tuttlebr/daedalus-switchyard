#!/usr/bin/env python3
"""Bounded, content-free provider capability gate, run before route promotion.

Only synthetic text and an in-process read-only tool are used. Credentials come
from the target's named environment variable; responses and secrets are not logged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

import tomllib

PARAMETERS = {
    "type": "object",
    "properties": {"key": {"type": "string", "enum": ["probe"]}},
    "required": ["key"],
    "additionalProperties": False,
}
TOOL = {
    "name": "probe_read",
    "description": "Read the synthetic canary marker. Its value is only in the tool result.",
    "parameters": PARAMETERS,
}
PROMPT = "Call probe_read with key probe to read the marker. Return only that marker after receiving its result."


@contextmanager
def reachable_client(client):
    """Reach cluster-local targets without changing their serving configuration."""
    parsed = urlsplit(client["base_url"])
    require(
        parsed.scheme in ("http", "https")
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment,
        "invalid_endpoint",
    )
    match = re.fullmatch(
        r"([a-z0-9-]+)\.([a-z0-9-]+)\.svc\.cluster\.local", parsed.hostname or ""
    )
    if not match:
        yield client
        return
    require(parsed.scheme == "http", "cluster_tls_requires_direct_access")
    service, namespace = match.groups()
    command = [
        "kubectl",
        "--namespace",
        namespace,
        "port-forward",
        f"service/{service}",
        f"0:{parsed.port or 80}",
        "--address=127.0.0.1",
        "--pod-running-timeout=15s",
    ]
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 20
            while True:
                log.seek(0)
                forwarding = re.search(
                    rb"Forwarding from 127\.0\.0\.1:(\d+)", log.read()
                )
                require(process.poll() is None, "port_forward_failed")
                if forwarding:
                    base = urlunsplit(
                        (
                            "http",
                            "127.0.0.1:" + forwarding[1].decode(),
                            parsed.path,
                            "",
                            "",
                        )
                    )
                    yield {**client, "base_url": base}
                    break
                require(time.monotonic() < deadline, "port_forward_timeout")
                time.sleep(0.05)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class CheckFailed(Exception):
    """A sanitized failure code suitable for deployment output."""


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def exchange(url, key, body, api, timeout):
    request = Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    started = time.monotonic()
    text, calls, finish, done, model = "", {}, None, False, None
    completed = None
    size = 0
    with urlopen(request, timeout=timeout) as response:
        require(
            "text/event-stream" in response.headers.get("Content-Type", ""), "not_sse"
        )
        # Every fixture requires a terminal event, including otherwise valid text.
        for line in response:
            size += len(line)
            require(size < 2_000_000, "response_limit")
            require(time.monotonic() - started < timeout, "request_deadline")
            if not line.startswith(b"data:"):
                continue
            raw = line[5:].strip()
            if raw == b"[DONE]":
                done = True
                break
            value = json.loads(raw)
            require(not value.get("error"), "provider_error")
            if api == "openai_responses":
                require(
                    value.get("type")
                    not in ("response.failed", "response.incomplete", "error"),
                    "incomplete",
                )
                if value.get("type") == "response.completed":
                    require(completed is None, "duplicate_terminal")
                    completed = value["response"]
                    break
                continue
            model = value.get("model", model)
            for choice in value.get("choices", []):
                require(choice.get("index", 0) == 0, "multiple_choices")
                delta = choice.get("delta", {})
                parts = delta.get("tool_calls")
                require(parts is None or isinstance(parts, list), "invalid_tool_calls")
                next_finish = choice.get("finish_reason")
                # Some Chat providers (including Mercury) use stop for completed
                # tool calls. The roundtrip still validates IDs and JSON arguments.
                if next_finish == "stop" and (calls or parts):
                    next_finish = "tool_calls"
                if finish is not None:
                    # Mercury repeats the finish reason in an empty final chunk.
                    # Accept that repetition, but reject new data or a changed reason.
                    require(
                        not delta.get("content")
                        and not parts
                        and next_finish in (None, finish),
                        "duplicate_terminal",
                    )
                text += delta.get("content") or ""
                # Optional Chat delta fields may be explicitly null.
                for part in parts or []:
                    call = calls.setdefault(
                        part["index"],
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    call["id"] += part.get("id") or ""
                    for field in ("name", "arguments"):
                        call["function"][field] += (
                            part.get("function", {}).get(field) or ""
                        )
                if next_finish is not None:
                    finish = next_finish
    if api == "openai_responses":
        require(
            completed is not None and completed.get("status") == "completed",
            "missing_terminal",
        )
        output = completed.get("output", [])
        text = "".join(
            c.get("text", "")
            for item in output
            if item.get("type") == "message"
            for c in item.get("content", [])
            if c.get("type") == "output_text"
        )
        calls = [c for c in output if c.get("type") == "function_call"]
        require(
            all(
                c.get("type") in ("message", "reasoning", "function_call")
                for c in output
            ),
            "unexpected_output",
        )
        return {
            "text": text,
            "calls": calls,
            "history": output,
            "model": completed.get("model"),
        }
    require(done and finish in ("stop", "tool_calls"), "missing_terminal")
    require((finish == "tool_calls") == bool(calls), "invalid_finish_reason")
    ordered = [calls[i] for i in sorted(calls)]
    assistant = {"role": "assistant", "content": text or None}
    if ordered:
        assistant["tool_calls"] = ordered
    return {"text": text, "calls": ordered, "history": [assistant], "model": model}


def check_target(client, target, *, timeout=60, max_tokens=4096, transport=exchange):
    api = client["format"]
    require(api in ("openai_chat", "openai_responses"), "unsupported_api")
    key = os.environ.get(client.get("api_key_env", ""), "")
    require(bool(key) or not client.get("api_key_env"), "missing_credential")
    chat = api == "openai_chat"
    url = client["base_url"].rstrip("/") + (
        "/chat/completions" if chat else "/responses"
    )
    field = "messages" if chat else "input"
    base = {
        **target.get("extra_body", {}),
        "model": target["model"],
        "stream": True,
        ("max_completion_tokens" if chat else "max_output_tokens"): max_tokens,
    }
    if not chat:
        base["store"] = False
    identities = []

    def send(body):
        result = transport(url, key, body, api, timeout)
        identities.append(result["model"])
        return result

    text = send(
        {**base, field: [{"role": "user", "content": "Reply with only CANARY_OK."}]}
    )
    require(text["text"].strip() == "CANARY_OK" and not text["calls"], "text_mismatch")
    for forced in (False, True):
        conversation = [{"role": "user", "content": PROMPT}]
        tool = (
            {"type": "function", "function": TOOL}
            if chat
            else {"type": "function", **TOOL}
        )
        choice = (
            (
                {"type": "function", "function": {"name": "probe_read"}}
                if chat
                else {"type": "function", "name": "probe_read"}
            )
            if forced
            else "auto"
        )
        body = {**base, field: conversation, "tools": [tool], "tool_choice": choice}
        result = send(body)
        require(len(result["calls"]) == 1, "tool_not_selected")
        call = result["calls"][0]
        function = call["function"] if chat else call
        call_id = call.get("id") if chat else call.get("call_id")
        require(isinstance(call_id, str) and bool(call_id), "missing_call_id")
        require(function.get("name") == "probe_read", "wrong_tool")
        require(
            json.loads(function["arguments"]) == {"key": "probe"}, "invalid_arguments"
        )
        marker = uuid.uuid4().hex
        receipt = (
            {"role": "tool", "tool_call_id": call_id, "content": marker}
            if chat
            else {"type": "function_call_output", "call_id": call_id, "output": marker}
        )
        final = send(
            {
                **body,
                field: conversation + result["history"] + [receipt],
                "tool_choice": "none",
            }
        )
        require(
            not final["calls"] and final["text"].strip() == marker,
            "result_not_consumed",
        )
    return {
        "checks": ["text", "automatic_tool_roundtrip", "forced_tool_roundtrip"],
        "reported_models": sorted({m for m in identities if m}),
        "unknown_model_responses": identities.count(None),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("routes", type=Path)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    require(
        0 < args.timeout <= 180 and 128 <= args.max_output_tokens <= 16384,
        "invalid_budget",
    )
    raw = args.routes.read_bytes()
    config = tomllib.loads(raw.decode())
    report = {
        "version": 1,
        "routes_sha256": hashlib.sha256(raw).hexdigest(),
        "targets": [],
        "passed": True,
    }
    seen = set()
    for name, target in config["targets"].items():
        client = config["llm_clients"][target["llm_client"]]
        identity = json.dumps(
            [client, target["model"], target.get("extra_body", {})], sort_keys=True
        )
        if identity in seen:
            continue
        seen.add(identity)
        row = {
            "target": name,
            "model": target["model"],
            "api": client["format"],
            "settings_sha256": hashlib.sha256(identity.encode()).hexdigest(),
        }
        started = time.monotonic()
        try:
            with reachable_client(client) as reachable:
                row.update(
                    check_target(
                        reachable,
                        target,
                        timeout=args.timeout,
                        max_tokens=args.max_output_tokens,
                    ),
                    passed=True,
                )
        except Exception as error:
            row.update(
                passed=False,
                error=str(error)
                if isinstance(error, CheckFailed)
                else f"http_{error.code}"
                if isinstance(error, HTTPError)
                else type(error).__name__,
            )
            report["passed"] = False
        row["elapsed_seconds"] = round(time.monotonic() - started, 2)
        report["targets"].append(row)
        print(json.dumps(row), flush=True)
    require(bool(report["targets"]), "no_targets")
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
