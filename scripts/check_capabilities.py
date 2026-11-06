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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from email.message import Message
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


@dataclass
class FailureDetails:
    """Only protocol metadata, never prompts, output, tool arguments or keys."""

    stage: str | None = None
    request_id: str | None = None
    event_index: int | None = None
    previous_finish_reason: str | None = None
    finish_reason: str | None = None
    content_chars: int | None = None
    tool_call_parts: int | None = None
    done_seen: bool | None = None
    violation: str | None = None


class CheckFailed(Exception):
    """A sanitized failure code suitable for deployment output."""

    def __init__(self, code: str, details: FailureDetails | None = None) -> None:
        super().__init__(code)
        self.details = details


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def error_code(error):
    if isinstance(error, CheckFailed):
        return str(error)
    if isinstance(error, HTTPError):
        return f"http_{error.code}"
    return type(error).__name__


@contextmanager
def failure_context(details: FailureDetails) -> Iterator[None]:
    try:
        yield
    except Exception as error:
        if isinstance(error, CheckFailed):
            if error.details is None:
                error.details = details
            elif details.stage is not None:
                error.details.stage = details.stage
            raise
        # HTTP bodies and exception messages can contain credentials or content.
        raise CheckFailed(error_code(error), details) from error


def request_id(headers: Message | None, key: str) -> str | None:
    for header in ("x-request-id", "x-fireworks-request-id", "fw-request-id"):
        value = headers.get(header) if headers is not None else None
        if (
            isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", value)
            and not (key and key in value)
        ):
            return value
    return None


def finish_label(value: str | None) -> str | None:
    # Unknown provider values are useful to classify, but unsafe to echo verbatim.
    known = (None, "stop", "tool_calls", "length", "content_filter", "function_call")
    return value if value in known else "other"


def failure_message(error: Exception) -> str:
    code = error_code(error)
    details = error.details if isinstance(error, CheckFailed) else None
    messages = {
        "content_after_finish": "The provider sent additional text after declaring the response finished.",
        "tool_calls_after_finish": "The provider sent tool-call data after declaring the response finished.",
        "conflicting_finish_reason": "The provider changed its finish reason after declaring the response finished.",
        "duplicate_terminal": "The provider sent conflicting data after completion; see error_details.",
        "missing_terminal": "The provider stream ended without the expected successful completion markers.",
        "invalid_finish_reason": "The provider finish reason does not match the returned tool calls.",
        "invalid_delta": "The provider sent a malformed Chat delta.",
        "invalid_tool_calls": "The provider sent malformed tool-call fragments.",
        "text_mismatch": "The model did not return the requested text marker.",
        "tool_not_selected": "The model did not select exactly one synthetic probe tool.",
        "invalid_arguments": "The model returned incorrect arguments for the synthetic probe tool.",
        "result_not_consumed": "The model did not return the fresh marker supplied by the probe tool.",
        "incomplete": "The provider reported a failed or incomplete response.",
        "not_sse": "The provider did not return a server-sent event stream.",
        "missing_credential": "The API-key environment variable configured for this target is empty or unset.",
        "TimeoutError": "The provider request timed out.",
        "request_deadline": "The provider stream exceeded the request deadline.",
        "URLError": "Could not establish a connection to the provider endpoint.",
        "JSONDecodeError": "The provider returned invalid JSON in a stream event or tool arguments.",
    }
    if details and details.violation in messages:
        return messages[details.violation]
    if code.startswith("http_"):
        return f"The provider returned HTTP {code[5:]} during the capability check."
    return messages.get(code, f"Capability check failed ({code}).")


@dataclass(frozen=True)
class ContextWindow:
    tokens: int | None = None
    field: str | None = None
    reason: str | None = None


