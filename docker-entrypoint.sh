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
  # Recursive chown serves two purposes:
  #  1. Reconcile ownership after a UID change (LOCAL_UID at runtime can
  #     differ from the build-time useradd UID).
  #  2. Reset any root-owned dotfiles a previous `docker exec -u root`
  #     (or default-user exec) may have left in $HOME, e.g. ~/.config or
  #     ~/.gitconfig created by an interactive glab invocation. Without
  #     this, the next agent dispatch fails with "permission denied"
  #     when glab-usr tries to write its config (see glab-usr's
  #     self-defense check for the runtime side of this).
  # Errors on bind-mounted dirs (.claude, .gemini, .codex) are expected
  # and silenced; the underlying host filesystem owns those.
  chown -R appuser:appuser /home/appuser /work 2>/dev/null || true

  # Drop privileges and re-exec. setpriv replaces gosu, whose Debian build is a static Go
  # binary linked against an EOL Go 1.19.8 toolchain that Debian will not rebuild (#58).
  # Three details are load-bearing, none of them stylistic:
  #  1. No --reset-env. setpriv would otherwise clear the environment before exec, and the
  #     *_AGENT_GITLAB_TOKEN loop below walks `env` -- it would find nothing and write no
  #     token files, failing every dispatch later rather than here. It would also drop the
  #     venv-first PATH the Dockerfile sets, so `python3 -m app.preflight` runs against the
  #     wrong interpreter. gosu inherited the environment too, so this is parity, not a
  #     shortcut.
  #  2. appuser is named, never resolved to a number. The groupmod/usermod calls above pass
  #     -o, which permits a non-unique id, so a LOCAL_UID colliding with an account already
  #     in the base image would make a uid lookup return the wrong record. --init-groups
  #     then runs initgroups(3) off that same name.
  #  3. --init-groups is mandatory: setpriv rejects --regid outright without one of
  #     --keep-groups/--clear-groups/--init-groups/--groups.
  # --inh-caps=-all clears the inheritable set. Scope the claim honestly: after a uid change
  # with no file capabilities and no ambient set the inheritable set is already empty, so this
  # states the intent rather than closing a hole. It is *not* a bar on regaining privilege --
  # that would be --no-new-privs (setuid/setgid bits, file capabilities) and --bounding-set=-all
  # (the ceiling itself), both deliberately left out because they change the contract every
  # agent subprocess and runtime installer inherits, and that is a separate decision.
  # A consequence worth knowing before deleting this flag: the smoke test's CapInh assertion
  # cannot catch its removal, because CapInh reads empty either way. That assertion witnesses
  # the post-drop capability state, not the presence of the flag, and nothing in CI does.
  exec setpriv --reuid appuser --regid appuser --init-groups --inh-caps=-all "$0" --as-app "$@"
fi

if [[ "${1:-}" == "--as-app" ]]; then
  shift
fi

# HOME arrives from the image (`ENV HOME=/home/appuser`), not from the privilege drop: gosu set
# it for the target user, setpriv deliberately leaves it alone. Root already carries the right
# value, so the fallback below never fires -- but that makes the image's ENV load-bearing. Drop
# it and every agent's credential dir silently resolves under /root.
export HOME=${HOME:-/home/appuser}

# The identity after the drop, stated once where operators and CI can both see it. Without this
# the property is unobservable from outside: there is no USER directive in the image, so a bare
# `docker exec <container> id -u` reports the exec's own root and says nothing about the served
# process. scripts/ci-smoke-image.sh asserts this line.
# stderr is dropped from the name lookup alone: under `docker run --user <uid>` with a uid that
# is not in /etc/passwd -- a mode SECURITY.md documents as supported -- `id -un` still prints the
# number, but warns on the way. The numbers are what matter here and they are read separately, so
# suppress the warning rather than let it into every operator's logs.
echo "[entrypoint] running as $(id -un 2>/dev/null) uid=$(id -u) gid=$(id -g) groups=$(id -G)"

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

echo "[entrypoint] Installing/updating agent CLIs..."

# Ensure user-local bin directory exists (used by the install scripts).
# The default agent CLIs use native installers; the previous npm cache and
# global-prefix plumbing was removed alongside the Node.js runtime when Gemini
# CLI was replaced by the Antigravity binary (`agy`). The one exception is the
# optional Pi harness: when a pi-* route is enabled, scripts/install-pi.sh
# bootstraps a pinned, user-local Node into ~/.local (only `node` is exposed on
# this shared PATH; Pi's npm/npx stay in the versioned Node dir).
mkdir -p "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"

# Validate the agent configuration and derive which harnesses to install.
# The preflight prints one install-script path per line on stdout and fails
# (non-zero) when a route dispatches an agent that has no usable credentials
# -- a route that could never succeed. Failing here is deliberate: the
# alternative is failing later, mid-dispatch, after a webhook has fired.
# Must run *after* the token-file loop above, which the preflight reads.
# To add a new agent CLI, drop a scripts/install-<agent>.sh file into the
# repository with a `# provides: <binary>` line (see docs/ADDING_AN_AGENT.md).
SCRIPT_DIR="/work/scripts"
if ! preflight_output="$(python3 -m app.preflight --scripts-dir "$SCRIPT_DIR")"; then
  echo "[entrypoint] FATAL: agent configuration preflight failed; refusing to start" >&2
  exit 1
fi

# stdout is `key=value` lines: install=<script> and binary=<name>.
installers=()
binaries=()
while IFS= read -r line; do
  case "$line" in
    install=*) installers+=("${line#install=}") ;;
    binary=*) binaries+=("${line#binary=}") ;;
  esac
done <<< "$preflight_output"

# A download failure stays a warning: a flaky CDN is not a misconfiguration.
for installer in "${installers[@]}"; do
  echo "[entrypoint] Running $(basename "$installer")..."
  bash "$installer" || echo "[entrypoint] WARN: $(basename "$installer") exited with errors" >&2
done

# Goose reads its provider wiring (including a local model's base_url) from the
# host config dir, which is bind-mounted read-only. A base_url on loopback is
# correct on the host but points at the container from in here, so materialize a
# container-local copy with loopback redirected at the Docker host gateway. This
# reuses the preflight's gate for free: `binary=goose` is only emitted when a
# goose route is enabled *and* credentialed, so an operator who leaves the goose
# routes commented out pays nothing.
for binary in "${binaries[@]}"; do
  if [[ "$binary" == "goose" ]]; then
    python3 -m app.goose_config || {
      echo "[entrypoint] FATAL: could not materialize the Goose config" >&2
      exit 1
    }
    break
  fi
done

# ...but a harness that is still missing once the installs are done *is* a
# misconfiguration, and every dispatch to that agent would die at exec. This
# is the other half of the preflight invariant: credentials are checked before
# the installs, binaries after. Covers a failed download, a mis-declared
# `# provides:` line, and a BYOA binary that was never baked into the image.
missing=()
for binary in "${binaries[@]}"; do
  command -v "$binary" >/dev/null 2>&1 || missing+=("$binary")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "[entrypoint] FATAL: harness binaries not on PATH after install: ${missing[*]}" >&2
  echo "[entrypoint]        Routes dispatch these agents, so every run would fail at exec." >&2
  echo "[entrypoint]        Check the install script above for download errors, and that its" >&2
  echo "[entrypoint]        '# provides:' line names the binary the route actually runs." >&2
  exit 1
fi

echo "[entrypoint] Starting app: $*"
exec "$@"
