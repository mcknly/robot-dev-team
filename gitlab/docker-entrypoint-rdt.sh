#!/bin/bash
# Robot Dev Team Project
# File: gitlab/docker-entrypoint-rdt.sh
# Description: Entrypoint wrapper for GitLab CE that performs one-time RDT setup.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC
#
# Wraps the standard GitLab entrypoint (/assets/wrapper) to perform
# first-boot setup automatically:
#
#   1. Waits for GitLab to report healthy (/-/health).
#   2. Reads the auto-generated root password from
#      /etc/gitlab/initial_root_password (written by GitLab on first boot).
#   3. Creates an admin Personal Access Token (api scope) via
#      gitlab-rails runner and writes it to /etc/gitlab/rdt.env.
#   4. Enables outbound requests to local network addresses via the admin API
#      (required so the File Hook can register webhooks that resolve to Docker
#      gateway IPs such as host.docker.internal / 172.x.x.x).
#
# On subsequent container starts the setup is skipped because the marker
# file /etc/gitlab/.rdt-setup-done already exists.
#
# If /etc/gitlab/initial_root_password is absent (i.e. a custom
# GITLAB_ROOT_PASSWORD was set), automatic PAT creation is skipped and a
# warning is printed. In that case, create the PAT manually and set
# GITLAB_ADMIN_TOKEN in gitlab/.env, then restart the container.

set -eo pipefail

SETUP_MARKER="/etc/gitlab/.rdt-setup-done"
RDT_ENV_FILE="/etc/gitlab/rdt.env"
GITLAB_URL="http://localhost"

# ---------------------------------------------------------------------------
# Start the standard GitLab entrypoint in the background.
# PID 1 is this wrapper script; all GitLab processes are its children.
# ---------------------------------------------------------------------------
/assets/wrapper &
WRAPPER_PID=$!

# ---------------------------------------------------------------------------
# First-time setup (skipped on subsequent starts)
# ---------------------------------------------------------------------------
if [[ ! -f "$SETUP_MARKER" ]]; then
  echo "[RDT] Waiting for GitLab to become healthy before running first-time setup..."
  until curl -sf "$GITLAB_URL/-/health" > /dev/null 2>&1; do
    if ! kill -0 "$WRAPPER_PID" 2>/dev/null; then
      echo "[RDT] ERROR: GitLab process exited unexpectedly during health wait."
      exit 1
    fi
    sleep 10
  done
  echo "[RDT] GitLab is healthy. Starting first-time setup..."

  PASS_FILE="/etc/gitlab/initial_root_password"

  if [[ ! -f "$PASS_FILE" ]]; then
    # Custom GITLAB_ROOT_PASSWORD was set — we have no way to authenticate
    # programmatically.  Remind the operator to complete setup manually.
    echo "[RDT] WARNING: $PASS_FILE not found."
    echo "[RDT]   This usually means a custom GITLAB_ROOT_PASSWORD was set."
    echo "[RDT]   To enable automatic webhook provisioning:"
    echo "[RDT]     1. Create an admin PAT (api scope) in the GitLab web UI."
    echo "[RDT]     2. Set GITLAB_ADMIN_TOKEN in gitlab/.env."
    echo "[RDT]     3. Restart the container."
    echo "[RDT]   Alternatively, run: bash gitlab/setup-gitlab.sh"
  else
    # ---------------------------------------------------------------------------
    # Create (or retrieve) the admin PAT via Rails runner.
    # find_or_initialize_by makes this idempotent: re-running against an existing
    # 'rdt-admin' token updates it in-place rather than creating a duplicate.
    # ---------------------------------------------------------------------------
    echo "[RDT] Creating admin Personal Access Token..."
    TOKEN=$(gitlab-rails runner "
      user = User.find_by_username('root')
      pat = user.personal_access_tokens.find_or_initialize_by(name: 'rdt-admin')
      pat.scopes = ['api']
      pat.expires_at = nil
      pat.save!
      puts pat.token
    " 2>/dev/null | tail -n1)

    if [[ -z "$TOKEN" || ! "$TOKEN" =~ ^glpat- ]]; then
      echo "[RDT] WARNING: PAT creation returned unexpected output: '${TOKEN}'."
      echo "[RDT]   File Hook webhook provisioning will be unavailable until a valid"
      echo "[RDT]   GITLAB_ADMIN_TOKEN is written to $RDT_ENV_FILE or set in gitlab/.env."
    else
      echo "[RDT] Admin PAT created. Writing to $RDT_ENV_FILE..."
      echo "GITLAB_ADMIN_TOKEN=$TOKEN" > "$RDT_ENV_FILE"
      chmod 600 "$RDT_ENV_FILE"

      # ---------------------------------------------------------------------------
      # Enable outbound requests to local/private network addresses.
      # GitLab blocks these by default. This setting is stored in the database
      # and cannot be configured in gitlab.rb, so it must be applied via the API.
      # ---------------------------------------------------------------------------
      echo "[RDT] Enabling outbound requests to local network addresses..."
      RESULT=$(curl -sf -X PUT \
        -H "PRIVATE-TOKEN: $TOKEN" \
        "$GITLAB_URL/api/v4/application/settings" \
        --data "allow_local_requests_from_web_hooks_and_services=true" \
        --data "allow_local_requests_from_system_hooks=true")
      ACTUAL=$(echo "$RESULT" | python3 -c \
        "import sys,json; s=json.load(sys.stdin); print(s.get('allow_local_requests_from_web_hooks_and_services','?'))" \
        2>/dev/null || echo "?")
      if [[ "$ACTUAL" == "True" ]]; then
        echo "[RDT] Local network webhook delivery enabled."
      else
        echo "[RDT] WARNING: Setting may not have applied (got: $ACTUAL)."
        echo "[RDT]   Check manually in Admin > Settings > Network > Outbound requests."
      fi

      touch "$SETUP_MARKER"
      echo "[RDT] First-time setup complete."
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Hand off to the GitLab process tree and keep the container alive.
# ---------------------------------------------------------------------------
wait $WRAPPER_PID
