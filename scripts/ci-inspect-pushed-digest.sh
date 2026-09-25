#!/bin/sh
# Robot Dev Team Project
# File: scripts/ci-inspect-pushed-digest.sh
# Description: Resolve and validate the registry digest for a pushed CI image.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

set -eu

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "usage: ci-inspect-pushed-digest.sh IMAGE_REFERENCE" >&2
  exit 2
fi

image_ref="$1"
manifest_file="$(mktemp)"
cleanup() {
  rm -f "$manifest_file"
}
trap cleanup 0
trap 'exit 1' HUP INT TERM

# A registry digest is the SHA-256 of the exact manifest bytes. The raw form
# avoids Buildx template-field differences across supported client versions.
if ! docker buildx imagetools inspect --raw "$image_ref" > "$manifest_file"; then
  echo "[ci-image] ERROR: pushed manifest could not be inspected" >&2
  exit 1
fi

if [ ! -s "$manifest_file" ]; then
  echo "[ci-image] ERROR: pushed manifest digest was not found" >&2
  exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
  digest_hash="$(sha256sum "$manifest_file")"
elif command -v shasum >/dev/null 2>&1; then
  digest_hash="$(shasum -a 256 "$manifest_file")"
else
  echo "[ci-image] ERROR: sha256sum or shasum is required" >&2
  exit 1
fi
digest_hash="${digest_hash%% *}"
digest="sha256:${digest_hash}"
if [ "${#digest}" -ne 71 ] || ! printf '%s\n' "$digest" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
  echo "[ci-image] ERROR: pushed manifest digest was invalid" >&2
  exit 1
fi

printf '%s\n' "$digest"
