#!/usr/bin/env bash
# Robot Dev Team Project
# File: scripts/generate-sbom.sh
# Description: Generate the SBOM artifact from the built container image.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "[generate-sbom] docker is required" >&2
  exit 1
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

docker compose build app
image_id=$(docker image inspect robot-dev-team-app --format '{{.ID}}' 2>/dev/null || true)
if [[ -z "$image_id" ]]; then
  echo "[generate-sbom] unable to determine built image id" >&2
  exit 1
fi

container_name="sbom-extract-$(date +%s)"
docker create --name "$container_name" "$image_id" >/dev/null
trap 'docker rm -f "$container_name" >/dev/null 2>&1 || true' EXIT

docker cp "$container_name:/work/sbom/sbom.spdx.json" sbom/sbom.spdx.json

echo "[generate-sbom] sbom written to sbom/sbom.spdx.json"