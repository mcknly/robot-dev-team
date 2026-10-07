#!/usr/bin/env python3
"""Robot Dev Team Project
File: scripts/third_party_notices.py
Description: Collect and verify the license notices of the binaries the image redistributes.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import io
import json
import re
import shutil
import struct
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

from scripts.license_review import Entry, LicenseReviewError, sbom_entries

NOTICES_DIR = Path("notices")
MANIFEST_NAME = "notices.json"
README_NAME = "README"
CREDITS_NAME = "CREDITS"
MANIFEST_SCHEMA = 1
IMAGE_DOC_ROOT = Path("/usr/share/doc")
USER_AGENT = "robot-dev-team-notices (+https://github.com/mcknly/robot-dev-team)"
GENERATED = "scripts/third_party_notices.py"

# Every input the collector reads is pinned here, or reached through a pinned input: the crates
# through the Cargo.lock checksums inside the pinned uv source, the Go modules through the go.sum
# inside glab's own module zip. `glab.h1` is the Go checksum database's hash of that zip, so the
# proxy and sum.golang.org both have to agree with the pin. The Go and Rust entries are the
# toolchains the shipping binaries were built with -- `collect` reads both from the binaries and
# refuses a mismatch -- fetched only for the standard libraries' own notices.
# tests/test_third_party_notices.py binds `glab` and `uv` to the Dockerfile's GLAB_VERSION and
# UV_VERSION, and the committed manifests to this table, so a pin bump that skips regeneration
# fails the merge request.
COLLECTION_INPUTS: dict[str, dict[str, str]] = {
    "glab": {
        "module": "gitlab.com/gitlab-org/cli",
        "version": "1.111.0",
        "h1": "h1:JeMMkIYiLgYGNWaaXanJAF0K+ND3jLVaeKlXzmFcbrI=",
    },
    "go": {
        "version": "go1.26.5",
        "url": "https://dl.google.com/go/go1.26.5.src.tar.gz",
        "sha256": "495be4bc87176ac567392e5b4116abd98466d33d7b49d41e764ccc6976b2dc42",
    },
    "uv": {
        "version": "0.12.1",
        "url": "https://github.com/astral-sh/uv/archive/refs/tags/0.12.1.tar.gz",
        "sha256": "4e87a6c0e5feddb55e080ba2e8b7ecc1379361471d724071b0c0e0ba5df72981",
    },
    # rust-src of the release whose commit the uv binary embeds; the hash is the one
    # static.rust-lang.org/dist/channel-rust-1.97.1.toml records for it.
    "rust": {
        "version": "1.97.1",
        "commit": "8bab26f4f68e0e26f0bb7960be334d5b520ea452",
        "url": "https://static.rust-lang.org/dist/2026-07-16/rust-src-1.97.1.tar.xz",
        "sha256": "e9a1e616d04c6845895c827a178b9227f7c7199f3f4a80af81ab3aff7b80156b",
    },
}


@dataclass(frozen=True)
class Fallback:
    """One reviewed upstream file for a component whose published archive omits it."""

    path: str
    url: str
    sha256: str
    reason: str
    # A notice that exists only as a source file's license header: `lines` (1-based, inclusive)
    # of the pinned file are installed, never the code, and those bytes are pinned again by
    # `header_sha256`. `embedded_in` names the file of the component's own archive that carries
    # the work; the header has to occur there verbatim, so it is the text that ships.
    lines: tuple[int, int] | None = None
    header_sha256: str = ""
    embedded_in: str = ""

    @property
    def origin(self) -> str:
        return f"{self.url}#L{self.lines[0]}-L{self.lines[1]}" if self.lines else self.url

    @property
    def installed_sha256(self) -> str:
        return self.header_sha256 if self.lines else self.sha256


def _upstream(base: str, reason: str, *files: tuple[str, str], into: str = "") -> list[Fallback]:
    return [Fallback(into + path, f"{base}/{path}", sha256, reason) for path, sha256 in files]


GITHUB_RAW = "https://raw.githubusercontent.com"
AT_WORKSPACE_ROOT = "the license files sit at the workspace root, outside the published crate"
STRIPPED_BY_CRATE = "compiled in; aws-lc/LICENSE points at this file, which the crate strips"
EMBEDDED_BY_D2 = "compiled into glab through d2, whose NOTICE.txt only links to this license"

# Components whose published archive omits a license file: it has none of its own, it strips
# one its license points at, or it embeds another work whose license it only links to. Each file is pinned by hash at the commit the crate's
# .cargo_vcs_info.json names, or, for difflib, which records none, the commit crates.io published
# it from 11 minutes later. A new component without a file fails `collect` rather than silently
# inheriting one, and text is never generated from an SPDX id.
NOTICE_FALLBACKS: dict[str, list[Fallback]] = {
    "cargo/asn1-rs-impl@0.2.0": _upstream(
        f"{GITHUB_RAW}/rusticata/asn1-rs/a20e5f7319c896737ad0f2557037817b91ad854f",
        AT_WORKSPACE_ROOT,
        ("LICENSE-APACHE", "a60eea817514531668d7e00765731449fe14d059d3249e0bc93b36de45f759f2"),
        ("LICENSE-MIT", "a5c61b93b6ee1d104af9920cf020ff3c7efe818e31fe562c72261847a728f513"),
    ),
    # aws-lc at the submodule commit aws-lc-rs d61726b6 (aws-lc-sys 0.39.0) pins. Its LICENSE there
    # is byte-identical to the crate's aws-lc/LICENSE.
    "cargo/aws-lc-sys@0.39.0": _upstream(
        f"{GITHUB_RAW}/aws/aws-lc/47389586f8aa77c83245173793f4d44ed1d6c3a8",
        STRIPPED_BY_CRATE,
        ("third_party/jitterentropy/jitterentropy-library/LICENSE",
         "088d208b9d22530691f1e9406a85dd7f6f5a42e3cf23d3d6df6582408e3840f7"),
        ("third_party/jitterentropy/jitterentropy-library/LICENSE.bsd",
         "13aa749a5b0a454917a944ed8fffc530b784f5ead522b1aacaf4ec8aa55a6239"),
        ("third_party/jitterentropy/jitterentropy-library/LICENSE.gplv2",
         "e6d6a009505e345fe949e1310334fcb0747f28dae2856759de102ab66b722cb4"),
        ("third_party/s2n-bignum/s2n-bignum-imported/LICENSE",
         "41c6380384dc6065456d01405ef0b43e5fe39ba1bccc4ec67801cc66142728e5"),
        ("third_party/s2n-bignum/s2n-bignum-imported/NOTICE",
         "d4290ed64c2edd0fce1d84e3f9dfb2881240fe534def76b8cd29ed6af683e287"),
        into="aws-lc/",
    ),
    "cargo/axoupdater@0.10.0": _upstream(
        f"{GITHUB_RAW}/axodotdev/axoupdater/23d38fb90798eacadfc34e8757e3b111f85532fe",
        AT_WORKSPACE_ROOT,
        ("LICENSE-APACHE", "4fa01c2031933992a1f9b40bd8ecf6d0ecf5b702d8b2f3cd770b7f9a2aca048e"),
        ("LICENSE-MIT", "46ef3b7986a58a36be4e559b9fcb7db2a9b0556705718e74e98efd692467b0eb"),
    ),
    "cargo/cyclonedx-bom-macros@0.1.0": _upstream(
        f"{GITHUB_RAW}/CycloneDX/cyclonedx-rust-cargo/649dcba64d9e0e34e233450e0f1e7b96ea95d02d",
        AT_WORKSPACE_ROOT,
        ("LICENSE", "8d774b8e55770e19ccdbebe68a10c7505efaa6988257e87bfc58bf0c6675817b"),
        ("NOTICE", "b4f86283f298a2b75372e8d2406c8406de1f70e91f0f78a784919ae581ea8de5"),
    ),
    "cargo/defmt-parser@1.0.0": _upstream(
        f"{GITHUB_RAW}/knurling-rs/defmt/4a8cdb44891ed57b8ff5a023b6bec7137c48708f",
        AT_WORKSPACE_ROOT,
        ("LICENSE-APACHE", "8173d5c29b4f956d532781d2b86e4e30f83e6b7878dce18c919451d6ba707c90"),
        ("LICENSE-MIT", "2710a622a896bba67356913d4d0492cab5465f61b2ecce6d880aeb483834fb50"),
    ),
    "cargo/difflib@0.4.0": _upstream(
        f"{GITHUB_RAW}/DimaKudosh/difflib/f035fb8e656f27119e23eca9d5b996df14f3885e",
        "the crate's Cargo.toml `include` list leaves LICENSE out",
        ("LICENSE", "6725d1437fc6c77301f2ff0e7d52914cf4f9509213e1078dc77d9356dbe6eac5"),
    ),
    "cargo/purl@0.1.6": _upstream(
        f"{GITHUB_RAW}/phylum-dev/purl/93152aae4005766d3ea7dcd0e5d8beaee3f0fad8",
        AT_WORKSPACE_ROOT,
        ("LICENSE", "93dd22d1a0fbb1ccb4e320a7ac1f5f58c7338eb11f8893756b7819cd1b62b5e3"),
    ),
    # The one component with no license text anywhere upstream: the repository at the published
    # commit has none, and the crate states its license only as `license = "MIT"` beside its
    # authors in Cargo.toml. That declaration is the whole of the upstream notice, so it is what
    # ships, rather than an MIT text nobody at seahash wrote.
    "cargo/seahash@4.1.0": _upstream(
        "https://gitlab.redox-os.org/redox-os/seahash/-/raw/"
        "94b632aeac099031c373599313d5b5f0acbbaec0",
        "upstream ships no license text; Cargo.toml's license and authors are its only notice",
        ("Cargo.toml", "dd213de338ca98b7ef7a2562bcf4e4214d215d1a8277750a6a72b4e809c8d0dd"),
    ),
    # d2 ships its own license, but compiles in MIT works whose texts its NOTICE.txt files only
    # link to: textmeasure is derived from pixel, d2svg embeds a modified github-markdown.css, and
    # d2dagrelayout embeds a dagre.js d2 bundled itself from dagre 0.8.5, graphlib 2.1.8 and lodash
    # modules. dagre and graphlib are pinned at those tags; lodash at 4.17.21, since both require
    # ^4.17.15 and LICENSE is byte-identical at every 4.17 tag in that range. pixel and
    # github-markdown-css record no version, so each is pinned at the last commit to change the
    # file, which predates d2's code. d2elklayout (elk.js, EPL-2.0) is not linked into glab.
    # The collector cannot see this shape, so a d2 bump makes the entry stale and fails `collect`.
    "golang/oss.terrastruct.com/d2@v0.7.1": [
        *_upstream(
            f"{GITHUB_RAW}/faiface/pixel/ca6a9865f60decf8a7da9497342f9b317154ad1d",
            EMBEDDED_BY_D2,
            ("LICENSE", "cf169f83e2cecbc3e5ed7a0c91ecebf2cfe1ed41916830e665f17f8d98d30b07"),
            into="lib/textmeasure/faiface-pixel/",
        ),
        *_upstream(
            f"{GITHUB_RAW}/sindresorhus/github-markdown-css/28b11434c1355a09e32b1d169ae2e0b49bcf3896",
            EMBEDDED_BY_D2,
            ("license", "5c932d88256b4ab958f64a856fa48e8bd1f55bc1d96b8149c65689e0c61789d3"),
            into="d2renderers/d2svg/github-markdown-css/",
        ),
        *_upstream(
            f"{GITHUB_RAW}/dagrejs/dagre/f56edb1abbb8530e532158f7cbd403228f5b0018",
            EMBEDDED_BY_D2,
            ("LICENSE", "6a349742a6cb219d5a2fc8d0844f6d89a6efc62e20c664450d884fc7ff2d6015"),
            into="d2layouts/d2dagrelayout/dagre/",
        ),
        *_upstream(
            f"{GITHUB_RAW}/dagrejs/graphlib/64375bb8d96bce0d906d238853c2b5afa2f2c231",
            EMBEDDED_BY_D2,
            ("LICENSE", "6a349742a6cb219d5a2fc8d0844f6d89a6efc62e20c664450d884fc7ff2d6015"),
            into="d2layouts/d2dagrelayout/graphlib/",
        ),
        # graphlib's index.js opens with a BSD-3-Clause header, not its repository's MIT text, and
        # the bundle keeps it whole. Its binary condition asks for that notice in the
        # documentation, so the header ships too, taken only as the lines of the pinned file.
        Fallback(
            "d2layouts/d2dagrelayout/graphlib/LICENSE-index-js-header",
            f"{GITHUB_RAW}/dagrejs/graphlib/64375bb8d96bce0d906d238853c2b5afa2f2c231/index.js",
            "107d1ad79744518779d70f536742c3e82cf767c086257b9c67d2d5c23af52138",
            "compiled into glab through d2's dagre.js, which keeps this BSD header of graphlib's",
            lines=(1, 29),
            header_sha256="5658fe07b56a91a4364bcbe25c0ea9e4a0ba9722879272b757f4f84b857d4956",
            embedded_in="d2layouts/d2dagrelayout/dagre.js",
        ),
        *_upstream(
            f"{GITHUB_RAW}/lodash/lodash/f299b52f39486275a9e6483b60a410e06520c538",
            EMBEDDED_BY_D2,
            ("LICENSE", "f71e8ed126b46346494aad5486874cd8f0aafe95092ed67d2e3cb6110f939abc"),
            into="d2layouts/d2dagrelayout/lodash/",
        ),
    ],
}

# Where each binary the SBOM records is installed, and which notice tree covers it.
IMAGE_BINARIES: dict[str, str] = {
    "/usr/bin/glab": "glab",
    "/usr/local/bin/uv": "uv",
    "/usr/local/bin/uvx": "uv",
}
# The SBOM package types whose components are compiled into those binaries. Debian and Python
# packages carry their own notices, under /usr/share/doc and in dist-info.
COMPILED_KINDS = frozenset({"cargo", "golang"})

# A notice is a file named like one anywhere in a component or, at the component's root only, a
# file in a directory named like one (REUSE's LICENSES/, freetype's licenses/ftl.txt) or a file
# named for its SPDX id (priority-queue's MPL-2.0.txt). The root restriction keeps the spdx
# crate's embedded license database out. A name has to end, or continue with a separator, right
# after its stem, so `LICENSE-MIT` and `COPYING.LESSER` match and `noticeboard.txt` does not.
# AUTHORS is kept because "Copyright (c) The Foo Authors" is a notice that points at it.
NOTICE_NAME = re.compile(
    r"^(?:licen[cs]e|unlicen[cs]e|copying|copyright|notice|patents|authors|credits)(?:$|[-._ ])",
    re.IGNORECASE,
)
NOTICE_DIRS = frozenset({"license", "licenses", "licence", "licences"})
SPDX_NAMED = re.compile(
    r"^(?:0BSD|AGPL|Apache|BSD|BSL|CC0|EPL|GPL|ISC|LGPL|MIT|MPL|Unicode|Unlicense|Zlib)"
    r"(?:-[A-Za-z0-9.+-]*)?(?:\.txt|\.md)?$"
)
# Source never enters the tree: the notices are license text, and MPL source availability is a
# pointer (docs/LICENSE_REVIEW.md), not a copy. `license.go` or `copyright_test.go` is code, whatever its name says.
SOURCE_SUFFIXES = frozenset(
    {
        ".asm", ".bzl", ".c", ".cc", ".cmake", ".cpp", ".css", ".go", ".h", ".hpp", ".java",
        ".js", ".json", ".lock", ".mk", ".mod", ".pl", ".png", ".proto", ".py", ".rb", ".rs",
        ".s", ".sh", ".sum", ".svg", ".tmpl", ".toml", ".tpl", ".ts", ".xml", ".yaml", ".yml",
    }
)
# Never compiled: the Go tool ignores testdata, and a notice found there describes a fixture.
SKIPPED_DIRS = frozenset({"testdata"})

FREETYPE_MODULE = "github.com/golang/freetype"
# The FTL's binary-redistribution condition, and the year range freetype-go's AUTHORS gives for
# the FreeType code it derives from. `collect` fails if either source stops saying so, because
# CREDITS quotes them.
FREETYPE_CONDITION = "based in part of the work of the FreeType Team"
FREETYPE_YEARS = "copyright 1996-2010 David Turner, Robert Wilhelm, and Werner Lemberg"


class NoticeError(RuntimeError):
    """The notices cannot be collected, or do not cover what the image ships."""


# --- Reading the shipping binaries -----------------------------------------------------------


GO_BUILDINFO_MAGIC = b"\xff Go buildinf:"


@dataclass(frozen=True)
class GoBuild:
    go_version: str
    main_module: str
    main_version: str
    deps: tuple[tuple[str, str], ...]


def _varint_string(data: bytes, position: int) -> tuple[bytes, int]:
    length = shift = 0
    while True:
        if position >= len(data) or shift > 63:
            raise NoticeError("truncated Go build information")
        byte = data[position]
        position += 1
        length |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return data[position:position + length], position + length


def _modules(modinfo: str) -> tuple[tuple[str, str], list[tuple[str, str]]]:
    """The main module and the linked modules. A replacement stands in for the module before it.

    A directory replacement has no version to fetch notices for, and fails.
    """
    main: tuple[str, str] | None = None
    deps: list[tuple[str, str]] = []
    for line in modinfo.splitlines():
        fields = line.split("\t")
        if fields[0] not in ("mod", "dep", "=>"):
            continue
        if len(fields) < 3 or (fields[0] != "mod" and not fields[2].startswith("v")):
            raise NoticeError(f"module line {line!r} has no version; a directory replacement?")
        if fields[0] == "mod":
            main = (fields[1], fields[2])
        elif fields[0] == "dep":
            deps.append((fields[1], fields[2]))
        elif deps:
            deps[-1] = (fields[1], fields[2])
        else:
            raise NoticeError("a module replacement precedes any dependency")
    if main is None:
        raise NoticeError("the binary names no main module")
    return main, deps


def read_go_buildinfo(data: bytes) -> GoBuild:
    """The toolchain and modules the Go linker embedded, which is what Syft reads too.

    Only the Go 1.18+ layout, which stores both strings inline after the header, is read.
    """
    offset = data.find(GO_BUILDINFO_MAGIC)
    while offset != -1 and offset % 16:
        offset = data.find(GO_BUILDINFO_MAGIC, offset + 1)
    if offset == -1:
        raise NoticeError("no Go build information in the binary")
    if not data[offset + 15] & 0x2:
        raise NoticeError("Go build information predates Go 1.18; its layout is not supported")
    version, position = _varint_string(data, offset + 32)
    modinfo, _ = _varint_string(data, position)
    if len(modinfo) < 33:
        raise NoticeError("the binary carries no module information")
    main, deps = _modules(modinfo[16:-16].decode())
    return GoBuild(version.decode(), main[0], main[1], tuple(sorted(deps)))


def elf_section(data: bytes, name: str) -> bytes:
    """One named section of a little-endian ELF64 file."""
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise NoticeError("not a little-endian ELF64 binary")
    (section_offset,) = struct.unpack_from("<Q", data, 0x28)
    entry_size, count, names_index = struct.unpack_from("<HHH", data, 0x3A)
    headers = [
        struct.unpack_from("<IIQQQQIIQQ", data, section_offset + index * entry_size)
        for index in range(count)
    ]
    names_offset = headers[names_index][4]
    wanted = name.encode()
    for header in headers:
        start = names_offset + header[0]
        if data[start:data.index(b"\0", start)] == wanted:
            return data[header[4]:header[4] + header[5]]
    raise NoticeError(f"the binary has no {name} section")


@dataclass(frozen=True)
class RustBuild:
    rustc_commit: str
    root: tuple[str, str]
    packages: tuple[tuple[str, str, str], ...]


def read_rust_build(data: bytes) -> RustBuild:
    """The crates cargo-auditable embedded, and the rustc commit the binary names.

    Build-only crates (build scripts, proc-macros) are left out, as Syft leaves them out: they run
    while compiling and are not linked into the binary.
    """
    try:
        audit = json.loads(zlib.decompress(elf_section(data, ".dep-v0")))
    except (zlib.error, json.JSONDecodeError) as exc:
        raise NoticeError(f"unreadable cargo-auditable data: {exc}") from exc
    packages = audit.get("packages") if isinstance(audit, dict) else None
    if not isinstance(packages, list):
        raise NoticeError("cargo-auditable data lists no packages")
    roots = [(str(p["name"]), str(p["version"])) for p in packages if p.get("root")]
    if len(roots) != 1:
        raise NoticeError(f"expected one root crate in cargo-auditable data, found {len(roots)}")
    runtime = sorted(
        (str(p["name"]), str(p["version"]), str(p.get("source", "")))
        for p in packages
        if p.get("kind", "runtime") != "build"
    )
    commits = set(re.findall(rb"/rustc/([0-9a-f]{40})/", data))
    if len(commits) != 1:
        raise NoticeError(f"expected the binary to name one rustc commit, found {len(commits)}")
    return RustBuild(commits.pop().decode(), roots[0], tuple(runtime))


# --- Upstream archives ------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def go_module_hash(archive: zipfile.ZipFile) -> str:
    """The `h1:` hash go.sum and sum.golang.org record for a module zip (dirhash Hash1)."""
    summary = "".join(
        f"{sha256_hex(archive.read(name))}  {name}\n" for name in sorted(archive.namelist())
    )
    return "h1:" + base64.b64encode(hashlib.sha256(summary.encode()).digest()).decode()


def go_proxy_escape(value: str) -> str:
    """Module proxy case-encoding: an upper-case letter becomes `!` and its lower-case form."""
    return re.sub(r"[A-Z]", lambda match: "!" + match.group(0).lower(), value)


def go_module_url(module: str, version: str) -> str:
    return f"https://proxy.golang.org/{go_proxy_escape(module)}/@v/{go_proxy_escape(version)}.zip"


def crate_url(name: str, version: str) -> str:
    return f"https://static.crates.io/crates/{name}/{name}-{version}.crate"


def is_notice_path(relative: str) -> bool:
    parts = relative.split("/")
    if any(part in SKIPPED_DIRS for part in parts[:-1]):
        return False
    # Below the root, a licenses/ directory is data -- the spdx crate's license database -- and
    # nothing in it is a license the crate is under.
    if any(part.lower() in NOTICE_DIRS for part in parts[1:-1]):
        return False
    if PurePosixPath(parts[-1]).suffix.lower() in SOURCE_SUFFIXES:
        return False
    if NOTICE_NAME.match(parts[-1]):
        return True
    if len(parts) == 1:
        return bool(SPDX_NAMED.match(parts[0]))
    return len(parts) == 2 and parts[0].lower() in NOTICE_DIRS


def safe_relative(name: str, prefix: str) -> str | None:
    """The path of an archive member below `prefix`, or None when it lies elsewhere."""
    if not name.startswith(prefix):
        return None
    relative = name[len(prefix):]
    parts = relative.split("/")
    if not relative or relative.startswith("/") or ".." in parts or "" in parts:
        raise NoticeError(f"unsafe archive member name {name!r}")
    return relative


def zip_notices(archive: zipfile.ZipFile, prefix: str) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        relative = safe_relative(info.filename, prefix)
        if relative is not None and is_notice_path(relative):
            found[relative] = archive.read(info)
    return found


def zip_member(archive: zipfile.ZipFile, prefix: str) -> Callable[[str], bytes]:
    return lambda name: archive.read(prefix + name)


def tar_members(data: bytes) -> dict[str, bytes]:
    """Every regular file of a tar archive by name. Links are dropped, never followed."""
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            handle = archive.extractfile(member)
            if handle is not None:
                members[member.name.removeprefix("./")] = handle.read()
    return members


def tar_notices(
    members: Mapping[str, bytes], prefix: str, include: Callable[[str], bool] = is_notice_path
) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for name, data in members.items():
        relative = safe_relative(name, prefix)
        if relative is not None and include(relative):
            found[relative] = data
    return found


class Fetcher:
    """HTTPS downloads, cached on disk by URL. The caller checks integrity, never the cache."""

    def __init__(self, cache: Path) -> None:
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)

    def path(self, url: str) -> Path:
        return self.cache / sha256_hex(url.encode())

    def ensure(self, url: str) -> None:
        target = self.path(url)
        if target.exists():
            return
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        last: Exception | None = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    data = response.read()
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
        else:
            raise NoticeError(f"GET {url} failed: {last}")
        partial = target.with_suffix(".part")
        partial.write_bytes(data)
        partial.replace(target)

    def get(self, url: str) -> bytes:
        self.ensure(url)
        return self.path(url).read_bytes()

    def prefetch(self, urls: Iterable[str]) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(self.ensure, sorted(set(urls))))


def pinned_bytes(fetcher: Fetcher, url: str, sha256: str) -> bytes:
    data = fetcher.get(url)
    if sha256_hex(data) != sha256:
        raise NoticeError(f"{url} has SHA-256 {sha256_hex(data)}, not the pinned {sha256}")
    return data


def fallback_bytes(
    fetcher: Fetcher, fallback: Fallback, member: Callable[[str], bytes] | None
) -> bytes:
    data = pinned_bytes(fetcher, fallback.url, fallback.sha256)
    if fallback.lines is None:
        return data
    first, last = fallback.lines
    header = b"".join(data.splitlines(keepends=True)[first - 1 : last])
    if sha256_hex(header) != fallback.header_sha256:
        raise NoticeError(f"{fallback.origin} hashes to {sha256_hex(header)}, not the pinned header")
    if not fallback.embedded_in or member is None:
        raise NoticeError(f"{fallback.origin} is a header, so it needs the file that embeds it")
    try:
        carrier = member(fallback.embedded_in)
    except KeyError:
        raise NoticeError(f"the component has no {fallback.embedded_in} to carry the header") from None
    if header not in carrier:
        raise NoticeError(f"{fallback.embedded_in} does not carry the header {fallback.origin}")
    return header


# --- Assembling a tree ------------------------------------------------------------------------


@dataclass
class Notices:
    """One binary's tree: file bytes and origins by path, and the files covering each component."""

    fetcher: Fetcher
    files: dict[str, bytes] = field(default_factory=dict)
    origins: dict[str, str] = field(default_factory=dict)
    components: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add_file(self, path: str, data: bytes, origin: str) -> str:
        if path in self.files and self.files[path] != data:
            raise NoticeError(f"two different files would be written to {path}")
        self.files[path] = data
        self.origins.setdefault(path, origin)
        return path

    def add_files(
        self,
        key: str,
        base: str,
        found: Mapping[str, bytes],
        origin: str,
        member: Callable[[str], bytes] | None = None,
    ) -> list[str]:
        """A component's own notice files, plus any reviewed fallback, under its directory.

        `member` reads a file of the component's own archive, for a fallback header to be found in.
        """
        paths = [
            self.add_file(f"{base}/{relative}", data, f"{origin}#{relative}")
            for relative, data in sorted(found.items())
        ]
        for fallback in NOTICE_FALLBACKS.get(key, []):
            data = fallback_bytes(self.fetcher, fallback, member)
            paths.append(self.add_file(f"{base}/{fallback.path}", data, fallback.origin))
        return paths

    def add_component(self, key: str, source: str, integrity: str, paths: Iterable[str]) -> None:
        listed = sorted(set(paths))
        if not listed:
            raise NoticeError(
                f"{key} ships no license or notice file in {source}; add a reviewed entry to "
                "NOTICE_FALLBACKS rather than generating one"
            )
        self.components[key] = {"source": source, "integrity": integrity, "files": listed}


