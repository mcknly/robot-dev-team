#!/usr/bin/env bash
# Robot Dev Team Project
# File: scripts/install-grok.sh
# Description: Install xAI Grok Build CLI (native binary) for the grok agent.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

# provides: grok

# Grok Build is a cloud harness: the model runs at xAI, and credentials come from
# the host config dir (~/.grok/auth.json, written by `grok login`) bind-mounted in
# docker-compose.yml -- this script installs the binary and nothing else. The
# preflight gate installs it only when the grok agent is both routed and
# credentialed, so leaving the grok routes commented out is a genuine opt-out.

set -euo pipefail

INSTALL_DIR="${HOME}/.local/bin"
mkdir -p "$INSTALL_DIR"

# Fetch the release artifact directly instead of piping https://x.ai/cli/install.sh.
# Beyond the standing preference for artifacts over vendor scripts, that script has
# three side effects we actively do not want in a container: it symlinks a bare
# `agent` command onto PATH (hopelessly ambiguous in a repo whose whole vocabulary
# is "agents"), it appends a block to ~/.bashrc / ~/.zshrc, and it stages downloads
# in ~/.grok -- which is bind-mounted from the host, so all of that would land in
# the operator's home directory.
#
# The artifact layout is a two-step: `<base>/stable` returns the current version
# string, and `<base>/grok-<version>-linux-<arch>` is the raw binary (no archive).
# x.ai is Cloudflare-fronted; GCS is upstream's own fallback for when it is
# unreachable. No version is pinned -- see docs/DEPENDENCY_MANAGEMENT.md, every
# harness tracks latest on each container start.
#
# The binary lands in $HOME/.local/bin, which is container-local. That matters:
# grok's own updater stages into $HOME/.grok/downloads and repoints
# $HOME/.grok/bin/{grok,agent} at whatever it fetched -- and $HOME/.grok is
# bind-mounted from the host. So the routes pass `--no-auto-update` to keep a
# container-side update from writing a ~150 MB binary into the operator's home and
# flipping the symlink their *host* grok resolves through. Tracking latest is this
# script's job, once per boot, not the running agent's.
BASE_URL_PRIMARY="https://x.ai/cli"
BASE_URL_FALLBACK="https://storage.googleapis.com/grok-build-public-artifacts/cli"
CHANNEL="stable"

case "$(uname -m)" in
  x86_64|amd64) ARCH="x86_64" ;;
  aarch64|arm64) ARCH="aarch64" ;;
  *)
    echo "[install-grok] WARN: unsupported architecture $(uname -m); skipping" >&2
    exit 0
    ;;
esac

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# Try each source end to end -- version probe *and* binary fetch -- before moving
# on. Selecting the base on the probe alone would leave the mirror unused in the
# likelier failure: the small version file serves fine while the ~150 MB binary
# fetch dies on a transient 5xx or bot mitigation.
#
# The version string is validated before it reaches a URL. `curl -f` only fails on
# an HTTP error status, so a challenge page or redirect served with 200 would
# otherwise be pasted into the artifact path and surface as a confusing 404 on the
# download rather than an obvious problem with the version endpoint.
downloaded=""
for base in "$BASE_URL_PRIMARY" "$BASE_URL_FALLBACK"; do
  version="$(curl -fsSL "${base}/${CHANNEL}" 2>/dev/null | tr -d '[:space:]')" || {
    echo "[install-grok] ${base}/${CHANNEL} unreachable; trying next source" >&2
    continue
  }
  if [[ ! "$version" =~ ^[0-9]+(\.[0-9]+)+(-[A-Za-z0-9.]+)?$ ]]; then
    echo "[install-grok] ${base}/${CHANNEL} did not return a version string; trying next source" >&2
    continue
  fi
  echo "[install-grok] Installing Grok Build CLI ${version} (native binary, linux-${ARCH})..."
  if curl -fsSL "${base}/grok-${version}-linux-${ARCH}" -o "${TMP_DIR}/grok"; then
    downloaded="yes"
    break
  fi
  echo "[install-grok] failed to download grok-${version}-linux-${ARCH} from ${base}; trying next source" >&2
done

if [[ -z "$downloaded" ]]; then
  echo "[install-grok] WARN: failed to install Grok CLI; continuing without it" >&2
  exit 0
fi

install -m 0755 "${TMP_DIR}/grok" "${INSTALL_DIR}/grok"

if command -v grok >/dev/null; then
  echo "[install-grok] Grok CLI installed: $(grok --version 2>/dev/null || echo unknown)"
else
  echo "[install-grok] WARN: grok binary not found on PATH after install" >&2
fi
