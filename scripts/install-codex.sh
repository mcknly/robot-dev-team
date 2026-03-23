#!/usr/bin/env bash
# Robot Dev Team Project
# File: scripts/install-codex.sh
# Description: Install Codex CLI (native binary from GitHub Releases).
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

set -euo pipefail

echo "[install-codex] Installing Codex CLI (native binary)..."
CODEX_ARCH=$(uname -m)
CODEX_URL="https://github.com/openai/codex/releases/latest/download/codex-${CODEX_ARCH}-unknown-linux-musl.tar.gz"
mkdir -p /tmp/codex-install
if curl -fsSL "$CODEX_URL" | tar xz -C /tmp/codex-install; then
  # Tarball contains arch-suffixed binary; rename to 'codex'
  mv /tmp/codex-install/codex-*-unknown-linux-musl "$HOME/.local/bin/codex" 2>/dev/null \
    || mv /tmp/codex-install/codex "$HOME/.local/bin/codex" 2>/dev/null \
    || echo "[install-codex] WARN: could not locate codex binary in tarball" >&2
else
  echo "[install-codex] WARN: failed to install Codex CLI; continuing without it" >&2
fi
rm -rf /tmp/codex-install
if command -v codex >/dev/null; then
  echo "[install-codex] Codex CLI installed: $(codex --version 2>/dev/null || echo unknown)"
fi
