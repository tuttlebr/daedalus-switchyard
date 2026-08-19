#!/usr/bin/env python3
"""Validate the deployment invariants that Switchyard's TOML schema cannot express."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tomllib
from typing import Never


ALLOWED_REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
EXPECTED_FORMATS = {
    "strong": "openai_responses",
    "weak": "openai_responses",
    "classifier": "openai_chat",
}
CLASSIFIER_BASE_URL = (
    "http://inference-gateway.daedalus-dynamo.svc.cluster.local:32359/v1"
)
ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def fail(message: str) -> Never:
    raise ValueError(message)


def load_routes(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as routes_file:
            return tomllib.load(routes_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        fail(f"could not load {path}: {error}")


def api_key_environment_names(clients: dict[str, object]) -> list[str]:
    names: set[str] = set()
    for client_name, client in clients.items():
        if not isinstance(client, dict):
            fail(f"llm_clients.{client_name} must be a TOML table")
        api_key_env = client.get("api_key_env")
        if api_key_env is None:
            continue
        if not isinstance(api_key_env, str) or not ENVIRONMENT_VARIABLE_PATTERN.fullmatch(
            api_key_env
        ):
            fail(
                f"llm_clients.{client_name}.api_key_env must be a valid "
                "environment variable name"
            )
        names.add(api_key_env)
    return sorted(names)


def validate(path: Path) -> list[str]:
    config = load_routes(path)
    if config.get("schema_version") != 1:
        fail("schema_version must be 1")

    clients = config.get("llm_clients")
    targets = config.get("targets")
    routes = config.get("routes")
    if (
        not isinstance(clients, dict)
        or not isinstance(targets, dict)
        or not isinstance(routes, dict)
    ):
        fail("llm_clients, targets, and routes must be TOML tables")

    api_key_envs = api_key_environment_names(clients)

    target_clients: dict[str, str] = {}
    target_ids: dict[str, str] = {}
    for target_name in EXPECTED_FORMATS:
        target = targets.get(target_name)
        if not isinstance(target, dict):
            fail(f"targets.{target_name} is required")

        client_name = target.get("llm_client")
        model_id = target.get("id")
        extra_body = target.get("extra_body")
        if not isinstance(client_name, str) or not client_name:
            fail(f"targets.{target_name}.llm_client must be a non-empty string")
        if not isinstance(model_id, str) or not model_id:
            fail(f"targets.{target_name}.id must be a non-empty string")

        client = clients.get(client_name)
        if not isinstance(client, dict):
            fail(f"targets.{target_name} references missing llm_client {client_name!r}")
        expected_format = EXPECTED_FORMATS[target_name]
        if client.get("format") != expected_format:
            fail(f"llm_clients.{client_name}.format must be {expected_format}")
        if target_name == "classifier":
            if client.get("base_url") != CLASSIFIER_BASE_URL:
                fail(
                    f"llm_clients.{client_name}.base_url must be "
                    f"{CLASSIFIER_BASE_URL!r}"
                )
            if client.get("api_key_env") is not None:
                fail(
                    f"llm_clients.{client_name}.api_key_env must be omitted for "
                    "the unauthenticated cluster-local gateway"
                )
            chat_template_kwargs = (
                extra_body.get("chat_template_kwargs")
                if isinstance(extra_body, dict)
                else None
            )
            if not isinstance(chat_template_kwargs, dict) or (
                chat_template_kwargs.get("enable_thinking") is not False
            ):
                fail(
                    "targets.classifier.extra_body.chat_template_kwargs."
                    "enable_thinking must be false"
                )
        elif client.get("api_key_env") is None:
            fail(f"llm_clients.{client_name}.api_key_env is required")

        reasoning = extra_body.get("reasoning") if isinstance(extra_body, dict) else None
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
        if target_name == "classifier":
            if effort is not None:
                fail(
                    "targets.classifier.extra_body.reasoning.effort must be "
                    f"omitted; found {effort!r}"
                )
        elif effort not in ALLOWED_REASONING_EFFORTS:
            allowed_efforts = ", ".join(
                repr(value) for value in ALLOWED_REASONING_EFFORTS
            )
            fail(
                f"targets.{target_name}.extra_body.reasoning.effort must be one of "
                f"{allowed_efforts}; found {effort!r}"
            )

        target_clients[target_name] = client_name
        target_ids[target_name] = model_id

    backend_keys: dict[tuple[str, str], str] = {}
    for target_name in EXPECTED_FORMATS:
        backend_key = (target_clients[target_name], target_ids[target_name])
        if existing_target := backend_keys.get(backend_key):
            fail(
                f"same-model targets {existing_target!r} and {target_name!r} must use "
                "different llm_clients; Switchyard v0.2.0 otherwise drops one "
                "target backend"
            )
        backend_keys[backend_key] = target_name

    stage = routes.get("stage")
    if not isinstance(stage, dict):
        fail("routes.stage is required")
    expected_stage = {
        "id": "switchyard/stage",
        "type": "llm_classifier",
        "mode": "capability",
        "classifier_target": "classifier",
        "strong_target": "strong",
        "weak_target": "weak",
        "base_threshold": 0.5,
        "threshold_step": 0.1,
    }
    for field, expected in expected_stage.items():
        if stage.get(field) != expected:
            fail(f"routes.stage.{field} must be {expected!r}")
    if stage.get("reasoning") is not False:
        fail(
            "routes.stage.reasoning must be false so client discovery does not add "
            "an inbound effort that overrides target defaults"
        )
    return api_key_envs


def main() -> int:
    parser = argparse.ArgumentParser()
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--list-api-key-envs", action="store_true")
    output_group.add_argument("--json-api-key-envs", action="store_true")
    parser.add_argument("path", nargs="?", default="routes.toml", type=Path)
    args = parser.parse_args()
    try:
        api_key_envs = validate(args.path)
    except ValueError as error:
        print(f"route validation failed: {error}", file=sys.stderr)
        return os.EX_CONFIG
    if args.list_api_key_envs:
        print(*api_key_envs, sep="\n")
        return 0
    if args.json_api_key_envs:
        print(json.dumps(api_key_envs, separators=(",", ":")))
        return 0
    print(f"route validation passed: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
