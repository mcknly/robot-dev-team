#!/usr/bin/env bash
# Robot Dev Team Project
# File: scripts/install-opencode.sh
# Description: Install OpenCode CLI (native binary) for opencode-* agents.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

# provides: opencode

# OpenCode is a provider-agnostic coding harness. A single `opencode` binary
# backs every logical opencode-* agent (e.g. opencode-kimi); the model and
# provider are selected per-route via --model and the mounted host config.
# The preflight gate installs this binary when *any* opencode-* agent is both
# routed and credentialed, so leaving the opencode routes commented out is now
# a genuine opt-out: the binary is skipped entirely.

set -euo pipefail

INSTALL_DIR="${HOME}/.local/bin"
mkdir -p "$INSTALL_DIR"

echo "[install-opencode] Installing OpenCode CLI (native binary)..."

# OpenCode ships a self-contained shell installer that fetches the
# per-platform binary. We install to the default $HOME/.opencode/bin and
# symlink the binary onto the appuser PATH; keeping the default target
# means container rebuilds don't drift from a stock `curl | bash` install.
# No npm fallback: the image intentionally ships without Node.js, so we
# avoid reintroducing that dependency.
if ! curl -fsSL "https://opencode.ai/install" | bash; then
  echo "[install-opencode] WARN: failed to install OpenCode CLI; continuing without it" >&2
  exit 0
fi

# Link the installed binary into a directory already on PATH.
if [[ -x "${HOME}/.opencode/bin/opencode" ]]; then
  ln -sf "${HOME}/.opencode/bin/opencode" "${INSTALL_DIR}/opencode"
fi

if command -v opencode >/dev/null; then
  echo "[install-opencode] OpenCode CLI installed: $(opencode --version 2>/dev/null || echo unknown)"
else
  echo "[install-opencode] WARN: opencode binary not found on PATH after install" >&2
fi
