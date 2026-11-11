#!/usr/bin/env python3
"""Compose route fragments, then resolve provider header references before Helm."""

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


def merge_config(base: dict[str, Any], overlay: dict[str, Any], location: str = "") -> None:
    """Merge tables recursively; later leaves and arrays replace earlier values."""
    for key, value in overlay.items():
        field = f"{location}.{key}" if location else key
        if key in base and isinstance(base[key], dict) != isinstance(value, dict):
            raise ValueError(f"{field}: cannot replace a table with a value or a value with a table")
        if isinstance(value, dict):
            merge_config(base.setdefault(key, {}), value, field)
        else:
            base[key] = value


def load_config(path: Path, *, sources: dict[Path, str] | None = None) -> dict[str, Any]:
    """Read local includes relative to their declaring file, once per snapshot."""
    sources = {} if sources is None else sources

    def load(current: Path, parents: tuple[Path, ...]) -> dict[str, Any]:
        current = current.resolve()
        if current in parents:
            raise ValueError("include cycle: " + " -> ".join(str(p) for p in (*parents, current)))
        if len(parents) >= 32:
            raise ValueError(f"include nesting exceeds 32 files: {current}")
        try:
            if current not in sources:
                sources[current] = current.read_text()
            config = tomllib.loads(sources[current])
        except (OSError, ValueError) as error:
            raise ValueError(f"could not read {current}: {error}") from error
        includes = config.pop("include", [])
        if not isinstance(includes, list) or any(not isinstance(item, str) or not item.strip() for item in includes):
            raise ValueError(f"{current}: include must be an array of nonempty local file paths")
        merged: dict[str, Any] = {}
        for include in includes:
            merge_config(merged, load(current.parent / include, (*parents, current)))
        merge_config(merged, config)
        return merged

    return load(path, ())


def compose_source(path: Path, *outputs: Path | None) -> str:
    sources: dict[Path, str] = {}
    config = load_config(path, sources=sources)
    if any(output is not None and output.resolve() in sources for output in outputs):
        raise ValueError("output must differ from every source route file and included fragment")
    original = sources[path.resolve()]
    return dumps(config) if "include" in tomllib.loads(original) else original


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


def dumps(config: dict[str, Any]) -> str:
    """Emit readable standalone TOML; provider payloads remain inline tables."""
    lines: list[str] = []

    def key(value: str) -> str:
        return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else toml_value(value)

    def table(values: dict[str, Any], path: tuple[str, ...]) -> None:
        children = {}
        fields = []
        for name, value in values.items():
            if isinstance(value, dict) and name not in {"extra_body", "extra_headers"}:
                children[name] = value
            else:
                fields.append(f"{key(name)} = {toml_value(value)}")
        if fields or not children:
            if path:
                lines.append("[" + ".".join(key(part) for part in path) + "]")
            lines.extend(fields)
            lines.append("")
        for name, value in children.items():
            table(value, (*path, name))

    table(config, ())
    return "\n".join(lines)


def render(source: str, environment: Mapping[str, str]) -> str:
    config = tomllib.loads(source)
    if "include" in config:
        raise ValueError("compose includes before rendering header references")
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
    return dumps(config)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--list-env", action="store_true", help="list header variable names without resolving them")
    parser.add_argument("--compose-only", action="store_true", help="flatten includes without resolving header variables")
    parser.add_argument("--check-output", type=Path, help="also protect a later rendering destination from overwriting inputs")
    args = parser.parse_args()
    if not args.list_env and args.output is None:
        parser.error("output is required unless --list-env is used")
    try:
        source = compose_source(args.source, args.output, args.check_output)
        if args.list_env:
            names = environment_names(tomllib.loads(source))
            if names:
                print(*names, sep="\n")
        else:
            rendered = source if args.compose_only else render(source, os.environ)
            args.output.write_text(rendered)
    except (OSError, ValueError) as error:
        print(f"could not render routes: {error}", file=sys.stderr)
        return os.EX_CONFIG
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