def go_sum_hashes(text: str) -> dict[tuple[str, str], str]:
    hashes: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 3 and not fields[1].endswith("/go.mod"):
            hashes[(fields[0], fields[1])] = fields[2]
    return hashes


def verified_module(fetcher: Fetcher, module: str, version: str, expected: str) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(io.BytesIO(fetcher.get(go_module_url(module, version))))
    actual = go_module_hash(archive)
    if actual != expected:
        raise NoticeError(f"{module}@{version} from the proxy hashes to {actual}, not {expected}")
    return archive


def check_sumdb(fetcher: Fetcher, module: str, version: str, expected: str) -> None:
    """The checksum database has to agree with the pin, independently of the proxy."""
    url = f"https://sum.golang.org/lookup/{go_proxy_escape(module)}@{go_proxy_escape(version)}"
    if f"{module} {version} {expected}\n" not in fetcher.get(url).decode():
        raise NoticeError(f"sum.golang.org does not record {expected} for {module}@{version}")


def freetype_credits(archive: zipfile.ZipFile, prefix: str, version: str, ftl: str) -> bytes:
    def text(name: str) -> str:
        return " ".join(archive.read(prefix + name).decode("latin-1").split())

    if FREETYPE_CONDITION not in text("licenses/ftl.txt") or FREETYPE_YEARS not in text("AUTHORS"):
        raise NoticeError(
            f"{FREETYPE_MODULE}@{version} no longer states the credit condition or the year "
            "range CREDITS quotes; review the FreeType credit by hand"
        )
    return (
        "Credits required by the licenses of components compiled into glab\n"
        "\n"
        f"glab compiles in Freetype-Go, {FREETYPE_MODULE} {version}.\n"
        "It is licensed under your choice of the FreeType License or the GNU General\n"
        "Public License, version 2 or later. This distribution takes the FreeType\n"
        "License, whose condition for redistribution in binary form this file meets:\n"
        "\n"
        "    This software is based in part on the work of the FreeType Team.\n"
        "\n"
        "    Portions of this software are copyright (c) 1996-2010 The FreeType\n"
        "    Project (www.freetype.org). All rights reserved.\n"
        "\n"
        "The FreeType License text is in\n"
        f"{ftl}.\n"
    ).encode()


