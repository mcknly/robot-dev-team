#!/usr/bin/env bash
# Robot Dev Team Project
# File: scripts/install-claude.sh
# Description: Install Claude Code CLI (native installer).
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

set -euo pipefail

echo "[install-claude] Installing Claude Code (native installer)..."
if ! curl -fsSL https://claude.ai/install.sh | bash; then
  echo "[install-claude] WARN: failed to install Claude Code; continuing without it" >&2
  exit 0
fi
if command -v claude >/dev/null; then
  echo "[install-claude] Claude Code installed: $(claude --version 2>/dev/null || echo unknown)"
fi
