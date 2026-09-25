#!/bin/sh
# Robot Dev Team Project
# File: scripts/ci-smoke-image.sh
# Description: Exercise successful health startup and strict preflight failure in an image.
# License: MIT
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 MCKNLY LLC

set -eu

image_ref="${1:-}"
if [ -z "$image_ref" ]; then
  echo "Usage: $0 IMAGE_REFERENCE" >&2
  exit 2
fi

name_prefix="rdt-smoke-${CI_JOB_ID:-local}-$$"
positive_container="${name_prefix}-positive"
negative_container="${name_prefix}-negative"
gidonly_container="${name_prefix}-gidonly"
default_container="${name_prefix}-default"

# Deliberately not the build-time ids. The image's useradd already lands on those, so a smoke run
# that leaves LOCAL_UID/LOCAL_GID unset passes whether the remap ran or not -- it would be
# asserting a default, not a behaviour.
smoke_uid=1234
smoke_gid=5678
# The GID-only case remaps the group while leaving the uid on the build-time default, so the
# entrypoint's usermod branch is skipped and only groupmod runs.
smoke_gidonly_gid=4242

# Read from the image rather than hardcoded, so that changing the Dockerfile's useradd does not
# fail the unremapped cases with a message pointing at the remap instead of at the real cause.
build_uid="$(docker run --rm --entrypoint id "$image_ref" -u appuser)"
build_gid="$(docker run --rm --entrypoint id "$image_ref" -g appuser)"

cleanup() {
  docker rm -f \
    "$positive_container" "$negative_container" "$gidonly_container" \
    "$default_container" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

create_smoke_container() {
  container_name="$1"
  shift
  docker create \
    --name "$container_name" \
    --env ROUTE_CONFIG_PATH=/work/config/routes-ci-smoke.yaml \
    "$@" \
    "$image_ref" >/dev/null
  docker cp \
    tests/fixtures/ci/routes.yaml \
    "$container_name:/work/config/routes-ci-smoke.yaml"
}

# Block until the entrypoint has logged its post-drop identity. Cheaper than waiting for /health
# when the case under test is the drop itself and not whether the app serves.
wait_for_identity() {
  container_name="$1"
  case_label="$2"
  identity_attempt=0
  while [ "$identity_attempt" -lt 60 ]; do
    if docker logs "$container_name" 2>&1 | grep -Fq "[entrypoint] running as"; then
      return 0
    fi

    if [ "$(docker inspect --format '{{.State.Running}}' "$container_name")" != "true" ]; then
      echo "[ci-smoke] ERROR: $case_label container exited before dropping privileges" >&2
      docker logs "$container_name" >&2
      exit 1
    fi

    identity_attempt=$((identity_attempt + 1))
    sleep 1
  done

  echo "[ci-smoke] ERROR: $case_label container never logged a post-drop identity" >&2
  docker logs "$container_name" >&2
  exit 1
}

assert_identity() {
  container_name="$1"
  case_label="$2"
  want_uid="$3"
  want_gid="$4"
  if ! docker logs "$container_name" 2>&1 | grep -Fq \
    "[entrypoint] running as appuser uid=${want_uid} gid=${want_gid}"; then
    echo "[ci-smoke] ERROR: $case_label case expected uid=${want_uid} gid=${want_gid}" >&2
    docker logs "$container_name" >&2
    exit 1
  fi
}

echo "[ci-smoke] Starting positive preflight and health case..."
create_smoke_container \
  "$positive_container" \
  --env CI_SMOKE_AGENT_GITLAB_TOKEN=ci-smoke-token \
  --env CI_SMOKE_AGENT_GIT_EMAIL=ci-smoke@example.invalid \
  --env "LOCAL_UID=$smoke_uid" \
  --env "LOCAL_GID=$smoke_gid"
docker start "$positive_container" >/dev/null

healthy=false
attempt=0
while [ "$attempt" -lt 60 ]; do
  if docker exec "$positive_container" python3 -c '
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=2) as response:
    assert response.status == 200
    assert json.load(response) == {"status": "ok"}
' >/dev/null 2>&1; then
    healthy=true
    break
  fi

  if [ "$(docker inspect --format '{{.State.Running}}' "$positive_container")" != "true" ]; then
    echo "[ci-smoke] ERROR: positive container exited before becoming healthy" >&2
    docker logs "$positive_container" >&2
    exit 1
  fi

  attempt=$((attempt + 1))
  sleep 1
done

if [ "$healthy" != "true" ]; then
  echo "[ci-smoke] ERROR: health endpoint did not become ready" >&2
  docker logs "$positive_container" >&2
  exit 1
fi
echo "[ci-smoke] Positive startup reached /health successfully."

# Serving /health proves the app runs; it does not prove it stopped running as root. Uvicorn
# would answer identically either way, so before this the smoke test would have passed unchanged
# had the privilege drop silently disappeared. Assert the drop against the *served* process:
# there is no USER directive in the image, so `docker exec <container> id -u` reports the exec's
# own root and is not evidence about anything.
echo "[ci-smoke] Asserting the privilege drop on the served process..."
served_pid="$(docker exec "$positive_container" python3 -c '
import os
import sys