def glab_build(binary: bytes) -> GoBuild:
    pins, go_pin = COLLECTION_INPUTS["glab"], COLLECTION_INPUTS["go"]
    build = read_go_buildinfo(binary)
    if (build.main_module, build.main_version) != (pins["module"], "v" + pins["version"]):
        raise NoticeError(
            f"the glab binary is {build.main_module}@{build.main_version}, "
            f"not the pinned {pins['module']}@v{pins['version']}"
        )
    if build.go_version != go_pin["version"]:
        raise NoticeError(
            f"glab was built with {build.go_version}; move COLLECTION_INPUTS['go'] from "
            f"{go_pin['version']} to that release"
        )
    return build


def collect_glab(binary: bytes, fetcher: Fetcher) -> tuple[Notices, dict[str, Any]]:
    pins, go_pin = COLLECTION_INPUTS["glab"], COLLECTION_INPUTS["go"]
    build = glab_build(binary)
    notices = Notices(fetcher)
    module, version = pins["module"], "v" + pins["version"]
    check_sumdb(fetcher, module, version, pins["h1"])
    main_zip = verified_module(fetcher, module, version, pins["h1"])
    main_prefix, origin = f"{module}@{version}/", go_module_url(module, version)
    sums = go_sum_hashes(main_zip.read(main_prefix + "go.sum").decode())

    # glab's own license is the Debian-style copyright file; any other notice it ships sits with
    # the rest.
    own = zip_notices(main_zip, main_prefix)
    if "LICENSE" not in own:
        raise NoticeError(f"{module}@{version} has no top-level LICENSE")
    copyright_path = notices.add_file("copyright", own.pop("LICENSE"), f"{origin}#LICENSE")
    key = f"golang/{module}@{version}"
    own_paths = notices.add_files(key, f"third-party/{key}", own, origin)
    notices.add_component(key, origin, pins["h1"], [copyright_path, *own_paths])
    notices.add_component(f"deb/glab@{pins['version']}", origin, pins["h1"], [copyright_path])

    fetcher.prefetch(go_module_url(dep, dep_version) for dep, dep_version in build.deps)
    for dep, dep_version in build.deps:
        if (dep, dep_version) not in sums:
            raise NoticeError(f"{dep}@{dep_version} is linked into glab but absent from its go.sum")
        expected, url = sums[(dep, dep_version)], go_module_url(dep, dep_version)
        archive = verified_module(fetcher, dep, dep_version, expected)
        key, prefix = f"golang/{dep}@{dep_version}", f"{dep}@{dep_version}/"
        found, member = zip_notices(archive, prefix), zip_member(archive, prefix)
        paths = notices.add_files(key, f"third-party/{key}", found, url, member)
        if dep == FREETYPE_MODULE:
            ftl = f"third-party/{key}/licenses/ftl.txt"
            credits = freetype_credits(archive, prefix, dep_version, ftl)
            paths.append(notices.add_file(CREDITS_NAME, credits, GENERATED))
        notices.add_component(key, url, expected, paths)

    stdlib = tar_members(pinned_bytes(fetcher, go_pin["url"], go_pin["sha256"]))
    # src/cmd is the toolchain's own commands, never linked into a program; src/vendor is.
    found = tar_notices(
        stdlib, "go/", lambda path: is_notice_path(path) and not path.startswith("src/cmd/")
    )
    key = f"golang/stdlib@{go_pin['version']}"
    paths = notices.add_files(key, f"third-party/{key}", found, go_pin["url"])
    notices.add_component(key, go_pin["url"], "sha256:" + go_pin["sha256"], paths)
    return notices, {"glab": pins, "go": go_pin}


