#!/usr/bin/env python3
"""Resolve environment references only in provider extra_headers, before Helm."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import date, datetime, time
import json
import os
from pathlib import Path
import re
import sys
import tomllib
from typing import Any


REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def header_tables(config: dict[str, Any]) -> list[dict[str, str]]:
    clients = config.get("llm_clients", {})
    if not isinstance(clients, dict):
        raise ValueError("llm_clients must be a TOML table")
    tables = []
    for name, client in clients.items():
        if not isinstance(client, dict):
            raise ValueError(f"llm_clients.{name} must be a TOML table")
        headers = client.get("extra_headers", {})
        if not isinstance(headers, dict) or any(not isinstance(value, str) for value in headers.values()):
            raise ValueError(f"llm_clients.{name}.extra_headers must contain string values")
        for value in headers.values():
            if "${" in REFERENCE.sub("", value):
                raise ValueError(f"llm_clients.{name}.extra_headers requires references of the form ${{NAME}}")
        tables.append(headers)
    return tables


def environment_names(config: dict[str, Any]) -> list[str]:
    return sorted({name for headers in header_tables(config) for value in headers.values()
                   for name in REFERENCE.findall(value)})


def toml_value(value: Any) -> str:
    # Serialize the temporary copy, not the source. Parsing first prevents a
    # substitution from affecting prompts/comments or injecting TOML syntax.
    if isinstance(value, str):
        # JSON permits literal DEL, TOML does not. Keep non-ASCII literal to
        # avoid JSON surrogate-pair escapes, which TOML also forbids.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{toml_value(key)} = {toml_value(item)}" for key, item in value.items()) + " }"
    raise ValueError("unsupported TOML value")


def render(source: str, environment: Mapping[str, str]) -> str:
    config = tomllib.loads(source)
    names = environment_names(config)
    for name in names:
        value = environment.get(name)
        if not value:
            raise ValueError(f"Set {name} in .env or the environment; extra_headers requires a nonempty value")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError(f"{name} must not contain HTTP header control characters")
    if not names:
        return source
    for headers in header_tables(config):
        for key, value in headers.items():
            headers[key] = REFERENCE.sub(lambda match: environment[match[1]], value)
    return "\n".join(f"{toml_value(key)} = {toml_value(value)}" for key, value in config.items()) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--list-env", action="store_true", help="list header variable names without resolving them")
    args = parser.parse_args()
    if not args.list_env and args.output is None:
        parser.error("output is required unless --list-env is used")
    try:
        source = args.source.read_text()
        if args.list_env:
            names = environment_names(tomllib.loads(source))
            if names:
                print(*names, sep="\n")
        else:
            if args.source.resolve() == args.output.resolve():
                raise ValueError("output must differ from the source route file")
            rendered = render(source, os.environ)
            args.output.write_text(rendered)
    except (OSError, ValueError) as error:
        print(f"could not render routes: {error}", file=sys.stderr)
        return os.EX_CONFIG
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
