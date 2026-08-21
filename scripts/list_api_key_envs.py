#!/usr/bin/env python3
"""List credential environment variables referenced by a Switchyard config."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tomllib


ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def api_key_environment_names(path: Path) -> list[str]:
    try:
        with path.open("rb") as routes_file:
            config = tomllib.load(routes_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"could not read {path}: {error}") from error

    clients = config.get("llm_clients", {})
    if not isinstance(clients, dict):
        raise ValueError("llm_clients must be a TOML table")

    names: set[str] = set()
    for client_name, client in clients.items():
        if not isinstance(client, dict):
            continue
        api_key_env = client.get("api_key_env")
        if api_key_env is None:
            continue
        if not isinstance(api_key_env, str) or not ENVIRONMENT_VARIABLE_PATTERN.fullmatch(
            api_key_env
        ):
            raise ValueError(
                f"llm_clients.{client_name}.api_key_env cannot be used as an "
                "environment variable name"
            )
        names.add(api_key_env)
    return sorted(names)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("path", nargs="?", default="routes.toml", type=Path)
    args = parser.parse_args()

    try:
        names = api_key_environment_names(args.path)
    except ValueError as error:
        print(f"could not discover API-key environment names: {error}", file=sys.stderr)
        return os.EX_CONFIG

    if args.json:
        print(json.dumps(names, separators=(",", ":")))
    else:
        print(*names, sep="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