def workspace_crates(
    members: Mapping[str, bytes], prefix: str
) -> dict[tuple[str, str], tuple[str, bool]]:
    """uv's crates: (name, version) -> (crate directory, whether it inherits uv's license)."""
    crates: dict[tuple[str, str], tuple[str, bool]] = {}
    for name, data in members.items():
        match = re.fullmatch(re.escape(prefix) + r"(crates/[^/]+/)Cargo\.toml", name)
        if not match:
            continue
        package = tomllib.loads(data.decode()).get("package", {})
        version = package.get("version")
        if not isinstance(version, str):
            continue
        license_value = package.get("license")
        inherited = isinstance(license_value, dict) and license_value.get("workspace") is True
        crates[(package["name"], version)] = (match.group(1), inherited)
    return crates


def cargo_lock_checksums(text: str) -> dict[tuple[str, str], str]:
    checksums: dict[tuple[str, str], str] = {}
    for package in tomllib.loads(text).get("package", []):
        if package.get("source") == "registry+https://github.com/rust-lang/crates.io-index":
            checksums[(package["name"], package["version"])] = package["checksum"]
    return checksums


def crate_notices(data: bytes, name: str, version: str) -> dict[str, bytes]:
    """The notice files of a .crate, plus whatever its Cargo.toml names as `license-file`."""
    prefix = f"{name}-{version}/"
    members = tar_members(data)
    found = tar_notices(members, prefix)
    manifest = members.get(prefix + "Cargo.toml")
    if manifest is not None:
        license_file = tomllib.loads(manifest.decode()).get("package", {}).get("license-file")
        if isinstance(license_file, str) and prefix + license_file in members:
            relative = safe_relative(prefix + license_file, prefix)
            if relative is not None:
                found[relative] = members[prefix + license_file]
    return found