expected_uid, expected_gid = sys.argv[1], sys.argv[2]


def read(pid, name):
    with open("/proc/%s/%s" % (pid, name), "rb") as handle:
        return handle.read()


# Matching a bare "uvicorn" anywhere in a cmdline finds tini at PID 1 first, because tini is
# handed the image CMD as its own arguments and so carries the word while still running as root
# by design. The served process is the one that *execed* uvicorn, which is a console script with
# a shebang -- the kernel rewrites it, so argv is "<venv>/python <venv>/uvicorn app.main:app".
# Requiring a path rather than the bare word is what tells the two apart.
target = None
for entry in os.listdir("/proc"):
    if not entry.isdigit() or entry == str(os.getpid()):
        continue
    try:
        argv = [part for part in read(entry, "cmdline").decode().split("\0") if part]
    except OSError:
        # The process exited between listdir and open; it is not the one we want anyway.
        continue
    if any(part.endswith("/uvicorn") for part in argv):
        target = entry
        break

if target is None:
    sys.exit("no uvicorn process found in the container")

fields = {}
for line in read(target, "status").decode().splitlines():
    key, _, value = line.partition(":")
    fields[key] = value.split()

# Real, effective, saved-set and filesystem ids, all four. A drop that moved only the effective
# uid would leave the process able to restore root, and would still satisfy a plain `id -u`.
if set(fields["Uid"]) != {expected_uid}:
    sys.exit("uvicorn Uid is %s, expected all four to be %s" % (fields["Uid"], expected_uid))
if set(fields["Gid"]) != {expected_gid}:
    sys.exit("uvicorn Gid is %s, expected all four to be %s" % (fields["Gid"], expected_gid))

# initgroups(3) ran: with --clear-groups this list would be empty instead.
if expected_gid not in fields["Groups"]:
    sys.exit("uvicorn supplementary groups %s omit %s" % (fields["Groups"], expected_gid))

if int(fields["CapInh"][0], 16) != 0:
    sys.exit("inheritable capability set is %s, expected empty" % fields["CapInh"][0])

sys.stderr.write("[ci-smoke] served process: uid=%s gid=%s groups=%s CapInh=%s\n" % (
    expected_uid, expected_gid, ",".join(fields["Groups"]), fields["CapInh"][0]))
print(target)
' "$smoke_uid" "$smoke_gid")"

# The environment has to survive the re-exec too, and only the served process can answer for it:
# --reset-env would leave the app with no agent token (the entrypoint's token loop reads env)
# and without the venv ahead of PATH, neither of which shows up in a credentials check.
#
# This one runs as the app user rather than root. /proc/<pid>/environ needs PTRACE_MODE_READ,
# and Docker drops CAP_SYS_PTRACE by default, so root in this container cannot read it while the
# owning uid can. That constraint is useful rather than merely tolerated: the read only succeeds
# if the served process really is owned by the uid we expect, so it corroborates the drop from a
# second direction.
docker exec -u "$smoke_uid" "$positive_container" python3 -c '
import sys

pid = sys.argv[1]
with open("/proc/%s/environ" % pid, "rb") as handle:
    environ = dict(
        item.split("=", 1)
        for item in handle.read().decode().split("\0")
        if "=" in item
    )

if environ.get("HOME") != "/home/appuser":
    sys.exit("HOME is %r after the drop, expected /home/appuser" % environ.get("HOME"))

# Not "the venv is first": the entrypoint deliberately prepends ~/.local/bin so the harness
# binaries installed at boot win. The invariant is that the venv still outranks the system
# interpreter, which is what a --reset-env would have destroyed.
path_entries = environ.get("PATH", "").split(":")
for required in ("/opt/venv/bin", "/usr/bin"):
    if required not in path_entries:
        sys.exit("PATH is %r after the drop, expected it to contain %s"
                 % (environ.get("PATH"), required))
if path_entries.index("/opt/venv/bin") > path_entries.index("/usr/bin"):
    sys.exit("PATH is %r after the drop, expected the venv ahead of /usr/bin" % environ["PATH"])

if environ.get("CI_SMOKE_AGENT_GITLAB_TOKEN") != "ci-smoke-token":
    sys.exit("the agent token did not survive the re-exec")
' "$served_pid"

# The same property as operators see it. Asserted separately because the log line is the only
# form of this that survives in `docker logs` after the container is gone.
assert_identity "$positive_container" "positive" "$smoke_uid" "$smoke_gid"

# The token file is written after the drop, so its ownership is a second, independent witness
# that the remap and the drop agree -- and it is what every agent dispatch depends on.
token_owner="$(docker exec "$positive_container" \
  stat -c '%u:%g' /home/appuser/.ci-smoke/glab-token)"
if [ "$token_owner" != "${smoke_uid}:${smoke_gid}" ]; then
  echo "[ci-smoke] ERROR: agent token file is owned by $token_owner," \
    "expected ${smoke_uid}:${smoke_gid}" >&2
  exit 1
fi
echo "[ci-smoke] Privilege drop verified on the served process."

