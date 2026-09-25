#!/bin/sh
# Robot Dev Team Project
# File: scripts/ci-build-image.sh
# Description: Build, smoke-test, and conditionally publish the CI container image.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

set -eu

required_variables="CI_REGISTRY_IMAGE CI_COMMIT_SHA CI_DEFAULT_BRANCH"
for variable_name in $required_variables; do
  eval "variable_value=\${$variable_name:-}"
  if [ -z "$variable_value" ]; then
    echo "[ci-image] ERROR: $variable_name is required" >&2
    exit 1
  fi
done

if [ -S /var/run/docker.sock ]; then
  echo "[ci-image] ERROR: host Docker socket is exposed to the CI job" >&2
  exit 1
fi

echo "[ci-image] Waiting for the disposable rootless Docker daemon..."
attempt=0
until docker info >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "[ci-image] ERROR: rootless Docker daemon did not become ready" >&2
    exit 1
  fi
  sleep 1
done

docker info
if ! docker info --format '{{json .SecurityOptions}}' | grep -q rootless; then
  echo "[ci-image] ERROR: disposable Docker daemon is not rootless" >&2
  exit 1
fi
docker buildx version

mkdir -p artifacts
image_ref="${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHA}"

echo "[ci-image] Building ${image_ref} for linux/amd64..."
docker buildx build \
  --platform linux/amd64 \
  --load \
  --metadata-file artifacts/build-metadata.json \
  --tag "$image_ref" \
  .

sh scripts/ci-smoke-image.sh "$image_ref"
sh scripts/generate-sbom.sh "$image_ref" artifacts/sbom.spdx.json

printf 'IMAGE_PUBLISHED=false\n' > artifacts/image.env
printf 'IMAGE_SHA_TAG=%s\n' "$image_ref" >> artifacts/image.env
printf 'Not published: merge-request build %s passed smoke testing.\n' \
  "$image_ref" > artifacts/image-reference.txt

if [ "${CI_COMMIT_BRANCH:-}" != "$CI_DEFAULT_BRANCH" ]; then
  echo "[ci-image] Merge-request image passed; registry publication skipped."
  exit 0
fi

if [ "${CI_COMMIT_REF_PROTECTED:-false}" != "true" ]; then
  echo "[ci-image] ERROR: refusing to publish from an unprotected default branch" >&2
  exit 1
fi

if [ -z "${CI_REGISTRY:-}" ] || [ -z "${CI_REGISTRY_USER:-}" ] || [ -z "${CI_REGISTRY_PASSWORD:-}" ]; then
  echo "[ci-image] ERROR: GitLab registry credentials are unavailable" >&2
  exit 1
fi

printf '%s' "$CI_REGISTRY_PASSWORD" | \
  docker login "$CI_REGISTRY" --username "$CI_REGISTRY_USER" --password-stdin

if docker buildx imagetools inspect "$image_ref" >/dev/null 2>&1; then
  echo "[ci-image] ERROR: immutable SHA tag already exists: $image_ref" >&2
  exit 1
fi

echo "[ci-image] Publishing smoke-tested image ${image_ref}..."
docker push "$image_ref" > artifacts/push-output.txt
cat artifacts/push-output.txt

if ! digest="$(sh "$(dirname "$0")/ci-inspect-pushed-digest.sh" "$image_ref")"; then
  exit 1
fi

image_reference="${CI_REGISTRY_IMAGE}@${digest}"
printf 'IMAGE_PUBLISHED=true\n' > artifacts/image.env
printf 'IMAGE_SHA_TAG=%s\n' "$image_ref" >> artifacts/image.env
printf 'IMAGE_DIGEST=%s\n' "$digest" >> artifacts/image.env
printf 'IMAGE_REFERENCE=%s\n' "$image_reference" >> artifacts/image.env
printf '%s\n' "$image_reference" > artifacts/image-reference.txt

echo "[ci-image] Published immutable deployment reference: $image_reference"
