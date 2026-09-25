#!/usr/bin/env python3
"""Robot Dev Team Project
File: scripts/license_review.py
Description: Classify the license of every component in a release SBOM (#82).
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

EVIDENCE_PATH = Path("security/license-evidence.json")
EVIDENCE_SCHEMA = 1
NOASSERTION = "NOASSERTION"
USER_AGENT = "robot-dev-team-license-review (+https://github.com/mcknly/robot-dev-team)"
CRATES_IO = "https://crates.io/api/v1/crates"
DEPS_DEV = "https://api.deps.dev/v3/systems"
# crates.io's crawler policy asks for at most one request per second.
CRATES_IO_INTERVAL = 1.0

# Obligation classes, least to most restrictive. An OR expression takes the least restrictive
# alternative (the distributor may choose it); an AND takes the most restrictive part.
PERMISSIVE, WEAK, STRONG, UNKNOWN = 0, 1, 2, 3
CLASS_NAMES = {PERMISSIVE: "permissive", WEAK: "weak copyleft", STRONG: "strong copyleft",
               UNKNOWN: "needs review"}
STRONG_PREFIXES = ("GPL-", "AGPL-", "EUPL-", "OSL-", "CC-BY-SA-", "SSPL-")
# GFDL is a documentation licence: copyleft on the manuals it covers, which a Debian source
# package's copyright file lists alongside the code.
WEAK_PREFIXES = ("LGPL-", "MPL-", "EPL-", "CDDL-", "Artistic-", "CPL-", "MS-RL", "LGPLLR", "GFDL-")
PERMISSIVE_IDS = frozenset(
    {
        "0BSD", "Apache-2.0", "BlueOak-1.0.0", "BSD-1-Clause", "BSD-2-Clause", "BSD-3-Clause",
        "BSD-3-Clause-Clear", "BSD-4-Clause", "BSD-4-Clause-UC", "BSL-1.0", "bzip2-1.0.6",
        "CC0-1.0", "CC-BY-4.0", "CC-PDDC", "CDLA-Permissive-2.0", "curl", "FSFAP", "FSFUL",
        "FSFULLR", "FSFULLRWD", "HPND", "ICU", "ISC", "libpng-2.0", "Libpng", "MIT", "MIT-0",
        "MIT-CMU", "NCSA", "OpenSSL", "PSF-2.0", "Python-2.0", "Python-2.0.1", "Ruby",
        "TCL", "Unicode-3.0", "Unicode-DFS-2016", "Unlicense", "W3C", "X11", "Zlib", "ZPL-2.1",
    }
)
PERMISSIVE_IDS = PERMISSIVE_IDS | frozenset(
    {
        "Artistic-dist", "Beerware", "BSD-3-Clause-Attribution", "CC-BY-3.0", "FTL", "Kazlib",
        "Latex2e", "OLDAP-2.8", "RSA-MD", "SunPro",
    }
)
# Debian copyright files use short names that Syft passes through as `LicenseRef-<name>`, and
# Debian suffixes them freely (`BSD-3-clause-Regents`, `public-domain-md5`). A permissive name
# therefore matches a listed stem exactly or followed by `-`, never as a bare prefix: a short stem
# like `The` or `PD` must not wave through some future name that merely starts with it. Anything
# unlisted falls to UNKNOWN, which fails `classify` until someone reviews it.
DEBIAN_PERMISSIVE_STEMS = frozenset(
    {
        "Expat", "MIT", "BSD", "BSD3", "BSLA", "ISC", "public-domain", "permissive",
        "all-permissive", "customFSFUL", "customFSFULLRWD", "Boost", "BZIP", "CC0", "Chromium",
        "Unicode", "Univ-Coimbra", "Inner-Net", "Carnegie", "CORE-MATH", "PCRE", "pcre",
        "gnulib", "TCL-like", "UMich", "JCG", "NeoSoft-permissive", "OpenLDAP", "REGCOMP",
        "SDBM-PUBLIC-DOMAIN", "TEXT-TABS", "dlmalloc", "mingw-runtime", "verbatim", "EDL-1.0",
        "FreeSoftware",
        # The GNU All-Permissive License, despite the GNU name.
        "GAP",
        # The license *of the GPL's own text*: verbatim copying only. Not copyleft.
        "DONT-CHANGE-THE-GPL",
    }
)
# Stems too short to extend safely -- `The-Copyleft` must not pass as `The` -- so only the exact
# names reviewed in a copyright file are accepted. A new one is needs-review until it is added.
DEBIAN_PERMISSIVE_EXACT = frozenset(
    {"The", "PD", "PD-debian", "DEC", "IBM", "IBM-as-is", "F5", "FSF-manpages", "FSF-unlimited"}
)
# The restrictive families keep plain prefix matching: a miss there errs towards review, and a GNU
# name must win over any permissive-looking suffix Debian appended (`GPL-2--with-link-exception`).
DEBIAN_WEAK_PREFIXES = ("LGPL", "GFDL", "Artistic", "MPL", "noderivs")
DEBIAN_STRONG_PREFIXES = ("GPL",)


def debian_permissive(name: str) -> bool:
    if name in DEBIAN_PERMISSIVE_EXACT:
        return True
    return any(name == stem or name.startswith(stem + "-") for stem in DEBIAN_PERMISSIVE_STEMS)
# Copyleft or restrictive terms outside the GNU families, classified explicitly so they can
# never be read as permissive by a prefix accident.
EXPLICIT_CLASSES = {"Sleepycat": STRONG, "SMAIL-GPL": STRONG, "MS-PL": WEAK, "MS-RL": WEAK}
TOKEN = re.compile(r"\(|\)|[A-Za-z0-9.+:-]+")


class LicenseReviewError(RuntimeError):
    """The classification cannot be produced or does not account for every entry."""


# --- SPDX expressions -------------------------------------------------------------------------


def license_class(identifier: str) -> int:
    base = identifier.removesuffix("+")
    if base in EXPLICIT_CLASSES:
        return EXPLICIT_CLASSES[base]
    if base in PERMISSIVE_IDS:
        return PERMISSIVE
    if base.startswith(WEAK_PREFIXES):
        return WEAK
    if base.startswith(STRONG_PREFIXES):
        return STRONG
    if base.startswith("LicenseRef-"):
        name = base.removeprefix("LicenseRef-")
        # GNU families before permissive stems: `LGPL...` must not match `GPL`, and a GNU name
        # must win over any permissive-looking suffix.
        if name.startswith(DEBIAN_WEAK_PREFIXES):
            return WEAK
        if name.startswith(DEBIAN_STRONG_PREFIXES):
            return STRONG
        if debian_permissive(name):
            return PERMISSIVE
    return UNKNOWN


class _ExpressionClassifier:
    """Recursive descent over an SPDX expression, folding each operand into its class."""

    def __init__(self, expression: str) -> None:
        self.expression = expression
        # Every non-space character must belong to a token. `findall` alone skips what it cannot
        # match, which would turn `MIT?` or a newly formatted upstream string into a clean `MIT`.
        residue = TOKEN.sub("", expression)
        if residue.strip():
            raise LicenseReviewError(
                f"unsupported characters {sorted(set(residue.split()))} in license expression: "
                f"{expression!r}"
            )
        self.tokens: list[str] = TOKEN.findall(expression)
        self.position = 0

    def peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self) -> str:
        if self.position >= len(self.tokens):
            raise LicenseReviewError(f"truncated license expression: {self.expression!r}")
        token = self.tokens[self.position]
        self.position += 1
        return token

    def primary(self) -> int:
        token = self.take()
        if token == "(":
            value = self.disjunction()
            if self.take() != ")":
                raise LicenseReviewError(f"unbalanced license expression: {self.expression!r}")
            return value
        if token == ")":
            raise LicenseReviewError(f"unbalanced license expression: {self.expression!r}")
        value = license_class(token)
        if self.peek() == "WITH":
            self.take()
            self.take()
        return value

    def conjunction(self) -> int:
        value = self.primary()
        while self.peek() == "AND":
            self.take()
            value = max(value, self.primary())
        return value

    def disjunction(self) -> int:
        value = self.conjunction()
        while self.peek() == "OR":
            self.take()
            value = min(value, self.conjunction())
        return value


def expression_class(expression: str) -> int:
    """The obligation class a distributor incurs under the most favourable reading.

    Precedence follows SPDX: WITH binds tightest, then AND, then OR. A WITH exception only ever
    relaxes its base licence, so the base's class is kept as the conservative answer.
    """
    parser = _ExpressionClassifier(expression)
    if not parser.tokens:
        raise LicenseReviewError(f"empty license expression: {expression!r}")
    result = parser.disjunction()
    if parser.position != len(parser.tokens):
        raise LicenseReviewError(f"unbalanced license expression: {expression!r}")
    return result


# --- SBOM entries -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    key: str
    kind: str
    name: str
    version: str
    location: str
    declared: str


def purl_of(package: Mapping[str, Any]) -> str | None:
    for reference in package.get("externalRefs") or []:
        if reference.get("referenceType") == "purl":
            return str(reference.get("referenceLocator"))
    return None


def location_of(package: Mapping[str, Any]) -> str:
    match = re.search(r"(/[^ ,]+)", str(package.get("sourceInfo", "")))
    return match.group(1) if match else ""


def entry_of(package: Mapping[str, Any]) -> Entry:
    """Key one SBOM package the way the evidence file keys it.

    The image's own root entry is keyed generically: its SPDX name is the canonical registry
    reference, which must never reach a tracked file.
    """
    purl = purl_of(package)
    kind = purl.split(":", 1)[1].split("/", 1)[0] if purl else "file"
    name = str(package.get("name", ""))
    version = str(package.get("versionInfo", ""))
    location = location_of(package)
    if kind == "oci":
        key, name, version = "oci/image", "image", ""
    elif kind == "file":
        key = f"file:{location}"
    else:
        key = f"{kind}/{name}@{version}"
    declared = str(package.get("licenseDeclared") or NOASSERTION)
    return Entry(key, kind, name, version, location, declared)


def sbom_entries(document: Mapping[str, Any]) -> list[Entry]:
    packages = document.get("packages")
    if not isinstance(packages, list) or not packages:
        raise LicenseReviewError("the SBOM carries no packages")
    return [entry_of(package) for package in packages]


# --- Evidence ---------------------------------------------------------------------------------


def load_evidence(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != EVIDENCE_SCHEMA:
        raise LicenseReviewError(f"{path} has an unsupported schema")
    entries = document.get("entries")
    if not isinstance(entries, dict):
        raise LicenseReviewError(f"{path} carries no entries object")
    for key, record in entries.items():
        if not isinstance(record, dict) or not isinstance(record.get("source"), str):
            raise LicenseReviewError(f"evidence entry {key} has no source")
        license_value = record.get("license")
        if license_value is None and not record.get("reason"):
            raise LicenseReviewError(f"evidence entry {key} has no license and no reason")
    return dict(entries)


def write_evidence(path: Path, entries: Mapping[str, Mapping[str, Any]]) -> None:
    document = {
        "schema_version": EVIDENCE_SCHEMA,
        "description": (
            "Licenses for release-SBOM components whose licenseDeclared is NOASSERTION, keyed by "
            "type/name@version. Fetched entries record their source URL; manual entries record "
            "where the evidence was read. Written by scripts/license_review.py (#82)."
        ),
        "entries": {key: dict(entries[key]) for key in sorted(entries)},
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise LicenseReviewError(f"GET {url} failed ({exc.code})") from exc
    except urllib.error.URLError as exc:
        raise LicenseReviewError(f"GET {url} failed: {exc}") from exc


def crates_io_record(entry: Entry, fetch: Callable[[str], Any]) -> dict[str, Any]:
    url = f"{CRATES_IO}/{urllib.parse.quote(entry.name)}/{urllib.parse.quote(entry.version)}"
    payload = fetch(url)
    license_value = (payload or {}).get("version", {}).get("license")
    if license_value:
        # Cargo's legacy `/` separator means OR; crates.io still stores it for older crates.
        normalized = re.sub(r"\s*/\s*", " OR ", str(license_value))
        return {"license": normalized, "source": "crates.io", "evidence": url}
    return {"license": None, "source": "crates.io", "evidence": url,
            "reason": "crates.io records no license for this version"}


def deps_dev_record(entry: Entry, fetch: Callable[[str], Any]) -> dict[str, Any]:
    name = urllib.parse.quote(entry.name, safe="")
    url = f"{DEPS_DEV}/go/packages/{name}/versions/{urllib.parse.quote(entry.version)}"
    payload = fetch(url) or {}
    detected = [str(value) for value in payload.get("licenses") or [] if value]
    # `non-standard` is license text deps.dev could not identify. It could be anything, so an
    # answer containing it goes to review by hand even when a standard license sits beside it --
    # dropping it would record the recognisable half as the whole answer and fail open.
    if "non-standard" in detected:
        return {"license": None, "source": "deps.dev", "evidence": url,
                "reason": f"deps.dev found unidentified license text ({', '.join(detected)}); "
                          "read the module's license by hand"}
    if detected:
        # deps.dev lists every license it detected in the module; all of them apply.
        expression = " AND ".join(f"({value})" if " " in value else value for value in detected)
        return {"license": expression, "source": "deps.dev", "evidence": url}
    return {"license": None, "source": "deps.dev", "evidence": url,
            "reason": "deps.dev detected no license for this version"}


# Sources a tool wrote and may overwrite; every other source is a manual entry.
FETCHED_SOURCES = frozenset({"crates.io", "deps.dev", "publisher SBOM"})

FETCHERS: dict[str, Callable[[Entry, Callable[[str], Any]], dict[str, Any]]] = {
    "cargo": crates_io_record,
    "golang": deps_dev_record,
}


def resolve(
    entries: Iterable[Entry],
    evidence: dict[str, dict[str, Any]],
    *,
    fetch: Callable[[str], Any] = fetch_json,
    retrieved: str,
    pause: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Fetch evidence for every NOASSERTION entry of a fetchable type that has none yet."""
    added: list[str] = []
    for entry in {entry.key: entry for entry in entries}.values():
        if entry.declared != NOASSERTION or entry.key in evidence or entry.kind not in FETCHERS:
            continue
        if entry.kind == "cargo":
            pause(CRATES_IO_INTERVAL)
        record = FETCHERS[entry.kind](entry, fetch)
        record["retrieved"] = retrieved
        evidence[entry.key] = record
        added.append(entry.key)
    return added


