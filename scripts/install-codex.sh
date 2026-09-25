#!/usr/bin/env bash
# Robot Dev Team Project
# File: scripts/install-codex.sh
# Description: Install Codex CLI (native binary from GitHub Releases).
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

# provides: codex codex-code-mode-host

set -euo pipefail

CODEX_ARCH=$(uname -m)
CODEX_RELEASE_PAGE="https://github.com/openai/codex/releases/latest"
CODEX_RELEASE_URL=""
CODEX_RELEASE_TAG=""
if CODEX_RELEASE_URL=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$CODEX_RELEASE_PAGE"); then
  CODEX_RELEASE_TAG=${CODEX_RELEASE_URL##*/}
fi

if [[ ! "$CODEX_RELEASE_TAG" =~ ^[A-Za-z0-9._-]+$ || "$CODEX_RELEASE_TAG" == "latest" ]]; then
  echo "[install-codex] WARN: failed to resolve the latest Codex release; keeping the existing binary pair" >&2
  exit 0
fi

# Resolve latest once so a release published between downloads cannot pair a
# new CLI with an older code-mode host protocol.
CODEX_DOWNLOAD_ROOT="https://github.com/openai/codex/releases/download/${CODEX_RELEASE_TAG}"
CODEX_URL="${CODEX_DOWNLOAD_ROOT}/codex-${CODEX_ARCH}-unknown-linux-musl.tar.gz"
CODEX_CMH_URL="${CODEX_DOWNLOAD_ROOT}/codex-code-mode-host-${CODEX_ARCH}-unknown-linux-musl.tar.gz"
CODEX_STAGED="/tmp/codex-install/codex-ready"
CODEX_CMH_STAGED="/tmp/codex-cmh-install/codex-code-mode-host-ready"
CODEX_READY=false
CODEX_CMH_READY=false

echo "[install-codex] Installing Codex release ${CODEX_RELEASE_TAG}..."
rm -rf /tmp/codex-install /tmp/codex-cmh-install
mkdir -p /tmp/codex-install /tmp/codex-cmh-install
if curl -fsSL "$CODEX_URL" | tar xz -C /tmp/codex-install; then
  # Tarball contains an arch-suffixed binary; normalize it in the staging dir.
  if mv /tmp/codex-install/codex-*-unknown-linux-musl "$CODEX_STAGED" 2>/dev/null \
    || mv /tmp/codex-install/codex "$CODEX_STAGED" 2>/dev/null; then
    chmod +x "$CODEX_STAGED"
    CODEX_READY=true
  else
    echo "[install-codex] WARN: could not locate codex binary in tarball" >&2
  fi
else
  echo "[install-codex] WARN: failed to download Codex CLI" >&2
fi

# Since 0.147.0 Codex routes tool calls through a separate "code mode" host
# binary that ships as its own release asset -- the codex tarball does not
# contain it. Without it every tool call fails closed ("failed to spawn code-mode
# host"), so a dispatch still exits 0 while never running a command or posting a
# GitLab comment. Install it alongside codex, into its own temp dir so the
# arch-suffixed glob above cannot pick it up.
echo "[install-codex] Installing Codex code-mode host..."
if curl -fsSL "$CODEX_CMH_URL" | tar xz -C /tmp/codex-cmh-install; then
  if mv /tmp/codex-cmh-install/codex-code-mode-host-*-unknown-linux-musl "$CODEX_CMH_STAGED" 2>/dev/null \
    || mv /tmp/codex-cmh-install/codex-code-mode-host "$CODEX_CMH_STAGED" 2>/dev/null; then
    chmod +x "$CODEX_CMH_STAGED"
    CODEX_CMH_READY=true
  else
    echo "[install-codex] WARN: could not locate codex-code-mode-host binary in tarball; codex tool calls will fail closed" >&2
  fi
else
  echo "[install-codex] WARN: failed to download Codex code-mode host; codex tool calls will fail closed" >&2
fi

if $CODEX_READY && $CODEX_CMH_READY; then
  mv "$CODEX_STAGED" "$HOME/.local/bin/codex"
  mv "$CODEX_CMH_STAGED" "$HOME/.local/bin/codex-code-mode-host"
  echo "[install-codex] Codex CLI installed: $(codex --version 2>/dev/null || echo unknown)"
  echo "[install-codex] Codex code-mode host installed from ${CODEX_RELEASE_TAG}"
else
  echo "[install-codex] WARN: incomplete Codex release ${CODEX_RELEASE_TAG}; keeping the existing binary pair" >&2
fi

rm -rf /tmp/codex-install /tmp/codex-cmh-install
