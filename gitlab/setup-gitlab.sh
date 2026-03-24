#!/usr/bin/env bash
# Robot Dev Team Project
# File: gitlab/setup-gitlab.sh
# Description: One-time GitLab instance setup for Robot Dev Team integration.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC
#
# Run this script once after:
#   1. GitLab has started and become healthy
#   2. An admin Personal Access Token (api scope) has been created in the UI
#   3. GITLAB_ADMIN_TOKEN has been set in gitlab/.env
#
# Usage (from repository root):
#   bash gitlab/setup-gitlab.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

# --- Load gitlab/.env ---
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found."
  echo "Copy gitlab/.env.example to gitlab/.env and fill in the values first."
  exit 1
fi

set -o allexport
# shellcheck disable=SC1090
source "$ENV_FILE"
set +o allexport

if [[ -z "${GITLAB_ADMIN_TOKEN:-}" || "$GITLAB_ADMIN_TOKEN" == glpat-xxx* ]]; then
  echo "ERROR: GITLAB_ADMIN_TOKEN is not set or is still the placeholder value."
  echo "Create an admin PAT in the GitLab UI and add it to gitlab/.env."
  exit 1
fi

GITLAB_URL="${GITLAB_URL:-http://localhost:8929}"
API="$GITLAB_URL/api/v4"
AUTH=(-H "PRIVATE-TOKEN: $GITLAB_ADMIN_TOKEN")

echo "==> Checking GitLab connectivity at $GITLAB_URL ..."
if ! curl -sf "${AUTH[@]}" "$API/version" > /dev/null; then
  echo "ERROR: Cannot reach GitLab at $GITLAB_URL — is the container running and healthy?"
  exit 1
fi
echo "    OK"

# --- Allow webhooks/hooks to reach local network addresses ---
# Required so GitLab will accept the robot-dev-team webhook URL, which
# resolves to a private Docker network IP (host.docker.internal / 172.x.x.x).
echo "==> Enabling outbound requests to local network from webhooks ..."
RESULT=$(curl -sf -X PUT "${AUTH[@]}" "$API/application/settings" \
  --data "allow_local_requests_from_web_hooks_and_services=true" \
  --data "allow_local_requests_from_system_hooks=true")
ACTUAL=$(echo "$RESULT" | python3 -c "import sys,json; s=json.load(sys.stdin); print(s.get('allow_local_requests_from_web_hooks_and_services','?'))" 2>/dev/null || echo "?")
if [[ "$ACTUAL" == "True" ]]; then
  echo "    OK"
else
  echo "WARNING: Setting may not have applied (got: $ACTUAL). Check manually in Admin > Settings > Network."
fi

echo ""
echo "GitLab setup complete. New projects will now receive automatic webhook configuration."
echo "See docs/GROUP_SETUP.md for next steps (create a group, add agent members)."
