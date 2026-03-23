#!/usr/bin/env bash
# Robot Dev Team Project
# File: scripts/install-gemini.sh
# Description: Install Gemini CLI (npm package).
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

set -euo pipefail

echo "[install-gemini] Installing Gemini CLI (npm)..."
if ! npm install -g "@google/gemini-cli@latest"; then
  echo "[install-gemini] WARN: failed to install Gemini CLI; continuing without it" >&2
  exit 0
fi
if command -v gemini >/dev/null; then
  echo "[install-gemini] Gemini CLI installed: $(gemini --version 2>/dev/null || echo unknown)"
fi