def uv_build(binary: bytes) -> RustBuild:
    pins, rust_pin = COLLECTION_INPUTS["uv"], COLLECTION_INPUTS["rust"]
    build = read_rust_build(binary)
    if build.root != ("uv", pins["version"]):
        raise NoticeError(f"the uv binary is {build.root}, not the pinned uv {pins['version']}")
    if build.rustc_commit != rust_pin["commit"]:
        raise NoticeError(
            f"uv was built by rustc {build.rustc_commit}; move COLLECTION_INPUTS['rust'] to the "
            "release with that commit"
        )
    return build


def rust_std_notices(fetcher: Fetcher) -> dict[str, bytes]:
    pin = COLLECTION_INPUTS["rust"]
    members = tar_members(pinned_bytes(fetcher, pin["url"], pin["sha256"]))
    # The component installs the source under rust-src/lib/rustlib/src/rust/. Dropping that gives
    # the paths the rust-lang/rust repository uses: library/vendor/hashbrown-.../LICENSE-MIT.
    found: dict[str, bytes] = {}
    for relative, data in tar_notices(members, f"rust-src-{pin['version']}/").items():
        short = relative.removeprefix("rust-src/lib/rustlib/src/rust/")
        if found.get(short, data) != data:
            raise NoticeError(f"rust-src carries two different {short}")
        found[short] = data
    return found