def cyclonedx_license(component: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    for choice in component.get("licenses") or []:
        if choice.get("expression"):
            parts.append(str(choice["expression"]))
        elif isinstance(choice.get("license"), dict):
            identifier = choice["license"].get("id") or choice["license"].get("name")
            if identifier:
                parts.append(str(identifier))
    if not parts:
        return None
    return " AND ".join(f"({part})" if " " in part and len(parts) > 1 else part for part in parts)


def import_cyclonedx(
    document: Mapping[str, Any],
    evidence: dict[str, dict[str, Any]],
    *,
    kind: str,
    label: str,
    retrieved: str,
) -> tuple[list[str], list[str]]:
    """Take licenses from a publisher's CycloneDX SBOM shipped inside the image.

    Evidence from the artifact's own publisher, exact to the build and readable offline, is
    preferred over a registry lookup, so it replaces a *fetched* entry for the same key. A manual
    entry is never overwritten: someone read that one by hand, and a tool must not undo it.
    Returns the keys written and every disagreement with a previous entry, for review.
    """
    written: list[str] = []
    disagreed: list[str] = []
    for component in document.get("components") or []:
        license_value = cyclonedx_license(component)
        if not license_value:
            continue
        key = f"{kind}/{component.get('name')}@{component.get('version')}"
        previous = evidence.get(key)
        if previous and previous.get("license") and previous["license"] != license_value:
            disagreed.append(f"{key}: {previous['source']} {previous['license']!r} vs {license_value!r}")
        if previous and previous.get("source") not in FETCHED_SOURCES:
            continue
        evidence[key] = {
            "license": license_value,
            "source": "publisher SBOM",
            "evidence": label,
            "retrieved": retrieved,
        }
        written.append(key)
    return written, disagreed


def prune_evidence(entries: Iterable[Entry], evidence: dict[str, dict[str, Any]]) -> list[str]:
    """Keep only evidence the SBOM needs: entries it references with no declared license.

    An entry kept for a component that is no longer shipped reads as if it still were, and the
    file is committed evidence for one digest, not a cache.
    """
    needed = {entry.key for entry in entries if entry.declared == NOASSERTION}
    dropped = sorted(key for key in evidence if key not in needed)
    for key in dropped:
        del evidence[key]
    return dropped


# --- Classification ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Classified:
    entry: Entry
    license: str | None
    source: str
    obligation: int | None
    reason: str | None


def classify(entries: Sequence[Entry], evidence: Mapping[str, Mapping[str, Any]]) -> list[Classified]:
    results: list[Classified] = []
    for entry in entries:
        license_value: str | None
        reason: str | None
        if entry.declared != NOASSERTION:
            license_value, source, reason = entry.declared, "sbom", None
        elif entry.key in evidence:
            record = evidence[entry.key]
            license_value = record.get("license")
            source = str(record["source"])
            reason = record.get("reason")
        else:
            license_value, source = None, "none"
            reason = "no declared license in the SBOM and no evidence entry"
        obligation = expression_class(license_value) if license_value else None
        results.append(Classified(entry, license_value, source, obligation, reason))
    return results


def summarize(results: Sequence[Classified], digest: str, sbom_sha256: str) -> dict[str, Any]:
    """Count every entry into exactly one outcome.

    The outcomes that need a person -- unresolved, and resolved to a license nobody has
    classified -- are listed by key, because `classify` fails while either list is non-empty.
    """
    by_type: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    unresolved: list[dict[str, str]] = []
    needs_review: list[dict[str, str]] = []
    review: dict[str, dict[str, Any]] = {}
    for result in results:
        counts = by_type[result.entry.kind]
        counts["total"] += 1
        counts["sbom_noassertion"] += result.entry.declared == NOASSERTION
        if result.license is None:
            counts["unresolved"] += 1
            unresolved.append({"key": result.entry.key, "reason": str(result.reason)})
            continue
        counts["resolved"] += 1
        assert result.obligation is not None
        counts[CLASS_NAMES[result.obligation]] += 1
        if result.obligation == UNKNOWN:
            needs_review.append({"key": result.entry.key, "license": result.license})
        if result.obligation != PERMISSIVE:
            item = review.setdefault(
                result.entry.key,
                {"license": result.license, "class": CLASS_NAMES[result.obligation],
                 "source": result.source, "locations": []},
            )
            if result.entry.location and result.entry.location not in item["locations"]:
                item["locations"].append(result.entry.location)
    totals: collections.Counter[str] = collections.Counter()
    for counts in by_type.values():
        totals.update(counts)
    return {
        "digest": digest,
        "sbom_sha256": sbom_sha256,
        "entries": totals["total"],
        "by_type": {kind: dict(sorted(counts.items())) for kind, counts in sorted(by_type.items())},
        "totals": dict(sorted(totals.items())),
        "unresolved": sorted(unresolved, key=lambda item: item["key"]),
        "needs_review": sorted(needs_review, key=lambda item: item["key"]),
        "non_permissive": dict(sorted(review.items())),
    }


def verified_sbom(raw: bytes, *, source_name: str, expected_sha256: str) -> dict[str, Any]:
    """Bind the document to the image before classifying it.

    `--digest` alone is a label: the same SBOM would produce a clean report for any digest. The
    binding is the one the release path uses. The SBOM is stored under a digest-keyed package
    path, whose file SHA-256 the registry records, and its SPDX `name` must be the image it was
    generated from, `${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHA}`. The name is checked but never
    written out: it carries the canonical registry host.
    """
    # The bytes first: nothing is parsed from a document the registry did not record.
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256.removeprefix("sha256:"):
        raise LicenseReviewError(
            f"the SBOM's SHA-256 is {actual}, not the digest-keyed package file's {expected_sha256}"
        )
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise LicenseReviewError("the SBOM is not a JSON object")
    if document.get("name") != source_name:
        raise LicenseReviewError(
            "the SBOM's SPDX name is not --source-name; it describes a different image"
        )
    return document


def run_classify(args: argparse.Namespace, evidence: Mapping[str, Mapping[str, Any]]) -> int:
    raw = args.sbom.read_bytes()
    document = verified_sbom(raw, source_name=args.source_name, expected_sha256=args.sbom_sha256)
    summary = summarize(
        classify(sbom_entries(document), evidence), args.digest, hashlib.sha256(raw).hexdigest()
    )
    if args.output:
        args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    totals = summary["totals"]
    print(
        f"[license] {totals['total']} entries: {totals.get('resolved', 0)} resolved, "
        f"{totals.get('unresolved', 0)} unresolved, {len(summary['needs_review'])} need review, "
        f"{len(summary['non_permissive'])} distinct non-permissive components"
    )
    for item in summary["unresolved"]:
        print(f"[license] UNRESOLVED: {item['key']}: {item['reason']}", file=sys.stderr)
    for item in summary["needs_review"]:
        print(f"[license] NEEDS REVIEW: {item['key']}: {item['license']}", file=sys.stderr)
    # A tool exit status, not a release gate: whether releases fail on this is a separate policy
    # decision. It only stops a re-run from reporting success while entries are unaccounted for.
    return 1 if summary["unresolved"] or summary["needs_review"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("resolve", help="fetch evidence for unresolved crates and Go modules")
    fetch.add_argument("--sbom", type=Path, required=True)
    imported = sub.add_parser(
        "import-cyclonedx", help="take licenses from a publisher SBOM shipped in the image"
    )
    imported.add_argument("--cyclonedx", type=Path, required=True)
    imported.add_argument("--kind", required=True, help="package type the components are, e.g. cargo")
    imported.add_argument("--label", required=True, help="where the SBOM sits inside the image")
    prune = sub.add_parser("prune", help="drop evidence entries the SBOM does not reference")
    prune.add_argument("--sbom", type=Path, required=True)
    offline = sub.add_parser(
        "classify",
        help="classify every SBOM entry, offline; exits 1 while any entry is unresolved or needs review",
    )
    offline.add_argument("--sbom", type=Path, required=True)
    offline.add_argument("--digest", required=True, help="the image digest the SBOM describes")
    offline.add_argument(
        "--source-name",
        required=True,
        help="the image the SBOM must name: ${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHA}",
    )
    # Required, not optional: without it the digest is only a label, and a summary from an
    # unverified run would read exactly like a verified one.
    offline.add_argument(
        "--sbom-sha256",
        required=True,
        help="the file SHA-256 the registry records for the digest-keyed SBOM package file",
    )
    offline.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        evidence = load_evidence(args.evidence)
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        if args.command == "import-cyclonedx":
            raw = args.cyclonedx.read_bytes()
            # The file's hash pins which publisher SBOM the answers came from.
            label = f"{args.label} sha256:{hashlib.sha256(raw).hexdigest()}"
            written, disagreed = import_cyclonedx(
                json.loads(raw), evidence, kind=args.kind, label=label, retrieved=today
            )
            write_evidence(args.evidence, evidence)
            print(f"[license] imported {len(written)} {args.kind} licenses from {args.label}")
            for line in disagreed:
                print(f"[license] DISAGREES: {line}")
            return 0
        if args.command == "classify":
            return run_classify(args, evidence)
        entries = sbom_entries(json.loads(args.sbom.read_text(encoding="utf-8")))
        if args.command == "prune":
            dropped = prune_evidence(entries, evidence)
            write_evidence(args.evidence, evidence)
            print(f"[license] dropped {len(dropped)} evidence entries the SBOM does not reference")
            return 0
        added = resolve(entries, evidence, retrieved=today)
        write_evidence(args.evidence, evidence)
        print(f"[license] added {len(added)} evidence entries to {args.evidence}")
    except (LicenseReviewError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"[license] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
