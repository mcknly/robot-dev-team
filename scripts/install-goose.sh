#!/usr/bin/env bash
# Robot Dev Team Project
# File: scripts/install-goose.sh
# Description: Install Block Goose CLI (native binary) for goose-* agents.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

# provides: goose

# Goose is a provider-agnostic coding harness. A single `goose` binary backs
# every logical goose-* agent (e.g. goose-gemma); the model is selected per-route
# via --model, and provider + extensions come from the host config dir
# bind-mounted in docker-compose.yml, not from this script -- see
# app/goose_config.py, which materializes that config for the container.
# The preflight gate installs this binary only when *any* goose-* agent is both
# routed and credentialed, so leaving the goose routes commented out is a genuine
# opt-out: the binary is skipped entirely.

set -euo pipefail

INSTALL_DIR="${HOME}/.local/bin"
mkdir -p "$INSTALL_DIR"

# Pull the release binary straight from the `stable` tag rather than piping
# Block's download_cli.sh. That script is itself a release asset, and upstream
# removed it mid-flight: a container that had installed Goose fine an hour
# earlier started failing its boot with `curl: (22) ... 404`. Fetching the
# tarball we actually want removes a moving part we do not control, and skips the
# script's interactive provider-onboarding prompt (which has no terminal to talk
# to at container start) without having to suppress it with CONFIGURE=false.
BASE_URL="https://github.com/block/goose/releases/download/stable"

case "$(uname -m)" in
  x86_64|amd64) TARGET="x86_64-unknown-linux-gnu" ;;
  aarch64|arm64) TARGET="aarch64-unknown-linux-gnu" ;;
  *)
    echo "[install-goose] WARN: unsupported architecture $(uname -m); skipping" >&2
    exit 0
    ;;
esac

echo "[install-goose] Installing Block Goose CLI (native binary, ${TARGET})..."

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# The release carries both compressions. Prefer .tar.gz (gzip is in every base
# image); fall back to .tar.bz2, which is what upstream shipped alone until
# recently -- hence `bzip2` in the Dockerfile's apt list. `tar -xf` sniffs the
# compression either way.
downloaded=""
for ext in tar.gz tar.bz2; do
  if curl -fsSL "${BASE_URL}/goose-${TARGET}.${ext}" -o "${TMP_DIR}/goose.${ext}"; then
    downloaded="${TMP_DIR}/goose.${ext}"
    break
  fi
  echo "[install-goose] goose-${TARGET}.${ext} unavailable; trying next format" >&2
done

if [[ -z "$downloaded" ]]; then
  echo "[install-goose] WARN: failed to download Goose CLI; continuing without it" >&2
  exit 0
fi

if ! tar -xf "$downloaded" -C "$TMP_DIR"; then
  echo "[install-goose] WARN: failed to extract Goose CLI; continuing without it" >&2
  exit 0
fi

if [[ ! -f "${TMP_DIR}/goose" ]]; then
  echo "[install-goose] WARN: no goose binary in the release archive" >&2
  exit 0
fi

install -m 0755 "${TMP_DIR}/goose" "${INSTALL_DIR}/goose"

if command -v goose >/dev/null; then
  echo "[install-goose] Goose CLI installed: $(goose --version 2>/dev/null || echo unknown)"
else
  echo "[install-goose] WARN: goose binary not found on PATH after install" >&2
fi