def collect_uv(binary: bytes, fetcher: Fetcher) -> tuple[Notices, dict[str, Any]]:
    pins, rust_pin = COLLECTION_INPUTS["uv"], COLLECTION_INPUTS["rust"]
    build = uv_build(binary)
    notices = Notices(fetcher)
    source = tar_members(pinned_bytes(fetcher, pins["url"], pins["sha256"]))
    prefix, integrity = f"uv-{pins['version']}/", "sha256:" + pins["sha256"]
    checksums = cargo_lock_checksums(source[prefix + "Cargo.lock"].decode())
    workspace = workspace_crates(source, prefix)
    # uv's own license, at its repository root, which the workspace crates inherit.
    root = tar_notices(source, prefix, lambda path: "/" not in path and is_notice_path(path))
    root_paths = notices.add_files("", f"third-party/cargo/uv@{pins['version']}", root, pins["url"])

    fetcher.prefetch(crate_url(n, v) for n, v, origin in build.packages if origin == "crates.io")
    for name, version, origin in build.packages:
        key, base = f"cargo/{name}@{version}", f"third-party/cargo/{name}@{version}"
        if origin == "crates.io" and (name, version) in checksums:
            url, checksum = crate_url(name, version), checksums[(name, version)]
            found = crate_notices(pinned_bytes(fetcher, url, checksum), name, version)
            paths = notices.add_files(key, base, found, url)
            notices.add_component(key, url, "sha256:" + checksum, paths)
        elif origin == "local" and (name, version) in workspace:
            directory, inherited = workspace[(name, version)]
            found = tar_notices(source, prefix + directory)
            paths = notices.add_files(key, base, found, f"{pins['url']}#{directory}")
            paths += root_paths if inherited else []
            notices.add_component(key, pins["url"], integrity, paths)
        else:
            raise NoticeError(f"{key} comes from {origin!r} and matches nothing in uv's Cargo.lock")

    key = f"rust/std@{rust_pin['version']}"
    paths = notices.add_files(key, f"third-party/{key}", rust_std_notices(fetcher), rust_pin["url"])
    notices.add_component(key, rust_pin["url"], "sha256:" + rust_pin["sha256"], paths)
    return notices, {"uv": pins, "rust": rust_pin}


