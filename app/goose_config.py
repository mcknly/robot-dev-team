"""Robot Dev Team Project
File: app/goose_config.py
Description: Materialize a container-local Goose config, redirecting loopback URLs.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

# Goose keeps its provider wiring in the host config dir: `config.yaml` names the
# active provider, and `custom_providers/*.json` carries that provider's
# `base_url`. Bind-mounting the dir is what makes a working host setup work in
# the container -- except for one value. An operator whose Goose talks to a
# locally served model (llama.cpp, Ollama, LM Studio, vLLM) has an endpoint on
# loopback, and inside the container `127.0.0.1` is the container itself.
#
# So the mounted config is treated as read-only *source* and copied to the real
# config path with loopback hosts redirected at the Docker host gateway. The
# host's own config is never touched: it stays correct for running Goose on the
# host, and the container gets a config that is correct for the container.
#
# The alternative -- a stack of Goose-specific env overrides in `.env`
# (GOOSE_PROVIDER, OPENAI_HOST, OPENAI_API_KEY, GOOSE_DISABLE_KEYRING) -- forces
# the operator to restate provider config the mounted dir already carries, and
# only works for Goose's built-in providers, not custom ones.
DEFAULT_SOURCE_DIR = "~/.config/goose-host"
DEFAULT_TARGET_DIR = "~/.config/goose"

# docker-compose.yml declares `extra_hosts: host.docker.internal:host-gateway`,
# which resolves to the host from inside the container on Linux and is provided
# natively by Docker Desktop.
DEFAULT_GATEWAY_HOST = "host.docker.internal"

# Goose refuses to run without this; its absence means the mount is not actually
# a Goose config dir.
CONFIG_FILENAME = "config.yaml"

# Where Goose keeps user-defined providers -- including the `base_url` of a
# locally served model, which is the value the loopback rewrite exists for.
CUSTOM_PROVIDERS_DIRNAME = "custom_providers"

# Match only the authority of an http(s) URL -- scheme, optional userinfo, host,
# optional port -- and stop there. Path/query/fragment are never consumed, so
# they cannot be mangled and trailing punctuation (a closing JSON quote, a YAML
# comma) needs no special handling. Rewriting is textual rather than a
# YAML/JSON round-trip so comments, key order, and formatting survive verbatim.
URL_AUTHORITY_PATTERN = re.compile(
    r"""(?P<scheme>https?://)
        (?P<userinfo>[^/?\#@\s"']*@)?
        (?P<host>\[[0-9A-Fa-f:.]+\]|[^/?\#:\s"']+)
        (?P<port>:\d+)?
    """,
    re.VERBOSE,
)

# Not every Goose endpoint is spelled as a URL. The provider-specific host
# variables (OLLAMA_HOST, OPENAI_HOST, ...) also accept a *bare* host with no
# scheme -- Goose prepends one itself. Verified against goose 1.41.0:
# `OLLAMA_HOST: host.docker.internal:10000` connects. A scheme-only rewrite
# would leave `OLLAMA_HOST: localhost:11434` untouched, the container would
# resolve `localhost` to itself, and the operator would get connection-refused
# with the log cheerfully reporting "no loopback URLs found".
#
# Scoped to keys ending in _HOST so an arbitrary bare word (a model name, a
# provider name) is never mistaken for an endpoint.
BARE_HOST_PATTERN = re.compile(
    r"""^(?P<prefix>\s*["']?[A-Za-z_][A-Za-z0-9_]*_HOST["']?\s*:\s*["']?)
        (?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.\-]+)
        (?P<port>:\d+)?
        (?P<suffix>["']?,?[ \t]*)$
    """,
    re.VERBOSE | re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class Rewrite:
    """A single loopback endpoint that was redirected at the host gateway."""

    path: Path
    before: str
    after: str


def is_loopback_host(host: str) -> bool:
    """True when ``host`` addresses the local machine rather than a peer.

    Covers the literal name ``localhost``, the whole 127.0.0.0/8 block (not just
    127.0.0.1 -- a systemd-resolved setup may use 127.0.0.53), IPv6 ``::1``, and
    the unspecified addresses ``0.0.0.0`` / ``::``, which a server binds to mean
    "all interfaces" and operators routinely paste into a client URL.
    """

    candidate = host.strip().strip("[]")
    if not candidate:
        return False
    if candidate.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def rewrite_text(text: str, gateway: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Redirect every loopback endpoint in ``text`` at ``gateway``.

    Handles both spellings Goose accepts: a full ``http(s)://`` URL (a custom
    provider's ``base_url``) and a bare ``*_HOST`` value with no scheme. Only the
    host is swapped; scheme, userinfo, port, and everything after the authority
    are preserved. Non-loopback hosts (api.openai.com) and strings that are not
    endpoints are left exactly as found.

    Note this applies to every UTF-8 file in the config dir, ``secrets.yaml``
    included. That is deliberate -- a self-hosted endpoint may carry credentials
    -- and is why the secrets test asserts verbatim copying of a *URL-free*
    secrets file rather than promising secrets are exempt from rewriting.
    """

    replacements: List[Tuple[str, str]] = []

    def _replace_url(match: re.Match[str]) -> str:
        if not is_loopback_host(match.group("host")):
            return match.group(0)
        rewritten = "{scheme}{userinfo}{host}{port}".format(
            scheme=match.group("scheme"),
            userinfo=match.group("userinfo") or "",
            host=gateway,
            port=match.group("port") or "",
        )
        replacements.append((match.group(0), rewritten))
        return rewritten

    def _replace_bare_host(match: re.Match[str]) -> str:
        host = match.group("host")
        # A value the URL pass already handled (or any scheme-bearing value) is
        # not a bare host; leave it alone rather than rewriting `http` as a host.
        if "//" in match.group(0) or not is_loopback_host(host):
            return match.group(0)
        port = match.group("port") or ""
        rewritten = "{prefix}{host}{port}{suffix}".format(
            prefix=match.group("prefix"),
            host=gateway,
            port=port,
            suffix=match.group("suffix"),
        )
        replacements.append((f"{host}{port}", f"{gateway}{port}"))
        return rewritten

    text = URL_AUTHORITY_PATTERN.sub(_replace_url, text)
    text = BARE_HOST_PATTERN.sub(_replace_bare_host, text)
    return text, replacements


def local_streaming_warnings(config_dir: Path, gateway: str) -> List[str]:
    """Flag a locally served provider that still has streaming enabled.

    llama.cpp's server does not emit valid OpenAI SSE when a response carries a
    tool call: the stream breaks at the tool-call boundary. Goose then
    deserializes the server's mid-stream error event as an ordinary chunk, fails
    because it has no `choices` field, and reports an opaque "Stream decode
    error" that discards what the server actually said (goose#8021,
    llama.cpp#12601).

    That failure is close to invisible from the outside -- the agent burns
    minutes of GPU, llama-server logs nothing wrong and releases its slot
    normally, the inactivity watchdog never fires because the process exits on
    its own, and no comment is ever posted. It is intermittent too: only turns
    that emit a tool call trip it, so a short "confirm you got this" reply
    succeeds while a real task fails. Nobody is going to guess that from the
    symptom, so say it at boot.

    A warning rather than a fatal: other locally served backends (Ollama, vLLM,
    LM Studio) may stream tool calls correctly, and this cannot tell which server
    is behind the endpoint. A provider addressing the host gateway is only
    *suspicious*, not wrong.
    """

    providers_dir = config_dir / CUSTOM_PROVIDERS_DIRNAME
    if not providers_dir.is_dir():
        return []

    warnings: List[str] = []
    for path in sorted(providers_dir.glob("*.json")):
        try:
            provider = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(provider, dict):
            continue

        base_url = provider.get("base_url")
        if not isinstance(base_url, str):
            continue
        # The endpoint is local precisely when it addresses the Docker host --
        # either because materialize() just rewrote a loopback host, or because
        # the operator wrote the gateway in by hand.
        match = URL_AUTHORITY_PATTERN.match(base_url)
        host = match.group("host") if match else ""
        if host != gateway and not is_loopback_host(host):
            continue

        if provider.get("supports_streaming") is False:
            continue

        name = provider.get("name") or path.stem
        warnings.append(
            f"provider '{name}' serves a model on the host ({base_url}) with "
            f"streaming enabled. If that endpoint is llama.cpp, every turn that "
            f"emits a tool call will die with an opaque 'Stream decode error' "
            f"after minutes of inference, and the agent will post nothing -- the "
            f"server logs no error and the watchdog does not fire, so the run "
            f"just vanishes. Set \"supports_streaming\": false in "
            f"{path.name} on the host. (goose#8021; harmless to ignore if the "
            f"endpoint is a server that streams tool calls correctly.)"
        )
    return warnings


def stdio_extension_warnings(config_dir: Path) -> List[str]:
    """Flag stdio extensions whose executable is not present in the container.

    The bind mount carries an extension's *config*, not its *executable*. A
    `type: stdio` extension launched with `npx ...` works on the host and cannot
    work here -- the image ships no Node.js (it was dropped when Gemini's harness
    became `agy`). Goose surfaces that as a failure deep in extension startup at
    first dispatch; catching it at boot turns a confusing runtime error into one
    startup line.
    """

    config = config_dir / CONFIG_FILENAME
    try:
        parsed = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(parsed, dict):
        return []

    extensions = parsed.get("extensions")
    if not isinstance(extensions, dict):
        return []

    warnings: List[str] = []
    for name, spec in extensions.items():
        if not isinstance(spec, dict) or not spec.get("enabled"):
            continue
        if spec.get("type") != "stdio":
            continue
        command = spec.get("cmd")
        if not isinstance(command, str) or not command.strip():
            continue
        executable = command.split()[0]
        if shutil.which(executable) is None:
            warnings.append(
                f"extension '{name}' runs '{executable}', which is not on PATH "
                f"in this container; it works on the host but will fail at "
                f"dispatch. The bind mount carries extension config, not the "
                f"executables it names (the image ships no Node.js, so npx-based "
                f"MCP extensions cannot run here)."
            )
    return warnings


def _validate_materialize_paths(source: Path, target: Path) -> None:
    """Refuse to materialize when it would damage the host's Goose config.

    Guards, in order: (1) target must not be the source itself; (2) target must
    not be a mount point (a bind-mounted host config would be deleted by the
    ``rmtree(target)`` below); (3) source must actually contain a config file.
    """
    if source.resolve() == target.resolve():
        raise ValueError(
            f"refusing to materialize {source} onto itself; the mounted Goose "
            f"config must be a separate read-only path (see docker-compose.yml)"
        )

    # The destructive step in materialize() is `rmtree(target)`. If the operator
    # has *also* bind-mounted their host config onto the target path -- the "fix
    # it back to match the other agents' mounts" mistake the compose comment
    # predicts, but additive rather than replacing -- then source != target, the
    # check above passes, and rmtree would walk their real config dir, deleting
    # secrets.yaml before failing EBUSY on the mountpoint itself. Refuse instead:
    # the whole point of this module is that the host's config is never damaged.
    if target.is_dir() and os.path.ismount(target):
        raise ValueError(
            f"{target} is a mount point. The host Goose config must be mounted "
            f"read-only at the staging path ({source}) and nowhere else; "
            f"mounting it here would have this process delete it. Remove the "
            f"volume that targets {target} (see docker-compose.yml)."
        )

    # An empty-but-present source is not a config. Docker creates the host dir if
    # the operator uncomments the mount before ever running `goose configure`, so
    # this is a live failure mode -- and without the check we would wipe the
    # target, copy nothing, report success, and let Goose die at first dispatch
    # with "no provider configured". The preflight exists to move exactly that
    # class of failure to boot.
    if not (source / CONFIG_FILENAME).is_file():
        raise ValueError(
            f"{source} has no {CONFIG_FILENAME}; it is not a Goose config dir. "
            f"Run `goose configure` on the host, and check GOOSE_CONFIG_PATH "
            f"points at the result."
        )


def materialize(source: Path, target: Path, gateway: str) -> List[Rewrite]:
    """Copy the Goose config from ``source`` to ``target``, rewriting loopback endpoints.

    ``target`` is rebuilt from scratch on every call so the result is a pure
    function of the mounted config -- a stale rewrite from a previous boot can
    never linger. Files that are not valid UTF-8 are copied byte-for-byte, and
    permissions are preserved on files *and* directories (``secrets.yaml`` ships
    0600, and its directory should not widen to 0755).

    The copy is staged in a sibling directory and swapped into place, so a
    failure part-way through leaves the previous config intact rather than a
    half-written one.
    """

    _validate_materialize_paths(source, target)

    staging = target.parent / f".{target.name}.materializing"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    rewrites: List[Rewrite] = []
    for entry in sorted(source.rglob("*")):
        destination = staging / entry.relative_to(source)
        if entry.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif entry.is_file():
            raw = entry.read_bytes()
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError:
                destination.write_bytes(raw)
            else:
                rewritten, replacements = rewrite_text(decoded, gateway)
                destination.write_text(rewritten, encoding="utf-8")
                rewrites.extend(
                    Rewrite(path=target / entry.relative_to(source), before=before, after=after)
                    for before, after in replacements
                )
        else:
            continue
        shutil.copystat(entry, destination)

    if target.exists():
        shutil.rmtree(target)
    os.replace(staging, target)
    shutil.copystat(source, target)

    return rewrites


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=os.environ.get("GOOSE_HOST_CONFIG_DIR", DEFAULT_SOURCE_DIR),
        help="Read-only mount of the host Goose config dir.",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("GOOSE_CONTAINER_CONFIG_DIR", DEFAULT_TARGET_DIR),
        help="Container-local config dir Goose actually reads.",
    )
    parser.add_argument(
        "--gateway",
        default=os.environ.get("GOOSE_GATEWAY_HOST", DEFAULT_GATEWAY_HOST),
        help="Hostname that resolves to the Docker host.",
    )
    args = parser.parse_args(argv)

    source = Path(args.source).expanduser()
    target = Path(args.target).expanduser()

    if not source.is_dir():
        # This runs only because the preflight found a Goose route that is both
        # enabled and credentialed, so "no config" is not a benign default --
        # the very next webhook would dispatch a Goose that has no provider. The
        # one legitimate no-mount setup is a config baked into the image at the
        # target path; allow that, and fail everything else at boot rather than
        # mid-dispatch.
        if (target / CONFIG_FILENAME).is_file():
            print(
                f"[goose-config] WARN: no Goose config mounted at {source}; using "
                f"the existing config at {target}. A loopback endpoint in it will "
                f"NOT be redirected at the host.",
                file=sys.stderr,
            )
            return 0
        print(
            f"[goose-config] FATAL: no Goose config at {source} or {target}, but a "
            f"Goose route is enabled and credentialed -- every dispatch would fail "
            f"with no provider configured. Uncomment the goose volume in "
            f"docker-compose.yml (and run `goose configure` on the host), or remove "
            f"the goose routes.",
            file=sys.stderr,
        )
        return 1

    try:
        rewrites = materialize(source, target, args.gateway)
    except (OSError, ValueError) as exc:
        print(f"[goose-config] FATAL: {exc}", file=sys.stderr)
        return 1

    print(f"[goose-config] Materialized Goose config from {source} to {target}")
    for rewrite in rewrites:
        print(
            f"[goose-config] {rewrite.path.name}: {rewrite.before} -> {rewrite.after} "
            f"(loopback is the container itself; redirected at the Docker host)"
        )
    if not rewrites:
        print("[goose-config] No loopback endpoints found; config copied verbatim.")

    for warning in local_streaming_warnings(target, args.gateway):
        print(f"[goose-config] WARN: {warning}", file=sys.stderr)

    for warning in stdio_extension_warnings(target):
        print(f"[goose-config] WARN: {warning}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
