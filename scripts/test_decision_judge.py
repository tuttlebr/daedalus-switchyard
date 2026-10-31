#!/usr/bin/env python3
"""Probe the configured SystemOne routing judge with bounded synthetic tasks.

This checks the native contract and reports routing decisions; it does not prove
answering-model quality or deployment readiness. No requested actions execute.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

import tomllib

CASES = {
    "greeting": "Hello! How are you?",
    "email": "Read my unread email and summarize the messages that need a reply.",
    "private_briefing": "Prepare my morning briefing using my private email and medical appointment messages.",
    "medium": "Explain this supplied Python expression in a short paragraph: sorted(set([3, 1, 3])).",
    "medium_summary": "Summarize this public park notice in two plain-language sentences: The north trail will close for maintenance on Monday and Tuesday. The south trail and picnic area remain open. Visitors should use the east parking lot while crews repair the north footbridge.",
    "high": "Research the public documentation for SQLite and PostgreSQL, compare their concurrency and deployment tradeoffs, and recommend one for a small multi-user hobby app. Cite sources.",
    "xhigh": "Implement a responsive HTML page with semantic structure, accessible navigation, a mobile layout and a contact form, then validate the finished page.",
    "simple": "What is 7 + 9? Return only the integer.",
    "proof": "Prove a necessary and sufficient convergence criterion for a non-normal matrix iteration under time-varying perturbations; include a rigorous counterexample to a weaker criterion.",
    "operations": "Plan a production Kubernetes storage migration preserving PVCs, with concurrent writers, corruption checks, verification gates and rollback.",
    "ambiguous": "Continue it and resolve their disagreement. No earlier task context is available.",
}
OPTIONS = ("efficient", "capable", "uncertain")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def endpoint(base: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/v1/systemone"):
        return base
    return base + ("/systemone" if base.endswith("/v1") else "/v1/systemone")


def validate_answer_fields(answer: dict) -> None:
    """Match the gateway's strict choice schema, including SGLang metadata."""
    required = {"type", "choice", "confidence", "probabilities"}
    if not required <= set(answer) or set(answer) - required - {"x_label_mass"}:
        raise ValueError("Unexpected decision answer fields")
    mass = answer.get("x_label_mass")
    if mass is not None and (
        type(mass) not in (float, int) or not math.isfinite(mass) or not 0 <= mass <= 1
    ):
        raise ValueError("Invalid SGLang label mass")


def probe(config: dict, base: str, fallback_target: str | None = None) -> list[dict]:
    key_env = config.get("api_key_env")
    headers = {"Content-Type": "application/json"}
    if key_env:
        key = os.environ.get(key_env)
        if not key:
            raise ValueError(f"Set the configured credential variable {key_env}")
        headers["Authorization"] = f"Bearer {key}"
    choices = config.get("choices", {})
    if choices:
        if not fallback_target:
            raise ValueError("Named choices require the stage capable target")
        criteria = {name: choice["criteria"] for name, choice in choices.items()}
        criteria["uncertain"] = config["uncertain_criteria"]
    else:
        criteria = {option: config[option + "_criteria"] for option in OPTIONS}
    question = {
        "type": "choice",
        "instructions": config["instructions"],
        "criteria": criteria,
    }
    results = []
    for name, prompt in CASES.items():
        started = time.monotonic()
        deadline = started + config.get("timeout_ms", 2000) / 1000
        model = config["model"]
        if model == "auto":
            request = Request(endpoint(base).rsplit("/", 1)[0] + "/models", headers=headers)
            with build_opener(NoRedirect).open(
                request, timeout=max(0.001, deadline - time.monotonic())
            ) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("Model discovery response exceeds the response budget")
            models = json.loads(raw)["data"]
            if not isinstance(models, list) or len(models) != 1:
                raise ValueError("Automatic discovery requires exactly one served model")
            model = models[0]["id"]
            if (
                not isinstance(model, str) or not model or len(model.encode()) > 512
                or model.strip() != model or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in model)
            ):
                raise ValueError("Invalid discovered model identity")
        payload = {
            "model": model,
            "state": {
                "client_instructions": [],
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": prompt}]}
                ],
                "available_tools": [],
            },
            "questions": {"effort": question},
        }
        body = json.dumps(payload).encode()
        if len(body) > config.get("max_request_bytes", 12000):
            raise ValueError("Synthetic request exceeds the configured input budget")
        if time.monotonic() >= deadline:
            raise TimeoutError("Decision deadline exceeded during discovery")
        request = Request(endpoint(base), data=body, headers=headers)
        with build_opener(NoRedirect).open(
            request, timeout=deadline - time.monotonic()
        ) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("Decision response exceeds the response budget")
            value = json.loads(raw)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        if value["model"] != model or set(value["answers"]) != {"effort"}:
            raise ValueError("Unexpected decision model or question")
        answer = value["answers"]["effort"]
        validate_answer_fields(answer)
        probabilities = answer["probabilities"]
        confidence = answer["confidence"]
        if (
            answer["type"] != "choice"
            or answer["choice"] not in criteria
            or set(probabilities) != set(criteria)
            or any(
                type(p) not in (float, int) or not math.isfinite(p) or not 0 <= p <= 1
                for p in probabilities.values()
            )
            or type(confidence) not in (float, int)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
            or abs(sum(probabilities.values()) - 1)
            > max(0.0005, len(criteria) * 0.00005 + 1e-9)
            or max(probabilities.values()) > probabilities[answer["choice"]] + 0.0005
        ):
            raise ValueError("Invalid decision probability distribution")
        usage = value["usage"]
        if (
            type(usage["input_tokens"]) is not int
            or type(usage["output_tokens"]) is not int
            or not 0 < usage["input_tokens"] < config.get("max_input_tokens", 16384)
            or usage["output_tokens"] != 0
        ):
            raise ValueError("Unexpected native usage or potentially truncated input")
        row = {
            "case": name,
            "model": model,
            "choice": answer["choice"],
            "probabilities": probabilities,
            "elapsed_ms": elapsed_ms,
            "usage": usage,
        }
        if choices:
            choice = choices.get(answer["choice"])
            accepted = bool(choice) and probabilities[answer["choice"]] >= choice.get(
                "min_probability", 0.9
            )
            row.update(
                accepted=accepted,
                selected_target=choice["target"] if accepted else fallback_target,
                min_probability=choice.get("min_probability", 0.9) if choice else None,
            )
        else:
            efficient = answer["choice"] == "efficient" and probabilities[
                "efficient"
            ] >= config.get("efficient_threshold", 0.9)
            row["default_tier"] = "efficient" if efficient else "capable"
        results.append(row)
        print(json.dumps(row), flush=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "routes", type=Path, nargs="?", default=Path("routes_custom.toml")
    )
    parser.add_argument("--route", default="stage", help="TOML route table name")
    parser.add_argument("--base-url", help="Override the target for a port-forward")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        route = tomllib.loads(args.routes.read_text())["routes"][args.route]
        config = route["classifier"]
        if config.get("type") != "decision_api":
            raise ValueError(
                "Selected route does not use a native decision_api classifier"
            )
        results = probe(
            config,
            args.base_url or config["base_url"],
            route["stage"]["capable_target"],
        )
        if args.output:
            args.output.write_text(
                json.dumps(
                    {
                        "scope": "Synthetic judge contract only; routing accuracy uncalibrated",
                        "cases": results,
                    },
                    indent=2,
                )
                + "\n"
            )
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
        # Do not expose provider response bodies, credentials or private context.
        print(
            f"Decision judge probe failed ({type(error).__name__}); verify the service, configuration and native response contract.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
