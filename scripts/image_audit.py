#!/usr/bin/env python3
"""Robot Dev Team Project
File: scripts/image_audit.py
Description: Extract a release image's raw layers and scan them for canonical hostnames.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Iterator, Sequence

LAYER_MEDIA_TYPES = (
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
)
WHITEOUT_PREFIX = ".wh."
# Written by `extract` next to the layers, and scanned with them: the manifest and the config
# blob reach every puller too, and `layers.json` carries the names and targets of the links and
# device nodes that extraction skips.
MANIFEST_FILE = "image-manifest.json"
CONFIG_FILE = "image-config.json"
LAYERS_FILE = "layers.json"
METADATA_FILES = (MANIFEST_FILE, CONFIG_FILE, LAYERS_FILE)


class ImageAuditError(RuntimeError):
    """The image could not be extracted or audited."""


# --- Extraction -------------------------------------------------------------------------------


@dataclass
class LayerRecord:
    index: int
    digest: str
    files: int = 0
    # Skipped members, by name and (for links) target. Recorded rather than dropped, because a
    # link's name or target ships in the layer and must be scanned like any other text in it.
    links: list[list[str]] = field(default_factory=list)
    special: list[str] = field(default_factory=list)
    whiteouts: list[str] = field(default_factory=list)


def crane(binary: Path, *arguments: str) -> bytes:
    result = subprocess.run([str(binary), *arguments], capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ImageAuditError(f"crane {arguments[0]} failed: {detail}")
    return result.stdout


def layer_digests(manifest: dict[str, Any]) -> list[str]:
    """The ordered layer digests of a single-platform manifest.

    A multi-platform index is refused rather than resolved: the release path is `linux/amd64`
    only, and auditing whichever platform an index resolves to would audit something unnamed.
    """
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ImageAuditError("the reference is not a single-platform image manifest")
    digests: list[str] = []
    for layer in layers:
        if not isinstance(layer, dict) or layer.get("mediaType") not in LAYER_MEDIA_TYPES:
            raise ImageAuditError(f"unsupported layer: {layer!r}")
        digests.append(str(layer["digest"]))
    return digests


def extract_layer(stream: IO[bytes], destination: Path, record: LayerRecord) -> None:
    """Extract regular files and directories only, as untrusted data.

    Links and device nodes carry no content, and a link is exactly what a hostile layer would use
    to write outside the destination, so they are not written -- but their names and targets are
    recorded in the layer record, which is scanned too. Whiteout markers are regular files and are
    extracted and recorded: they are how a layer deletes a lower layer's file, which is why every
    layer is scanned on its own and not only the merged filesystem.
    """
    def only_content(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        if member.issym() or member.islnk():
            record.links.append([member.name, member.linkname])
            return None
        if not (member.isfile() or member.isdir()):
            record.special.append(member.name)
            return None
        if Path(member.name).name.startswith(WHITEOUT_PREFIX):
            record.whiteouts.append(member.name)
        if member.isfile():
            record.files += 1
        return tarfile.data_filter(member, path)

    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        archive.extractall(destination, filter=only_content)


def extract_blob(binary: Path, reference: str, target: Path, record: LayerRecord) -> None:
    """Stream one layer blob through extraction, and surface crane's own error if it fails.

    A failed download reaches the extractor as an empty stream, which tarfile reports as "not a
    gzip file" -- true, and useless. The process is always reaped and its exit status checked
    first, so the error names the real cause (an expired login, a 404) instead.
    """
    process = subprocess.Popen([str(binary), "blob", reference], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    assert process.stdout is not None
    extraction_error: tarfile.TarError | None = None
    try:
        extract_layer(process.stdout, target, record)
    except tarfile.TarError as exc:
        extraction_error = exc
    finally:
        _, stderr = process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise ImageAuditError(f"crane blob {reference} failed: {detail}")
    if extraction_error is not None:
        raise extraction_error


def extract_image(binary: Path, image: str, destination: Path) -> list[LayerRecord]:
    if "@sha256:" not in image:
        raise ImageAuditError("the image must be named by digest (repository@sha256:...)")
    if destination.exists() and any(destination.iterdir()):
        raise ImageAuditError(f"{destination} is not empty; extract into a fresh directory")
    destination.mkdir(parents=True, exist_ok=True)
    repository = image.split("@", 1)[0]
    manifest_bytes = crane(binary, "manifest", image)
    digests = layer_digests(json.loads(manifest_bytes))
    (destination / MANIFEST_FILE).write_bytes(manifest_bytes)
    (destination / CONFIG_FILE).write_bytes(crane(binary, "config", image))
    records: list[LayerRecord] = []
    for index, digest in enumerate(digests):
        record = LayerRecord(index, digest)
        target = destination / f"layer-{index:02d}"
        target.mkdir()
        extract_blob(binary, f"{repository}@{digest}", target, record)
        records.append(record)
    (destination / LAYERS_FILE).write_text(
        json.dumps([record.__dict__ for record in records], indent=2) + "\n", encoding="utf-8"
    )
    return records


# --- Hostname scan ----------------------------------------------------------------------------


# ASCII DNS-style labels, none empty and none starting or ending with a hyphen. An allowlist, not
# a denylist: a typo such as `host;x` or `host.` would otherwise pass through as a literal that no
# bare occurrence of the host contains.
HOST_NAME = re.compile(r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?)*")


def ipv6_host(text: str) -> str:
    """The compressed form of an IPv6 address, refusing anything that is not a plain one.

    A zone id is kept in the compressed form, and an IPv4-mapped address compresses differently on
    Python 3.12 than on 3.13+, so neither is a stable literal to search for. A dotted form such as
    NAT64's `64:ff9b::1.2.3.4` compresses to hex nobody writes, and `::` would match nearly any
    binary, so both are refused too.
    """
    if "." in text:
        raise ImageAuditError("an IPv6 host must be written in hex, without an embedded IPv4 address")
    try:
        address = ipaddress.IPv6Address(text)
    except ValueError:
        # ipaddress quotes its input; the host stays out of the message, as everywhere else here.
        raise ImageAuditError("a bracketed or multi-colon host must be an IPv6 address") from None
    if address.scope_id is not None or address.ipv4_mapped is not None or address.is_unspecified:
        raise ImageAuditError("an IPv6 host must not carry a zone id, be IPv4-mapped, or be '::'")
    return address.compressed


def normalize_host(value: str) -> str:
    """A bare, lowercase host, from a value that may carry a port or IPv6 brackets.

    Anything else is refused rather than passed through: a scheme or path means the caller passed a
    URL, user info or whitespace a pasted value, and an empty port a typo, and searching for any of
    those literals would miss every bare occurrence of the host. An IPv6 address is searched in its
    compressed (RFC 5952) spelling only, so a file writing it out in full is not matched; the
    canonical hosts are names, so one spelling is enough.
    """
    if not value.strip():
        raise ImageAuditError("at least one non-empty host is required")
    # Checked on the value as given: trimming it first would accept " host" and "host\u00a0".
    if any(character.isspace() for character in value):
        raise ImageAuditError("a host must not contain whitespace")
    # Before lowercasing: str.lower() folds some non-ASCII letters into ASCII (KELVIN SIGN to "k").
    if not value.isascii():
        raise ImageAuditError("a host must be ASCII")
    host = value.lower()
    if "://" in host or "/" in host:
        raise ImageAuditError("a host must be bare: no scheme and no path")
    if "@" in host:
        raise ImageAuditError("a host must be bare: no user info")
    bracketed = re.fullmatch(r"\[([^\]]+)\](?::\d+)?", host)
    if bracketed:
        return ipv6_host(bracketed.group(1))
    named = re.fullmatch(r"([^:\[\]]+)(?::\d+)?", host)
    if named:
        if not HOST_NAME.fullmatch(named.group(1)):
            raise ImageAuditError(
                "a host name may hold only letters, digits, '-', '_' and '.', in non-empty labels"
            )
        return named.group(1)
    if host.count(":") > 1 and "[" not in host and "]" not in host:
        return ipv6_host(host)  # a bare IPv6 address: its colons are not a port separator
    raise ImageAuditError("a host must be bare: a name, optionally with a numeric port")


def all_paths(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_symlink():
            yield path


def redact(text: str, hosts: Sequence[str]) -> str:
    for index, host in enumerate(hosts):
        text = re.sub(re.escape(host), f"<host#{index + 1}>", text, flags=re.IGNORECASE)
    return text


@dataclass(frozen=True)
class HostScan:
    files: int
    offenders: list[tuple[str, int]]


def scan_hosts(root: Path, hosts: Sequence[str]) -> HostScan:
    """Scan every file's bytes and every file and directory name, case-insensitively.

    Offenders are returned with the index of the host they contain, and their paths with any host
    text redacted, so a finding is reported without restating the literal the scan exists to keep
    out of public view -- a file *named* after the host would otherwise print it.
    """
    # A registry on the server's host at another port normalizes to the same name: count it once.
    bare = list(dict.fromkeys(normalize_host(host) for host in hosts))
    if not bare:
        raise ImageAuditError("at least one non-empty host is required")
    needles = [host.encode() for host in bare]
    files = 0
    offenders: list[tuple[str, int]] = []
    for path in all_paths(root):
        relative = path.relative_to(root).as_posix()
        # tarfile decodes a non-UTF-8 member name with surrogate escapes; encode it back the same way.
        folded_name = relative.lower().encode(errors="surrogateescape")
        content = b""
        if path.is_file():
            files += 1
            content = path.read_bytes().lower()
        for index, needle in enumerate(needles):
            if needle in folded_name or needle in content:
                offenders.append((redact(relative, bare), index))
    return HostScan(files, offenders)


def expected_file_count(root: Path) -> int:
    """The number of regular files an `extract` wrote under `root`, metadata files included.

    Every metadata file is required. A missing one must not quietly lower the count it is checked
    against, and a tree with no layer record at all is not an extraction; scanning one of those
    takes an explicit `--no-extraction`.
    """
    missing = [name for name in METADATA_FILES if not (root / name).is_file()]
    if missing:
        raise ImageAuditError(
            f"{root} is not a complete extraction (missing {', '.join(missing)}); pass "
            "--no-extraction only for a tree `extract` did not produce"
        )
    records = json.loads((root / LAYERS_FILE).read_text(encoding="utf-8"))
    counts: list[int] = []
    for record in records if isinstance(records, list) else []:
        files = record.get("files") if isinstance(record, dict) else None
        if not isinstance(files, int) or isinstance(files, bool) or files < 0:
            raise ImageAuditError(f"malformed {LAYERS_FILE}: a layer record has no file count")
        counts.append(files)
    if not counts:
        raise ImageAuditError(f"malformed {LAYERS_FILE}: expected a list of layer records")
    return sum(counts) + len(METADATA_FILES)


def check_host_scan(root: Path, result: HostScan, *, extraction: bool = True) -> None:
    """Refuse a clean result that proves nothing: an empty or incomplete scan."""
    if result.files == 0:
        raise ImageAuditError(f"no files under {root}; nothing was scanned")
    if not extraction:
        return
    expected = expected_file_count(root)
    if expected != result.files:
        raise ImageAuditError(
            f"scanned {result.files} files but the extraction recorded {expected}; the tree under "
            f"{root} is not the extracted image (a changed tree, or a layer listing one path twice)"
        )


# --- Secrets baseline -------------------------------------------------------------------------

BASELINE_SCHEMA = 2
LAYER_DIR = re.compile(r"^(?:\./)?layer-\d+/")
# The secrets scan's `--exclude-files` pattern: the metadata files `extract` writes. The hostname
# scan covers them. The config changes on every build, so a hit there would be a new, unclassifiable
# hash each release. detect-secrets records the pattern in `filters_used`, which the profile binds.
SECRETS_EXCLUDE = r"^(image-manifest|image-config|layers)\.json$"
REGEX_EXCLUDE_FILE = "detect_secrets.filters.regex.should_exclude_file"
REGEX_FILTER_PREFIX = "detect_secrets.filters.regex."
EXCLUDE_FILTER: dict[str, Any] = {"path": REGEX_EXCLUDE_FILE, "pattern": [SECRETS_EXCLUDE]}

# What `detect-secrets scan` with no plugin or filter options records under this version. Every
# later release is held to a baseline's profile, so `baseline` accepts only these: a dropped or
# retuned plugin, or any filter beyond the defaults and the metadata exclusion, suppresses hits
# forever. Extending either list is a reviewed change made with the version pin (docs/RELEASING.md).
DETECT_SECRETS_VERSION = "1.5.0"
# Whole records, parameters included: `--only-verified` keeps the verification filter's path but
# raises its `min_level`. Two more defaults, `common.is_invalid_file` and
# `heuristic.is_non_text_file`, always run and are never serialized, so they are not listed.
DEFAULT_FILTERS: tuple[dict[str, Any], ...] = (
    {"min_level": 2, "path": "detect_secrets.filters.common.is_ignored_due_to_verification_policies"},
    {"path": "detect_secrets.filters.allowlist.is_line_allowlisted"},
    {"path": "detect_secrets.filters.heuristic.is_indirect_reference"},
    {"path": "detect_secrets.filters.heuristic.is_likely_id_string"},
    {"path": "detect_secrets.filters.heuristic.is_lock_file"},
    {"path": "detect_secrets.filters.heuristic.is_not_alphanumeric_string"},
    {"path": "detect_secrets.filters.heuristic.is_potential_uuid"},
    {"path": "detect_secrets.filters.heuristic.is_prefixed_with_dollar_sign"},
    {"path": "detect_secrets.filters.heuristic.is_sequential_string"},
    {"path": "detect_secrets.filters.heuristic.is_swagger_file"},
    {"path": "detect_secrets.filters.heuristic.is_templated_secret"},
)
PLUGINS: tuple[dict[str, Any], ...] = (
    {"keyword_exclude": "", "name": "KeywordDetector"},
    {"limit": 3.0, "name": "HexHighEntropyString"},
    {"limit": 4.5, "name": "Base64HighEntropyString"},
    {"name": "AWSKeyDetector"},
    {"name": "ArtifactoryDetector"},
    {"name": "AzureStorageKeyDetector"},
    {"name": "BasicAuthDetector"},
    {"name": "CloudantDetector"},
    {"name": "DiscordBotTokenDetector"},
    {"name": "GitHubTokenDetector"},
    {"name": "GitLabTokenDetector"},
    {"name": "IPPublicDetector"},
    {"name": "IbmCloudIamDetector"},
    {"name": "IbmCosHmacDetector"},
    {"name": "JwtTokenDetector"},
    {"name": "MailchimpDetector"},
    {"name": "NpmDetector"},
    {"name": "OpenAIDetector"},
    {"name": "PrivateKeyDetector"},
    {"name": "PypiTokenDetector"},
    {"name": "SendGridDetector"},
    {"name": "SlackDetector"},
    {"name": "SoftlayerDetector"},
    {"name": "SquareOAuthDetector"},
    {"name": "StripeDetector"},
    {"name": "TelegramBotTokenDetector"},
    {"name": "TwilioKeyDetector"},
)


def secret_keys(scan: dict[str, Any]) -> set[tuple[str, str, str]]:
    """Reduce a `detect-secrets scan` document to comparable (path, type, hashed secret) keys.

    The layer directory is dropped from each path: a rebuild of the same Dockerfile can shift
    layer numbers, while the file and the string it holds are what the classification is about.
    Line numbers are dropped for the same reason. `hashed_secret` is detect-secrets' own SHA-1
    of the matched string, so a changed string is a new key even at the same path.
    """
    results = scan.get("results")
    if not isinstance(results, dict):
        raise ImageAuditError("the scan document carries no results object")
    keys: set[tuple[str, str, str]] = set()
    for path, hits in results.items():
        if not isinstance(hits, list):
            raise ImageAuditError(f"unreadable results for {path}")
        normalized = LAYER_DIR.sub("", str(path))
        for hit in hits:
            keys.add((normalized, str(hit["type"]), str(hit["hashed_secret"])))
    return keys


def scanner_profile(scan: dict[str, Any]) -> dict[str, Any]:
    """What produced a scan: the detect-secrets version, the plugins it ran, and its filters.

    A comparison is only meaningful between scans with the same profile. A newer scanner that
    drops or retunes a plugin reports *fewer* hits, which would otherwise pass as clean, and so
    does a changed filter -- an added `--exclude-files` pattern or allowlist included.
    """
    version = scan.get("version")
    plugins = scan.get("plugins_used")
    filters = scan.get("filters_used")
    if not isinstance(version, str) or not isinstance(plugins, list) or not isinstance(filters, list):
        raise ImageAuditError("the scan records no scanner version, plugin list, or filter list")

    def canonical(items: list[Any]) -> list[Any]:
        return sorted(items, key=record_key)

    return {"version": version, "plugins_used": canonical(plugins), "filters_used": canonical(filters)}


def record_key(item: Any) -> str:
    """A profile record as canonical JSON, so records compare exactly: `3` is not `3.0` here."""
    return json.dumps(item, sort_keys=True)


def check_baseline_profile(scan: dict[str, Any]) -> None:
    """Refuse to record a scanner profile that would hold every later release to fewer hits.

    The version and plugins must be exactly the pinned ones. Each filter must be a pinned default,
    parameters included, or the one metadata exclusion; dropping a default only adds hits, so that
    is allowed. Errors name a record by its position and print a filter path only when it is a
    pinned one: a custom filter's `file://` path, a word list's file name, or an
    `--exclude-secrets` pattern is caller-controlled text that could hold an endpoint or a secret.
    """
    profile = scanner_profile(scan)
    if profile["version"] != DETECT_SECRETS_VERSION:
        raise ImageAuditError(
            f"a baseline must come from detect-secrets {DETECT_SECRETS_VERSION}; a new version's "
            "defaults are pinned in a reviewed change with the version (docs/RELEASING.md)"
        )
    used = [record_key(item) for item in profile["plugins_used"]]
    if used != sorted(record_key(item) for item in PLUGINS):
        missing = [str(item["name"]) for item in PLUGINS if record_key(item) not in used]
        others = len(used) - (len(PLUGINS) - len(missing))
        raise ImageAuditError(
            "a baseline must come from a scan with detect-secrets' default plugins, unchanged "
            f"(missing or retuned: {', '.join(missing) or 'none'}; unexpected records: {others})"
        )
    filters: list[Any] = scan["filters_used"]
    regex = [record_key(item) for item in filters
             if isinstance(item, dict) and str(item.get("path", "")).startswith(REGEX_FILTER_PREFIX)]
    if regex != [record_key(EXCLUDE_FILTER)]:
        raise ImageAuditError(
            f"a baseline must come from a scan run with --exclude-files '{SECRETS_EXCLUDE}' and no "
            "other regex filter: no --exclude-lines, --exclude-secrets, or other file pattern"
        )
    defaults = {record_key(item) for item in DEFAULT_FILTERS}
    default_paths = {str(item["path"]) for item in DEFAULT_FILTERS}
    for index, item in enumerate(filters):
        if not isinstance(item, dict):
            raise ImageAuditError(f"filters_used[{index}] is not a filter record")
        path = item.get("path")
        if record_key(item) in defaults or record_key(item) in regex:
            continue
        if isinstance(path, str) and path in default_paths:
            raise ImageAuditError(
                f"filters_used[{index}] ({path}) is a default filter with changed parameters, "
                "such as --only-verified produces"
            )
        raise ImageAuditError(
            f"filters_used[{index}] is not a detect-secrets {DETECT_SECRETS_VERSION} default "
            "filter; a baseline allows no custom filter or word list"
        )


def write_baseline(scan: dict[str, Any], *, digest: str, output: Path) -> int:
    entries = [
        {"path": path, "type": kind, "hashed_secret": hashed}
        for path, kind, hashed in sorted(secret_keys(scan))
    ]
    if not entries:
        raise ImageAuditError("an empty scan cannot be a baseline")
    check_baseline_profile(scan)
    profile = scanner_profile(scan)
    document = {
        "schema_version": BASELINE_SCHEMA,
        "description": (
            "Classified detect-secrets hits in the audited release image's layers, every one a "
            "false positive per docs/SANITIZATION_REPORT.md. A later image may carry only these."
        ),
        "digest": digest,
        "scanner": profile,
        "entries": entries,
    }
    output.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return len(entries)


@dataclass(frozen=True)
class SecretsComparison:
    unclassified: list[tuple[str, str, str]]
    lost: list[tuple[str, str, str]]


def compare_secrets(scan: dict[str, Any], baseline: dict[str, Any]) -> SecretsComparison:
    """Hits in `scan` the audited baseline does not hold, and baseline hits `scan` no longer has.

    Both directions fail the check. A lost hit means the scan did not read part of the image (or
    the image changed), so its "nothing new" says nothing about those files. Fails closed on a scan
    that cannot be compared at all: a different scanner profile, no hits, or no hit in common.
    """
    if baseline.get("schema_version") != BASELINE_SCHEMA:
        raise ImageAuditError("the secrets baseline has an unsupported schema")
    entries = baseline.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ImageAuditError("the secrets baseline carries no entries")
    if scanner_profile(scan) != baseline.get("scanner"):
        raise ImageAuditError(
            "the scan was not produced by the scanner, plugins and filters the baseline records "
            f"({baseline.get('scanner', {}).get('version')}); re-scan with the same profile"
        )
    current = secret_keys(scan)
    if not current:
        raise ImageAuditError("the scan found nothing at all; it did not scan the image")
    known = {(str(e["path"]), str(e["type"]), str(e["hashed_secret"])) for e in entries}
    if not current & known:
        raise ImageAuditError("the scan shares no hit with the baseline; it did not scan the image")
    return SecretsComparison(sorted(current - known), sorted(known - current))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    extract = sub.add_parser("extract", help="extract every raw layer of a digest-named image")
    extract.add_argument("--crane", type=Path, default=Path(".release-bin/crane"))
    extract.add_argument("--image", required=True, help="repository@sha256:<digest>")
    extract.add_argument("--dest", type=Path, required=True, help="a fresh, empty directory")
    hosts = sub.add_parser("hosts", help="scan extracted files for canonical hostnames")
    hosts.add_argument("--root", type=Path, required=True)
    hosts.add_argument(
        "--host", action="append", required=True,
        help="a bare hostname (a port is stripped); pass the server host and the registry host",
    )
    hosts.add_argument(
        "--no-extraction", action="store_true",
        help="scan a tree `extract` did not produce (a merged filesystem, a positive control): "
             "only an empty scan fails, as there is no layer record to count against",
    )
    baseline = sub.add_parser("baseline", help="record an audited, classified secrets scan")
    baseline.add_argument("--scan", type=Path, required=True, help="detect-secrets scan output")
    baseline.add_argument("--digest", required=True)
    baseline.add_argument("--output", type=Path, required=True)
    compare = sub.add_parser(
        "compare-secrets", help="fail on any secrets-scan hit the audited baseline does not hold"
    )
    compare.add_argument("--scan", type=Path, required=True)
    compare.add_argument("--baseline", type=Path, required=True)
    return parser


def run_hosts(args: argparse.Namespace) -> int:
    result = scan_hosts(args.root, args.host)
    check_host_scan(args.root, result, extraction=not args.no_extraction)
    for relative, index in result.offenders:
        print(f"[image] HOST #{index + 1}: {relative}", file=sys.stderr)
    print(f"[image] scanned {result.files} files: {len(result.offenders)} findings of a canonical host")
    return 1 if result.offenders else 0


def load(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ImageAuditError(f"{path} is not a JSON object")
    return document


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "extract":
            records = extract_image(args.crane, args.image, args.dest)
            files = sum(record.files for record in records)
            print(f"[image] extracted {len(records)} layers, {files} regular files, to {args.dest}")
            return 0
        if args.command == "baseline":
            count = write_baseline(load(args.scan), digest=args.digest, output=args.output)
            print(f"[image] recorded {count} classified hits in {args.output}")
            return 0
        if args.command == "compare-secrets":
            result = compare_secrets(load(args.scan), load(args.baseline))
            for path, kind, _ in result.unclassified:
                print(f"[image] UNCLASSIFIED: {kind}: {path}", file=sys.stderr)
            for path, kind, _ in result.lost:
                print(f"[image] LOST: {kind}: {path}", file=sys.stderr)
            print(f"[image] {len(result.unclassified)} secrets-scan hits are not in the audited "
                  f"baseline; {len(result.lost)} baseline hits are missing from the scan")
            return 1 if result.unclassified or result.lost else 0
        return run_hosts(args)
    # Malformed input (a truncated record, a missing key) fails closed with the same one-line error.
    except (ImageAuditError, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        print(f"[image] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