# --- Writing and reading a tree ---------------------------------------------------------------


README_TEXT = {
    "glab": (
        "License notices for glab, the GitLab CLI, as installed in this image\n"
        "\n"
        "glab is installed from GitLab's release .deb, which carries no license text.\n"
        "This directory supplies the notices that package omits:\n"
        "\n"
        "  copyright     glab's own license: LICENSE of gitlab.com/gitlab-org/cli {version}\n"
        "  CREDITS       credits a compiled-in component's license requires\n"
        "  third-party/  the license and notice files of every Go module compiled into\n"
        "                /usr/bin/glab, and of the Go standard library, one directory\n"
        "                per module: golang/<module>@<version>/\n"
        "  notices.json  the files that cover each component, where each was taken\n"
        "                from, and its SHA-256\n"
        "\n"
        "Every file other than this README and CREDITS is the unmodified upstream file.\n"
    ),
    "uv": (
        "License notices for uv and uvx, as installed in this image\n"
        "\n"
        "uv is installed from its PyPI wheel, whose dist-info carries uv's own license\n"
        "but not those of the crates compiled into it. This directory supplies them:\n"
        "\n"
        "  third-party/  the license and notice files of every crate compiled into\n"
        "                /usr/local/bin/uv and /usr/local/bin/uvx, one directory per\n"
        "                crate: cargo/<crate>@<version>/. rust/std@<version>/ holds the\n"
        "                Rust standard library's, including the crates it vendors.\n"
        "                Workspace crates that inherit uv's license list uv's own\n"
        "                files, in cargo/uv@{version}/.\n"
        "  notices.json  the files that cover each component, where each was taken\n"
        "                from, and its SHA-256\n"
        "\n"
        "Every file other than this README is the unmodified upstream file.\n"
    ),
}


def manifest_document(binary: str, notices: Notices, inputs: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "binary": binary,
        "description": (
            "License and notice files for the components compiled into this binary, keyed as "
            "the release SBOM keys them (type/name@version). Written by "
            f"{GENERATED}; paths are relative to this file."
        ),
        "inputs": {name: dict(inputs[name]) for name in sorted(inputs)},
        "components": {key: notices.components[key] for key in sorted(notices.components)},
        "files": {
            path: {"sha256": sha256_hex(notices.files[path]), "origin": notices.origins[path]}
            for path in sorted(notices.files)
        },
    }


