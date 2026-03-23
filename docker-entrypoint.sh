#!/usr/bin/env bash
# Robot Dev Team Project
# File: docker-entrypoint.sh
# Description: Container entrypoint configuring agent credentials and permissions.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

set -euo pipefail

if [[ "${1:-}" != "--as-app" && "$(id -u)" -eq 0 ]]; then
  TARGET_UID=${LOCAL_UID:-10001}
  TARGET_GID=${LOCAL_GID:-10001}

  if getent group appuser >/dev/null 2>&1; then
    CURRENT_GID=$(getent group appuser | cut -d: -f3)
    if [[ "$CURRENT_GID" != "$TARGET_GID" ]]; then
      groupmod -o -g "$TARGET_GID" appuser
    fi
  else
    groupadd -o -g "$TARGET_GID" appuser
  fi

  if id appuser >/dev/null 2>&1; then
    CURRENT_UID=$(id -u appuser)
    if [[ "$CURRENT_UID" != "$TARGET_UID" ]]; then
      usermod -o -u "$TARGET_UID" -g "$TARGET_GID" appuser
    fi
  else
    useradd -m -u "$TARGET_UID" -g "$TARGET_GID" appuser
  fi

  mkdir -p /home/appuser
  chown -R appuser:appuser /home/appuser /work 2>/dev/null || true

  exec gosu appuser "$0" --as-app "$@"
fi

if [[ "${1:-}" == "--as-app" ]]; then
  shift
fi

export HOME=${HOME:-/home/appuser}

write_token_file() {
  local token="$1"
  local path="$2"

  if [[ -n "$token" ]]; then
    local previous_umask
    previous_umask=$(umask)
    mkdir -p "$(dirname "$path")"
    umask 077
    printf '%s' "$token" > "$path"
    chmod 600 "$path"
    umask "$previous_umask"
  else
    rm -f "$path"
  fi
}

# Write token files for all agents that have *_AGENT_GITLAB_TOKEN set.
# Convention: env var FOO_AGENT_GITLAB_TOKEN -> token file ~/.<agent>/glab-token
# The agent directory name is the lowercase form with underscores replaced by hyphens.
while IFS='=' read -r var_name var_value; do
  case "$var_name" in
    *_AGENT_GITLAB_TOKEN)
      agent_prefix="${var_name%_AGENT_GITLAB_TOKEN}"
      agent_dir="$(printf '%s' "$agent_prefix" | tr '[:upper:]_' '[:lower:]-')"
      write_token_file "$var_value" "$HOME/.$agent_dir/glab-token"
      ;;
  esac
done < <(env)

DEFAULT_CACHE="/work/.npm-cache"
REQUESTED_CACHE="${NPM_CONFIG_CACHE:-$DEFAULT_CACHE}"
mkdir -p "$REQUESTED_CACHE" || true

CACHE_TEST_FILE="$REQUESTED_CACHE/.perm_check"
if ! touch "$CACHE_TEST_FILE" 2>/dev/null; then
  FALLBACK_CACHE="/tmp/npm-cache"
  mkdir -p "$FALLBACK_CACHE"
  export NPM_CONFIG_CACHE="$FALLBACK_CACHE"
  echo "[entrypoint] WARN: unable to write to $REQUESTED_CACHE; using $FALLBACK_CACHE instead" >&2
else
  rm -f "$CACHE_TEST_FILE"
  export NPM_CONFIG_CACHE="$REQUESTED_CACHE"
fi

echo "[entrypoint] Installing/updating agent CLIs..."

# npm global prefix (still needed for npm-based agent CLIs)
GLOBAL_PREFIX="${NPM_GLOBAL_PREFIX:-$HOME/.npm-global}"
mkdir -p "$GLOBAL_PREFIX/bin"
export NPM_CONFIG_PREFIX="$GLOBAL_PREFIX"
export PATH="$HOME/.local/bin:$GLOBAL_PREFIX/bin:$PATH"

# Ensure user-local bin directory exists (used by native installers)
mkdir -p "$HOME/.local/bin"

# Run all install scripts found under scripts/install-*.sh.
# To add a new agent CLI, drop a scripts/install-<agent>.sh file into the
# repository (see docs/ADDING_AN_AGENT.md).
SCRIPT_DIR="/work/scripts"
for installer in "$SCRIPT_DIR"/install-*.sh; do
  [[ -f "$installer" ]] || continue
  echo "[entrypoint] Running $(basename "$installer")..."
  bash "$installer" || echo "[entrypoint] WARN: $(basename "$installer") exited with errors" >&2
done

echo "[entrypoint] Starting app: $*"
exec "$@"
