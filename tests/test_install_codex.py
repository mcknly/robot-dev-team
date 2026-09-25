"""Robot Dev Team Project
File: tests/test_install_codex.py
Description: Behavioral coverage for the paired Codex binary installer.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import io
import os
import subprocess
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "scripts" / "install-codex.sh"
RELEASE_TAG = "rust-v0.147.0"


def _archive(path: Path, name: str, output: str) -> None:
    payload = f"#!/bin/sh\nprintf '%s\\n' '{output}'\n".encode()
    info = tarfile.TarInfo(name)
    info.mode = 0o755
    info.size = len(payload)
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))


def _installer_env(tmp_path: Path, *, fail_host: bool = False) -> tuple[dict, Path]:
    arch = os.uname().machine
    cli_archive = tmp_path / "codex.tar.gz"
    host_archive = tmp_path / "codex-code-mode-host.tar.gz"
    _archive(cli_archive, f"codex-{arch}-unknown-linux-musl", "codex-test")
    _archive(
        host_archive,
        f"codex-code-mode-host-{arch}-unknown-linux-musl",
        "host-test",
    )

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    curl_log = tmp_path / "curl.log"
    curl = stub_bin / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
set -eu
url="${!#}"
printf '%s\n' "$url" >> "$CODEX_TEST_CURL_LOG"
case "$url" in
  https://github.com/openai/codex/releases/latest)
    printf '%s' "https://github.com/openai/codex/releases/tag/$CODEX_TEST_RELEASE_TAG"
    ;;
  *codex-code-mode-host-*)
    if [[ "${CODEX_TEST_FAIL_HOST:-0}" == "1" ]]; then
      exit 22
    fi
    /bin/cat "$CODEX_TEST_HOST_ARCHIVE"
    ;;
  *codex-*)
    /bin/cat "$CODEX_TEST_CLI_ARCHIVE"
    ;;
  *)
    exit 22
    ;;
esac
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        {
            "CODEX_TEST_CLI_ARCHIVE": str(cli_archive),
            "CODEX_TEST_CURL_LOG": str(curl_log),
            "CODEX_TEST_FAIL_HOST": "1" if fail_host else "0",
            "CODEX_TEST_HOST_ARCHIVE": str(host_archive),
            "CODEX_TEST_RELEASE_TAG": RELEASE_TAG,
            "HOME": str(home),
            "PATH": f"{stub_bin}:{os.environ['PATH']}",
        }
    )
    return env, curl_log


def test_installs_cli_and_host_from_one_resolved_release(tmp_path):
    env, curl_log = _installer_env(tmp_path)

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    install_dir = Path(env["HOME"]) / ".local" / "bin"
    assert (install_dir / "codex").read_text(encoding="utf-8").endswith(
        "codex-test'\n"
    )
    assert (install_dir / "codex-code-mode-host").read_text(
        encoding="utf-8"
    ).endswith("host-test'\n")
    assert f"Codex code-mode host installed from {RELEASE_TAG}" in result.stdout

    requested = curl_log.read_text(encoding="utf-8").splitlines()
    assert requested[0] == "https://github.com/openai/codex/releases/latest"
    assert len(requested) == 3
    assert all(f"/download/{RELEASE_TAG}/" in url for url in requested[1:])


def test_failed_host_download_keeps_stale_pair_without_success_message(tmp_path):
    env, _ = _installer_env(tmp_path, fail_host=True)
    install_dir = Path(env["HOME"]) / ".local" / "bin"
    cli = install_dir / "codex"
    host = install_dir / "codex-code-mode-host"
    cli.write_text("old-cli\n", encoding="utf-8")
    host.write_text("old-host\n", encoding="utf-8")
    cli.chmod(0o755)
    host.chmod(0o755)

    result = subprocess.run(
        ["bash", str(INSTALLER)],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )

    assert cli.read_text(encoding="utf-8") == "old-cli\n"
    assert host.read_text(encoding="utf-8") == "old-host\n"
    assert "Codex CLI installed:" not in result.stdout
    assert "Codex code-mode host installed" not in result.stdout
    assert "codex tool calls will fail closed" in result.stderr
    assert "keeping the existing binary pair" in result.stderr