def write_tree(target: Path, binary: str, notices: Notices, inputs: Mapping[str, Any]) -> None:
    """Replace one binary's tree wholesale, so a file that is no longer collected disappears."""
    version = COLLECTION_INPUTS[binary]["version"]
    readme = README_TEXT[binary].format(version=version if binary == "uv" else "v" + version)
    notices.add_file(README_NAME, readme.encode(), GENERATED)
    staging = Path(tempfile.mkdtemp(prefix=f".{binary}-", dir=target.parent))
    try:
        for path, data in notices.files.items():
            destination = staging / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(0o644)
        document = json.dumps(manifest_document(binary, notices, inputs), indent=2, sort_keys=True)
        (staging / MANIFEST_NAME).write_text(document + "\n", encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def load_manifest(tree: Path) -> dict[str, Any]:
    try:
        document = json.loads((tree / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NoticeError(f"cannot read {tree / MANIFEST_NAME}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != MANIFEST_SCHEMA:
        raise NoticeError(f"{tree / MANIFEST_NAME} has an unsupported schema")
    for section in ("inputs", "components", "files"):
        if not isinstance(document.get(section), dict):
            raise NoticeError(f"{tree / MANIFEST_NAME} has no {section} object")
    return document


def verify_tree(tree: Path, manifest: Mapping[str, Any], *, exhaustive: bool) -> list[str]:
    """Every listed file is present, readable and the recorded bytes; every component has one.

    `exhaustive` also rejects a file the manifest does not list, which is what makes the tree
    reproducible from its manifest. It is for the committed tree; in the image the check is on
    what the manifest promises.
    """
    problems: list[str] = []
    files: Mapping[str, Any] = manifest["files"]
    for path, record in sorted(files.items()):
        try:
            data = (tree / path).read_bytes()
        except OSError as exc:
            problems.append(f"{tree / path}: unreadable ({exc.strerror or exc})")
            continue
        if sha256_hex(data) != record.get("sha256"):
            problems.append(f"{tree / path}: SHA-256 differs from {MANIFEST_NAME}")
    for key, component in sorted(manifest["components"].items()):
        listed = component.get("files") or []
        if not listed:
            problems.append(f"{key}: no notice file")
        problems.extend(f"{key}: lists unrecorded {path}" for path in listed if path not in files)
    if exhaustive:
        present = {path.relative_to(tree).as_posix() for path in tree.rglob("*") if path.is_file()}
        unlisted = sorted(present - set(files) - {MANIFEST_NAME})
        problems.extend(f"{tree / path}: not recorded in {MANIFEST_NAME}" for path in unlisted)
    return problems


def coverage_problems(
    entries: Iterable[Entry],
    manifests: Mapping[str, Mapping[str, Any]],
    binaries: Mapping[str, str],
) -> list[str]:
    """Every compiled-in component the SBOM lists has at least one notice file."""
    problems: set[str] = set()
    for entry in entries:
        if entry.kind not in COMPILED_KINDS:
            continue
        tree = binaries.get(entry.location)
        if tree is None or tree not in manifests:
            where = entry.location or "an unknown location"
            problems.add(f"{entry.key} in {where}: no notices tree covers that binary")
            continue
        component = manifests[tree]["components"].get(entry.key)
        if not component or not component.get("files"):
            problems.add(f"{entry.key} in {entry.location}: no notice in the {tree} tree")
    return sorted(problems)


def binary_problems(tree: str, binary: bytes, manifest: Mapping[str, Any]) -> list[str]:
    """The installed binary is the build the tree was collected from, with nothing uncovered.

    This holds without the SBOM: it reads the same build data Syft reads, straight from the file.
    """
    inputs, problems = manifest["inputs"], []
    if tree == "glab":
        go = read_go_buildinfo(binary)
        built = {"toolchain": go.go_version, "version": go.main_version}
        pinned = {"toolchain": inputs["go"]["version"], "version": "v" + inputs["glab"]["version"]}
        keys = [f"golang/{module}@{version}" for module, version in go.deps]
    else:
        rust = read_rust_build(binary)
        built = {"toolchain": rust.rustc_commit, "version": rust.root[1]}
        pinned = {"toolchain": inputs["rust"]["commit"], "version": inputs["uv"]["version"]}
        keys = [f"cargo/{name}@{version}" for name, version, _ in rust.packages]
    for field_name in ("toolchain", "version"):
        if built[field_name] != pinned[field_name]:
            problems.append(
                f"the {tree} binary's {field_name} is {built[field_name]}; "
                f"the notices cover {pinned[field_name]}"
            )
    components = manifest["components"]
    problems.extend(
        f"{key} is linked into {tree} but has no notices" for key in keys if key not in components
    )
    return problems


def check_image(sbom: Mapping[str, Any], doc_root: Path, binaries: Mapping[str, str]) -> list[str]:
    """What a recipient of the image receives. Runs inside the built image, as the app user."""
    manifests: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for tree in sorted(set(binaries.values())):
        manifests[tree] = load_manifest(doc_root / tree)
        problems.extend(verify_tree(doc_root / tree, manifests[tree], exhaustive=False))
    problems.extend(coverage_problems(sbom_entries(sbom), manifests, binaries))
    for location, tree in sorted(binaries.items()):
        problems.extend(binary_problems(tree, Path(location).read_bytes(), manifests[tree]))
    return problems


# --- Command line -----------------------------------------------------------------------------


def dockerfile_pins(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    return dict(re.findall(r"^ARG (GLAB_VERSION|UV_VERSION)=(\S+)$", text, re.MULTILINE))


def run_collect(args: argparse.Namespace) -> int:
    pins = dockerfile_pins(Path("Dockerfile"))
    wanted = {
        "GLAB_VERSION": COLLECTION_INPUTS["glab"]["version"],
        "UV_VERSION": COLLECTION_INPUTS["uv"]["version"],
    }
    if pins != wanted:
        raise NoticeError(f"the Dockerfile pins {pins}; COLLECTION_INPUTS pins {wanted}")
    fetcher = Fetcher(args.cache or Path(tempfile.mkdtemp(prefix="notices-cache-")))
    collected = {
        "glab": collect_glab(args.glab_binary.read_bytes(), fetcher),
        "uv": collect_uv(args.uv_binary.read_bytes(), fetcher),
    }
    # A fallback for a component nothing links any more is a reviewed answer to a question nobody
    # is asking; it would sit unread until a later version happened to reuse the key.
    used = {key for notices, _ in collected.values() for key in notices.components}
    stale = sorted(set(NOTICE_FALLBACKS) - used)
    if stale:
        raise NoticeError(f"NOTICE_FALLBACKS covers components nothing links: {', '.join(stale)}")
    args.output.mkdir(parents=True, exist_ok=True)
    for binary, (notices, inputs) in collected.items():
        write_tree(args.output / binary, binary, notices, inputs)
        counts = f"{len(notices.components)} components, {len(notices.files)} files"
        print(f"[notices] {binary}: {counts}")
    return 0


def run_verify(args: argparse.Namespace) -> int:
    trees = sorted(set(IMAGE_BINARIES.values()))
    manifests = {tree: load_manifest(args.notices / tree) for tree in trees}
    problems = [
        problem
        for tree, manifest in manifests.items()
        for problem in verify_tree(args.notices / tree, manifest, exhaustive=True)
    ]
    if args.sbom:
        entries = sbom_entries(json.loads(args.sbom.read_bytes()))
        problems.extend(coverage_problems(entries, manifests, IMAGE_BINARIES))
    count = sum(len(manifest["components"]) for manifest in manifests.values())
    return report(problems, f"{count} components verified")


def run_check_image(args: argparse.Namespace) -> int:
    raw = sys.stdin.buffer.read() if str(args.sbom) == "-" else args.sbom.read_bytes()
    problems = check_image(json.loads(raw), args.doc_root, IMAGE_BINARIES)
    return report(problems, "every compiled-in component the SBOM lists has notices in the image")


def report(problems: Sequence[str], success: str) -> int:
    for problem in problems:
        print(f"[notices] MISSING: {problem}", file=sys.stderr)
    if problems:
        print(f"[notices] ERROR: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(f"[notices] {success}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="regenerate notices/ from the binaries (network)")
    collect.add_argument("--glab-binary", type=Path, required=True)
    collect.add_argument("--uv-binary", type=Path, required=True)
    collect.add_argument("--output", type=Path, default=NOTICES_DIR)
    collect.add_argument("--cache", type=Path, help="download cache to reuse between runs")
    verify = sub.add_parser("verify", help="check the committed tree, offline")
    verify.add_argument("--notices", type=Path, default=NOTICES_DIR)
    verify.add_argument("--sbom", type=Path, help="also require notices for every component in it")
    image = sub.add_parser("check-image", help="check an image's notices, from inside it")
    image.add_argument("--sbom", type=Path, required=True, help="SPDX SBOM path, or - for stdin")
    image.add_argument("--doc-root", type=Path, default=IMAGE_DOC_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    commands = {"collect": run_collect, "verify": run_verify, "check-image": run_check_image}
    try:
        return commands[args.command](args)
    except (NoticeError, LicenseReviewError, OSError, KeyError, ValueError) as exc:
        print(f"[notices] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
