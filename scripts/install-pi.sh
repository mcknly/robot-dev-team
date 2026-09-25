#!/usr/bin/env bash
# Robot Dev Team Project
# File: scripts/install-pi.sh
# Description: Install the Pi coding agent (npm) plus a user-local Node runtime for pi-* agents.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

# provides: pi

# Pi is a provider-agnostic coding harness. A single `pi` binary backs every
# logical pi-* agent (e.g. pi-nemotron); the model is selected per-route via
# --model, and provider auth comes from the host config dir bind-mounted in
# docker-compose.yml (~/.pi/agent/auth.json), not from this script.
#
# Pi is an npm package (@earendil-works/pi-coding-agent) that needs Node >=22.19.0,
# but the base image deliberately ships without Node (it was dropped when Gemini's
# harness became `agy`). To keep that a genuine opt-out, this script bootstraps a
# user-local Node into ~/.local rather than baking Node into the Dockerfile: the
# preflight gate only runs this installer when a pi-* agent is both routed and
# credentialed, so a commented-out pi route costs nothing (no Node, no npm).
#
# Two version policies apply, and they are deliberately different:
#   * Node is version-PINNED and SHA256-verified against the release's
#     SHASUMS256.txt (like the Dockerfile pins glab/syft/uv). A full language
#     runtime warrants a pin, and it is cached across boots -- once the pinned
#     tree exists it is not re-downloaded. See docs/DEPENDENCY_MANAGEMENT.md.
#   * Pi itself tracks LATEST: `npm install -g` runs on every boot (like the
#     Goose/grok installers re-fetch each start), so a restart repairs or updates
#     an existing install rather than freezing it at the first-boot version.

set -euo pipefail

# Pinned Node runtime. Must satisfy the Pi package's engines constraint (>=22.19.0).
NODE_VERSION="22.23.1"
PI_PACKAGE="@earendil-works/pi-coding-agent"

INSTALL_DIR="${HOME}/.local/bin"
NODE_DIR="${HOME}/.local/node-v${NODE_VERSION}"
mkdir -p "$INSTALL_DIR"

# npm must never write into a bind-mounted host dir; keep its cache container-local
# and silence the self-update notifier.
export npm_config_cache="${HOME}/.local/npm-cache"
export npm_config_update_notifier=false

case "$(uname -m)" in
  x86_64|amd64) NODE_ARCH="x64" ;;
  aarch64|arm64) NODE_ARCH="arm64" ;;
  *)
    echo "[install-pi] WARN: unsupported architecture $(uname -m); skipping" >&2
    exit 0
    ;;
esac

# --- Node runtime (pinned, cached across boots) -----------------------------
# Only download when the pinned tree is absent. This is the expensive step, so
# caching it keeps boots fast; Pi (below) still tracks latest every boot.
if [[ -x "${NODE_DIR}/bin/node" ]]; then
  echo "[install-pi] Node v${NODE_VERSION} already present; skipping runtime download"
else
  echo "[install-pi] Installing Node v${NODE_VERSION} (${NODE_ARCH})..."

  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT

  NODE_TARBALL="node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"
  NODE_BASE_URL="https://nodejs.org/dist/v${NODE_VERSION}"

  if ! curl -fsSL "${NODE_BASE_URL}/${NODE_TARBALL}" -o "${TMP_DIR}/${NODE_TARBALL}"; then
    echo "[install-pi] WARN: failed to download Node runtime; continuing without Pi" >&2
    exit 0
  fi

  if ! curl -fsSL "${NODE_BASE_URL}/SHASUMS256.txt" -o "${TMP_DIR}/SHASUMS256.txt"; then
    echo "[install-pi] WARN: failed to download Node checksums; continuing without Pi" >&2
    exit 0
  fi

  # Verify the tarball against the published checksum before trusting it, exactly
  # like the Dockerfile's glab/syft verification (grep the line, then sha256sum -c).
  if ! (cd "$TMP_DIR" && grep " ${NODE_TARBALL}\$" SHASUMS256.txt > node.sha256 && sha256sum -c node.sha256); then
    echo "[install-pi] WARN: Node checksum verification failed; continuing without Pi" >&2
    exit 0
  fi

  # Extract into a versioned dir so a pin bump does not collide with the old tree.
  rm -rf "$NODE_DIR"
  mkdir -p "$NODE_DIR"
  if ! tar -xJf "${TMP_DIR}/${NODE_TARBALL}" -C "$NODE_DIR" --strip-components=1; then
    echo "[install-pi] WARN: failed to extract Node runtime; continuing without Pi" >&2
    exit 0
  fi

  # Sweep any Node trees left by an earlier pin so a long-lived container that
  # reboots across a NODE_VERSION bump does not accumulate stale ~100 MB runtimes.
  for old in "${HOME}"/.local/node-v*; do
    [[ -d "$old" && "$old" != "$NODE_DIR" ]] && rm -rf "$old"
  done
fi

# Expose ONLY `node` on the shared ~/.local/bin PATH. Pi's `#!/usr/bin/env node`
# shim needs it. `npm`/`npx` are deliberately NOT symlinked here: ~/.local/bin is
# on the application-wide PATH (docker-entrypoint.sh), so leaking `npx` would let
# an enabled Goose harness execute host-configured npx-based stdio extensions and
# silence goose_config's "executable not on PATH" warning. Pi's own npm stays in
# NODE_DIR and is used below via a command-scoped PATH, never persisted globally.
ln -sf "${NODE_DIR}/bin/node" "${INSTALL_DIR}/node"

if ! command -v node >/dev/null; then
  echo "[install-pi] WARN: node not found on PATH after install; continuing without Pi" >&2
  exit 0
fi

# --- Pi coding agent (tracks latest, installed every boot) ------------------
# --ignore-scripts matches upstream's own headless command and avoids needing
# build tooling (make/g++); --prefix ~/.local drops the `pi` bin into
# ~/.local/bin (on PATH). npm is invoked via a command-scoped PATH so neither
# `npm` nor `npx` lands on the shared PATH (see the node-only symlink above).
echo "[install-pi] Installing/updating ${PI_PACKAGE} (latest)..."
if ! PATH="${NODE_DIR}/bin:${PATH}" npm install -g --ignore-scripts --prefix "${HOME}/.local" "${PI_PACKAGE}"; then
  echo "[install-pi] WARN: failed to install ${PI_PACKAGE}; continuing without it" >&2
  exit 0
fi

if command -v pi >/dev/null; then
  echo "[install-pi] Pi coding agent installed: $(pi --version 2>/dev/null || echo unknown)"
else
  echo "[install-pi] WARN: pi binary not found on PATH after install" >&2
fi
