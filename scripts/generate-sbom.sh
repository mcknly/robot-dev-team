#!/bin/sh
# Robot Dev Team Project
# File: scripts/generate-sbom.sh
# Description: Generate the SBOM artifact from the built container image.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

set -eu

SYFT_IMAGE="anchore/syft:v1.42.2@sha256:15952b4306fd990724afaaf7f1c71fcd03546b89fbf6f2d32b0be5f81e3ef431"

if ! command -v docker >/dev/null 2>&1; then
  echo "[generate-sbom] docker is required" >&2
  exit 1
fi

project_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_root"

image_ref="${1:-}"
output_path="${2:-sbom/sbom.spdx.json}"

if [ -z "$image_ref" ]; then
  docker compose build app
  image_ref="robot-dev-team-app"
fi
source_name="${3:-$image_ref}"

if ! docker image inspect "$image_ref" >/dev/null 2>&1; then
  echo "[generate-sbom] image is not available locally: $image_ref" >&2
  exit 1
fi

mkdir -p "$(dirname -- "$output_path")"
archive_path="${output_path}.image.tar"
temporary_output="${output_path}.tmp"
container_name="sbom-scan-${CI_JOB_ID:-local}-$$"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  rm -f "$archive_path" "$temporary_output"
}
trap cleanup EXIT INT TERM

echo "[generate-sbom] exporting final image $image_ref..."
docker image save --output "$archive_path" "$image_ref"

docker create \
  --name "$container_name" \
  "$SYFT_IMAGE" \
  docker-archive:/tmp/image.tar \
  --source-name "$source_name" \
  --output spdx-json >/dev/null
docker cp "$archive_path" "$container_name:/tmp/image.tar"

echo "[generate-sbom] scanning the exported image with pinned Syft v1.42.2..."
if ! docker start -a "$container_name" > "$temporary_output"; then
  echo "[generate-sbom] Syft failed to scan $image_ref" >&2
  exit 1
fi

if [ ! -s "$temporary_output" ] || \
   ! grep -Fq '"spdxVersion":"SPDX-2.3"' "$temporary_output"; then
  echo "[generate-sbom] Syft did not produce a valid SPDX JSON document" >&2
  exit 1
fi

mv "$temporary_output" "$output_path"
rm -f "$archive_path"
docker rm "$container_name" >/dev/null

echo "[generate-sbom] sbom written to $output_path"
