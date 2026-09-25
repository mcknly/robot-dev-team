#!/usr/bin/env python3
"""Robot Dev Team Project
File: scripts/image_audit.py
Description: Extract a release image's raw layers and scan them for canonical hostnames (#51).
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import argparse
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


def normalize_host(value: str) -> str:
    """A bare, lowercase host, from a value that may carry a port or IPv6 brackets.

    Anything else is refused rather than passed through: a scheme or path means the caller passed a
    URL, and an empty port a typo, and searching for either literal would miss every bare
    occurrence of the host.
    """
    host = value.strip().lower()
    if not host:
        raise ImageAuditError("at least one non-empty host is required")
    if "://" in host or "/" in host:
        raise ImageAuditError("a host must be bare: no scheme and no path")
    port_form = re.fullmatch(r"\[([^\]]+)\](?::(\d+))?|([^:\[\]]+)(?::(\d+))?", host)
    if port_form:
        return port_form.group(1) or port_form.group(3)
    if host.count(":") > 1 and not host.endswith(":") and "[" not in host and "]" not in host:
        return host  # a bare IPv6 address: its colons are not a port separator
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
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))

    return {"version": version, "plugins_used": canonical(plugins), "filters_used": canonical(filters)}


def write_baseline(scan: dict[str, Any], *, digest: str, output: Path) -> int:
    entries = [
        {"path": path, "type": kind, "hashed_secret": hashed}
        for path, kind, hashed in sorted(secret_keys(scan))
    ]
    if not entries:
        raise ImageAuditError("an empty scan cannot be a baseline")
    profile = scanner_profile(scan)
    # The one regex filter allowed: every later release is held to this profile, so an
    # `--exclude-lines`, `--exclude-secrets`, or extra file pattern here would suppress hits forever.
    regex_filters = [item for item in profile["filters_used"]
                     if not isinstance(item, dict)
                     or str(item.get("path", "")).startswith("detect_secrets.filters.regex.")]
    if regex_filters != [{"path": REGEX_EXCLUDE_FILE, "pattern": [SECRETS_EXCLUDE]}]:
        raise ImageAuditError(
            f"a baseline must come from a scan run with --exclude-files '{SECRETS_EXCLUDE}' "
            "and nothing else excluded"
        )
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
