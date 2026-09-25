#!/usr/bin/env bash
# Robot Dev Team Project
# File: scripts/install-gemini.sh
# Description: Install Antigravity CLI (agy binary) for the `gemini` agent.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

# provides: agy

# Google's Antigravity CLI (binary name: agy) replaces the deprecated
# @google/gemini-cli npm package. The underlying model is still Gemini, so
# the agent is still referenced as `gemini` throughout the project
# (env vars, route agent identity, GitLab account, bind mount path);
# only the harness binary itself is named `agy`.

set -euo pipefail

INSTALL_DIR="${HOME}/.local/bin"
mkdir -p "$INSTALL_DIR"

echo "[install-gemini] Installing Antigravity CLI (agy) for the gemini agent..."

# Antigravity ships a self-contained shell installer that fetches the
# per-platform binary (verified via SHA512) and drops it into --dir. We
# pipe through `bash -s --` so we can pin the install target to a path
# that is already on the appuser PATH without editing upstream.
if ! curl -fsSL "https://antigravity.google/cli/install.sh" \
     | bash -s -- --dir "$INSTALL_DIR"; then
  echo "[install-gemini] WARN: failed to install Antigravity CLI; continuing without it" >&2
  exit 0
fi

if command -v agy >/dev/null; then
  echo "[install-gemini] Antigravity CLI installed: $(agy --version 2>/dev/null || echo unknown)"
else
  echo "[install-gemini] WARN: agy binary not found on PATH after install" >&2
fi