def discover_context_window(client: dict, model: str, *, timeout: float) -> ContextWindow:
    """Read the serving endpoint's metadata, without generating a large prompt."""
    key = os.environ.get(client.get("api_key_env", ""), "")
    require(bool(key) or not client.get("api_key_env"), "missing_credential")
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = Request(client["base_url"].rstrip("/") + "/models", headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
            require(len(raw) < 2_000_000, "response_limit")
            catalog = json.loads(raw)
    except HTTPError as error:
        # Some working inference services do not implement the optional catalog.
        if error.code in (404, 405, 501):
            error.close()
            return ContextWindow(reason="models_endpoint_unavailable")
        raise
    require(isinstance(catalog, dict), "invalid_models_metadata")
    require(not catalog.get("error"), "provider_error")
    models = catalog.get("data")
    require(
        isinstance(models, list) and all(isinstance(item, dict) for item in models),
        "invalid_models_metadata",
    )
    matches = [item for item in models if item.get("id") == model]
    if not matches:
        return ContextWindow(reason="model_not_listed")
    limits = {}
    for item in matches:
        for field in ("context_window", "context_length", "max_model_len", "max_context_length"):
            value = item.get(field)
            if value is not None:
                require(type(value) is int and value > 0, "invalid_context_window_metadata")
                limits[field] = min(value, limits.get(field, value))
    if not limits:
        return ContextWindow(reason="context_window_not_reported")
    # A serving cap can be lower than the architecture's context length.
    field = min(limits, key=limits.get)
    return ContextWindow(tokens=limits[field], field=field)


def routing_targets(route: dict) -> set[str]:
    """Completion destinations, including fallbacks but excluding judge-only models."""
    kind = route["type"]
    if kind == "noop":
        names = set()
    elif kind == "passthrough":
        names = {route["target"]}
    elif kind in ("random", "prefill_router"):
        names = set(route["targets"])
    elif kind in ("plan_execute", "auto", "stage_router"):
        names = {route["capable_target"], route["efficient_target"]}
    elif kind == "llm_classifier":
        names = (
            set(route["models"]["any"])
            if route.get("mode") == "custom"
            else {route["strong_target"], route["weak_target"]}
        )
    elif kind == "decision_api":
        names = {route["default_target"]}
        names.update(choice["target"] for choice in route["classifier"]["choices"].values())
    elif kind == "composite":
        names = routing_targets({**route["stage"], "type": "stage_router"})
        classifier = route["classifier"]
        if classifier.get("type") == "decision_api":
            names.update(choice["target"] for choice in classifier.get("choices", {}).values())
    elif kind == "advisor":
        names = {route["executor_target"]}
    else:
        raise CheckFailed("unsupported_context_window_route")
    if "subagents" in route:
        names.update(routing_targets(route["subagents"]))
    return names


def context_requirements(config: dict) -> dict[str, int]:
    required = {}
    for route in config.get("routes", {}).values():
        if "context_window" not in route:
            continue
        value = route["context_window"]
        require(type(value) is int and 0 < value <= 2**32 - 1, "invalid_context_window")
        for name in routing_targets(route):
            require(name in config["targets"], "unknown_context_window_target")
            required[name] = max(value, required.get(name, 0))
    return required


def exchange(url, key, body, api, timeout):
    details = FailureDetails(event_index=0)
    with failure_context(details):
        try:
            result = read_exchange(url, key, body, api, timeout, details)
        except HTTPError as error:
            details.request_id = request_id(error.headers, key)
            error.close()
            raise
    result["diagnostics"] = details
    return result


def read_exchange(
    url: str, key: str, body: dict, api: str, timeout: float, details: FailureDetails,
) -> dict:
    request = Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    started = time.monotonic()
    text, calls, finish, done, model = "", {}, None, False, None
    completed = None
    size = 0
    if api == "openai_chat":
        details.done_seen = False
    with urlopen(request, timeout=timeout) as response:
        details.request_id = request_id(response.headers, key)
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
                details.done_seen = True
                break
            details.event_index += 1
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
                delta = choice.get("delta")
                # An absent/null trailing delta carries no output, just like {}.
                if delta is None:
                    delta = {}
                require(isinstance(delta, dict), "invalid_delta")
                content = delta.get("content")
                require(content is None or isinstance(content, str), "invalid_delta")
                parts = delta.get("tool_calls")
                require(parts is None or isinstance(parts, list), "invalid_tool_calls")
                for part in parts or []:
                    require(isinstance(part, dict), "invalid_tool_calls")
                    require(type(part.get("index")) is int and part["index"] >= 0, "invalid_tool_calls")
                    function = part.get("function")
                    require(function is None or isinstance(function, dict), "invalid_tool_calls")
                    require(
                        all(value is None or isinstance(value, str) for value in (
                            part.get("id"), (function or {}).get("name"), (function or {}).get("arguments"),
                        )),
                        "invalid_tool_calls",
                    )
                next_finish = choice.get("finish_reason")
                details.previous_finish_reason = finish_label(finish)
                details.finish_reason = finish_label(next_finish)
                details.content_chars = len(content or "")
                details.tool_call_parts = len(parts or [])
                # Some Chat providers (including Mercury) use stop for completed
                # tool calls. The roundtrip still validates IDs and JSON arguments.
                if next_finish == "stop" and (calls or parts):
                    next_finish = "tool_calls"
                if finish is not None:
                    # A nonempty tool_calls array can still be a no-op: an existing
                    # index with empty fragments. Never append it again or create a
                    # phantom call. New identities, names and arguments remain errors.
                    if content:
                        details.violation = "content_after_finish"
                    elif any(
                        part["index"] not in calls
                        or part.get("id")
                        or part.get("type") not in (None, "function")
                        or (part.get("function") or {}).get("name")
                        or (part.get("function") or {}).get("arguments")
                        for part in parts or []
                    ):
                        details.violation = "tool_calls_after_finish"
                    elif next_finish not in (None, finish):
                        details.violation = "conflicting_finish_reason"
                    require(details.violation is None, "duplicate_terminal")
                    continue
                text += content or ""
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
                            (part.get("function") or {}).get(field) or ""
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
    details = FailureDetails(stage="setup")
    with failure_context(details):
        return run_target(client, target, timeout, max_tokens, transport, details)


def run_target(
    client: dict, target: dict, timeout: float, max_tokens: int,
    transport: Callable, details: FailureDetails,
) -> dict:
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

    def send(body: dict, stage: str) -> dict:
        details.stage = stage
        details.request_id = None
        result = transport(url, key, body, api, timeout)
        diagnostics = result.get("diagnostics")
        if diagnostics is not None:
            details.request_id = diagnostics.request_id
        identities.append(result["model"])
        return result

    text = send(
        {**base, field: [{"role": "user", "content": "Reply with only CANARY_OK."}]},
        "text",
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
        stage = "forced_tool" if forced else "automatic_tool"
        result = send(body, stage + "_selection")
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
            },
            stage + "_result",
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
    required_windows = context_requirements(config)
    report = {
        "version": 2,
        "routes_sha256": hashlib.sha256(raw).hexdigest(),
        "targets": [],
        "passed": True,
    }
    seen = set()
    context_cache = {}
    for name, target in config["targets"].items():
        client = config["llm_clients"][target["llm_client"]]
        required_window = required_windows.get(name)
        identity = json.dumps(
            [client, target["model"], target.get("extra_body", {}), required_window], sort_keys=True
        )
        if identity in seen:
            continue
        seen.add(identity)
        row = {
            "target": name,
            "model": target["model"],
            "api": client["format"],
            "settings_sha256": hashlib.sha256(identity.encode()).hexdigest(),
            "required_context_window": required_window,
            "context_window": None,
            "context_window_field": None,
            "context_window_status": "unknown",
        }
        started = time.monotonic()
        try:
            with reachable_client(client) as reachable:
                # Context metadata is advisory; always run the generation/tool checks.
                try:
                    # Share discovery across reasoning profiles, only within this run.
                    context_key = json.dumps([client, target["model"]], sort_keys=True)
                    if context_key not in context_cache:
                        context_cache[context_key] = discover_context_window(
                            reachable, target["model"], timeout=args.timeout
                        )
                    context = context_cache[context_key]
                    row.update(
                        context_window=context.tokens,
                        context_window_field=context.field,
                        context_window_status="reported" if context.tokens is not None else "unknown",
                    )
                    if context.reason:
                        row["context_window_reason"] = context.reason
                    if required_window is not None:
                        require(context.tokens is not None, "context_window_unknown")
                        require(context.tokens >= required_window, "context_window_too_small")
                except Exception as error:
                    row["warning"] = error_code(error)
                row.update(
                    check_target(
                        reachable,
                        target,
                        timeout=args.timeout,
                        max_tokens=args.max_output_tokens,
                    ),
                    passed=True,
                )
                if row["context_window"] is not None and "warning" not in row:
                    row["checks"].append("context_window")
        except Exception as error:
            row.update(passed=False, error=error_code(error), error_message=failure_message(error))
            if isinstance(error, CheckFailed) and error.details is not None:
                row["error_details"] = {
                    key: value for key, value in asdict(error.details).items() if value is not None
                }
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
