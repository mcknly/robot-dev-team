#!/usr/bin/env python3
"""Robot Dev Team Project
File: scripts/header_guard.py
Description: Validate mandatory file headers across supported file types.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

HEADER_TOKENS: Sequence[str] = (
    "Robot Dev Team Project",
    "License: MIT",
    "SPDX-License-Identifier: MIT",
    "Copyright (c) 2025 MCKNLY LLC",
)

DEFAULT_SUFFIXES: Sequence[str] = (".py", ".sh", ".md", ".yaml", ".yml", ".toml", ".ini")
DEFAULT_FILENAMES: Sequence[str] = (
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.override.yml",
    "docker-compose.override.example.yml",
    "launch-uvicorn-dev",
    "gitlab-connect",
    "glab-usr",
)
DEFAULT_EXCLUDED_PREFIXES: Sequence[str] = (
    ".git/",
    ".venv/",
    "npm-cache/",
    "run-logs/",
    "sbom/",
    "test-config/",
)

DEFAULT_CONFIG_PATH = Path("config/header_guard.toml")

signal.signal(signal.SIGPIPE, signal.SIG_DFL)


@dataclass(frozen=True)
class GuardRuntimeConfig:
    supported_suffixes: tuple[str, ...]
    special_filenames: tuple[str, ...]
    excluded_prefixes: tuple[str, ...]


def _dedupe_preserve_order(values: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return tuple(seen.keys())


def _normalize_config_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    raise SystemExit(f"Configuration entries must be strings or lists of strings, got {type(value)!r}.")


def _merge_values(base: Sequence[str], extras: Iterable[str]) -> tuple[str, ...]:
    merged = list(base)
    merged.extend(str(item) for item in extras)
    return _dedupe_preserve_order(merged)


def load_config(path: Path | None) -> dict[str, Any]:
    """Load optional TOML configuration for header guard."""

    resolved = path
    if resolved is None:
        if not DEFAULT_CONFIG_PATH.exists():
            return {}
        resolved = DEFAULT_CONFIG_PATH

    try:
        with resolved.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise SystemExit(f"Config file not found: {resolved}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"Failed to parse config file {resolved}: {exc}") from exc


def build_runtime_config(config_data: dict[str, Any]) -> GuardRuntimeConfig:
    sources = config_data.get("sources", {})
    exclusions = config_data.get("exclusions", {})

    suffixes = _merge_values(DEFAULT_SUFFIXES, _normalize_config_list(sources.get("extra_suffixes")))
    filenames = _merge_values(DEFAULT_FILENAMES, _normalize_config_list(sources.get("extra_filenames")))
    prefixes = _merge_values(DEFAULT_EXCLUDED_PREFIXES, _normalize_config_list(exclusions.get("prefixes")))

    return GuardRuntimeConfig(suffixes, filenames, prefixes)


def iter_tracked_files(config: GuardRuntimeConfig) -> Iterable[Path]:
    """Yield tracked files from git, filtering by suffix and exclusions."""

    try:
        result = subprocess.run(
            ["git", "ls-files"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover - defensive
        raise SystemExit(exc.stderr or exc.stdout or exc.returncode) from exc

    for line in result.stdout.splitlines():
        if not line:
            continue
        if any(line.startswith(prefix) for prefix in config.excluded_prefixes):
            continue
        path = Path(line)
        if not path.exists():
            continue
        if path.suffix in config.supported_suffixes or path.name in config.special_filenames:
            yield path


def head_text(path: Path, lines: int = 12) -> str:
    """Return the first `lines` lines of the file as a single string."""

    with path.open("r", encoding="utf-8") as file:
        return "".join(file.readline() for _ in range(lines))


def missing_tokens(text: str, tokens: Sequence[str]) -> list[str]:
    """Return tokens not found in the provided text."""

    return [token for token in tokens if token not in text]


def validate_headers(paths: Iterable[Path]) -> list[tuple[Path, list[str]]]:
    """Validate file headers and report missing tokens."""

    failures: list[tuple[Path, list[str]]] = []
    for path in paths:
        snippet = head_text(path)
        missing = missing_tokens(snippet, HEADER_TOKENS)
        if missing:
            failures.append((path, missing))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Robot Dev Team Project file headers.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Optional TOML file to extend suffixes, filenames, or excluded prefixes "
            "(defaults to config/header_guard.toml when present)."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the files that will be checked without validating.",
    )
    args = parser.parse_args()

    config_data = load_config(args.config)
    runtime_config = build_runtime_config(config_data)

    files = list(iter_tracked_files(runtime_config))
    if args.list:
        for path in files:
            print(path.as_posix())
        return 0

    failures = validate_headers(files)
    if failures:
        for path, missing in failures:
            missing_str = ", ".join(missing)
            print(f"{path.as_posix()}: missing {missing_str}")
        print("", file=sys.stderr)
        print(
            "Run 'python scripts/header_guard.py --list' to inspect the monitored files.",
            file=sys.stderr,
        )
        return 1

    print(f"Verified headers on {len(files)} files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