# The image contract the drop depends on. gosu's absence is asserted because its Debian build is
# the EOL-Go-toolchain CVE source this replaced (#58); a reintroduction would be silent.
echo "[ci-smoke] Asserting the privilege-drop image contract..."
docker exec "$positive_container" sh -c '
set -eu
command -v setpriv >/dev/null || { echo "setpriv is not on PATH" >&2; exit 1; }
dpkg -S "$(command -v setpriv)" | grep -q "^util-linux:" || {
  echo "setpriv is not supplied by util-linux" >&2; exit 1; }
if command -v gosu >/dev/null 2>&1; then echo "gosu is still on PATH" >&2; exit 1; fi
if dpkg-query -W -f="\${Status}" gosu 2>/dev/null | grep -q "install ok installed"; then
  echo "gosu is still installed according to dpkg" >&2; exit 1
fi
'
echo "[ci-smoke] setpriv present via util-linux; gosu absent from PATH and dpkg."

# The uid is left on the build-time default here, so the entrypoint's usermod branch never runs
# and groupmod alone has to reconcile both account databases. Covered because the substitution
# changed how the primary group is resolved: gosu read the passwd record, setpriv resolves the
# group by name, and those two only agree while /etc/passwd and /etc/group stay consistent.
echo "[ci-smoke] Starting GID-only remap case..."
create_smoke_container \
  "$gidonly_container" \
  --env CI_SMOKE_AGENT_GITLAB_TOKEN=ci-smoke-token \
  --env CI_SMOKE_AGENT_GIT_EMAIL=ci-smoke@example.invalid \
  --env "LOCAL_GID=$smoke_gidonly_gid"
docker start "$gidonly_container" >/dev/null
wait_for_identity "$gidonly_container" "GID-only"
assert_identity "$gidonly_container" "GID-only" "$build_uid" "$smoke_gidonly_gid"

# Both account databases, not just the resulting process: a passwd record still naming the old
# gid would make the passwd-derived and name-derived answers diverge.
passwd_gid="$(docker exec "$gidonly_container" sh -c \
  'getent passwd appuser | cut -d: -f4')"
group_gid="$(docker exec "$gidonly_container" sh -c \
  'getent group appuser | cut -d: -f3')"
if [ "$passwd_gid" != "$smoke_gidonly_gid" ] || [ "$group_gid" != "$smoke_gidonly_gid" ]; then
  echo "[ci-smoke] ERROR: account databases disagree after a GID-only remap:" \
    "passwd=$passwd_gid group=$group_gid expected=$smoke_gidonly_gid" >&2
  exit 1
fi
echo "[ci-smoke] GID-only remap kept passwd and group consistent at $smoke_gidonly_gid."

# Neither variable set: what a plain `docker compose up` without a configured .env produces, and
# so the most commonly run configuration of the three. The other cases deliberately move off the
# build-time ids to prove the remap happened, which leaves the no-remap path -- where the drop
# still has to work -- covered by nothing. Asserted against the ids read from the image, so this
# stays a statement about the drop rather than about a hardcoded number.
echo "[ci-smoke] Starting default (no remap) case..."
create_smoke_container \
  "$default_container" \
  --env CI_SMOKE_AGENT_GITLAB_TOKEN=ci-smoke-token \
  --env CI_SMOKE_AGENT_GIT_EMAIL=ci-smoke@example.invalid
docker start "$default_container" >/dev/null
wait_for_identity "$default_container" "default"
assert_identity "$default_container" "default" "$build_uid" "$build_gid"
echo "[ci-smoke] Default case dropped to the image's build-time uid=$build_uid gid=$build_gid."

echo "[ci-smoke] Starting missing-credential rejection case..."
create_smoke_container "$negative_container"
docker start "$negative_container" >/dev/null

negative_stopped=false
attempt=0
while [ "$attempt" -lt 30 ]; do
  if [ "$(docker inspect --format '{{.State.Running}}' "$negative_container")" != "true" ]; then
    negative_stopped=true
    break
  fi

  attempt=$((attempt + 1))
  sleep 1
done

if [ "$negative_stopped" != "true" ]; then
  echo "[ci-smoke] ERROR: invalid configuration did not exit within 30 seconds" >&2
  docker logs "$negative_container" >&2
  exit 1
fi

negative_exit="$(docker inspect --format '{{.State.ExitCode}}' "$negative_container")"
negative_logs="$(docker logs "$negative_container" 2>&1)"
printf '%s\n' "$negative_logs"

if [ "$negative_exit" -eq 0 ]; then
  echo "[ci-smoke] ERROR: invalid configuration exited successfully" >&2
  exit 1
fi

if ! printf '%s\n' "$negative_logs" | grep -Fq "agent configuration preflight failed"; then
  echo "[ci-smoke] ERROR: expected preflight rejection message was not logged" >&2
  exit 1
fi

if printf '%s\n' "$negative_logs" | grep -Fq "Uvicorn running"; then
  echo "[ci-smoke] ERROR: Uvicorn started after preflight rejection" >&2
  exit 1
fi

echo "[ci-smoke] Invalid configuration was rejected before Uvicorn startup."
