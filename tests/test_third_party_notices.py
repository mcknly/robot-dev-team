"""Robot Dev Team Project
File: tests/test_third_party_notices.py
Description: Tests for the license notices installed for glab and uv.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import zipfile
import zlib
from pathlib import Path
from typing import Any, Mapping

import pytest

from scripts import header_guard
from scripts import third_party_notices as notices_module
from scripts.license_review import sbom_entries
from scripts.third_party_notices import (
    COLLECTION_INPUTS,
    GO_BUILDINFO_MAGIC,
    IMAGE_BINARIES,
    MANIFEST_NAME,
    NOTICE_FALLBACKS,
    SOURCE_SUFFIXES,
    Fallback,
    Fetcher,
    NoticeError,
    Notices,
    binary_problems,
    check_image,
    coverage_problems,
    dockerfile_pins,
    fallback_bytes,
    go_module_hash,
    go_proxy_escape,
    is_notice_path,
    load_manifest,
    pinned_bytes,
    read_go_buildinfo,
    read_rust_build,
    safe_relative,
    verified_module,
    verify_tree,
    write_tree,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTICES = REPO_ROOT / "notices"
REFERENCE_SBOM = REPO_ROOT / "sbom" / "sbom.spdx.json"
DOCKERFILE = REPO_ROOT / "Dockerfile"
CI_BUILD_IMAGE = REPO_ROOT / "scripts" / "ci-build-image.sh"
RUSTC_COMMIT = "8bab26f4f68e0e26f0bb7960be334d5b520ea452"


def manifest(tree: str) -> dict[str, Any]:
    return load_manifest(NOTICES / tree)


def component_files(tree: str, crate_or_module: str) -> list[str]:
    """The files of the one linked version of a component, whatever that version is."""
    components = manifest(tree)["components"]
    matches = [key for key in components if key.rsplit("@", 1)[0] == crate_or_module]
    assert len(matches) == 1, (
        f"expected one linked version of {crate_or_module}, found {matches}. If it is no longer "
        "linked at all, drop it from this test."
    )
    base = f"third-party/{matches[0]}/"
    return [path.removeprefix(base) for path in components[matches[0]]["files"]]


# --- The committed tree: the merge-request early warning ----------------------------------------


@pytest.mark.parametrize("tree", ["glab", "uv"])
def test_committed_tree_is_exactly_what_its_manifest_records(tree: str) -> None:
    """Every file is the recorded upstream bytes, and nothing is present that is not recorded."""
    assert verify_tree(NOTICES / tree, manifest(tree), exhaustive=True) == []


def test_committed_tree_covers_every_compiled_in_component_of_the_reference_sbom() -> None:
    """The SBOM join the image check runs, against the reference SBOM in `sbom/`.

    `test_reference_sbom_records_the_runtime_interpreter` keeps that SBOM current with the image,
    so this fails in `validate` when a pin moves without a regeneration -- long before the build
    job, which only runs on `main`, would.
    """
    entries = sbom_entries(json.loads(REFERENCE_SBOM.read_text(encoding="utf-8")))
    compiled = [entry for entry in entries if entry.kind in ("cargo", "golang")]
    # A join over nothing proves nothing.
    assert {entry.location for entry in compiled} == set(IMAGE_BINARIES)
    manifests = {tree: manifest(tree) for tree in ("glab", "uv")}
    assert coverage_problems(entries, manifests, IMAGE_BINARIES) == []


def test_collection_pins_are_the_dockerfile_pins() -> None:
    assert dockerfile_pins(DOCKERFILE) == {
        "GLAB_VERSION": COLLECTION_INPUTS["glab"]["version"],
        "UV_VERSION": COLLECTION_INPUTS["uv"]["version"],
    }


@pytest.mark.parametrize(("tree", "inputs"), [("glab", ("glab", "go")), ("uv", ("rust", "uv"))])
def test_committed_tree_was_collected_from_the_current_pins(tree: str, inputs: tuple[str, ...]) -> None:
    """A pin bump that skipped `collect` leaves the manifest naming the old inputs."""
    assert manifest(tree)["inputs"] == {name: COLLECTION_INPUTS[name] for name in inputs}


def test_every_reviewed_fallback_is_installed_with_its_pinned_bytes() -> None:
    components = {**manifest("glab")["components"], **manifest("uv")["components"]}
    files = {**manifest("glab")["files"], **manifest("uv")["files"]}
    for key, fallbacks in NOTICE_FALLBACKS.items():
        assert key in components, f"fallback for {key}, which nothing links"
        for fallback in fallbacks:
            path = f"third-party/{key}/{fallback.path}"
            assert path in components[key]["files"]
            assert files[path] == {"sha256": fallback.installed_sha256, "origin": fallback.origin}


def test_bundled_c_library_notices_are_installed() -> None:
    """The -sys crates compile C libraries whose notices sit below the crate root.

    The SBOM names only the Rust wrapper's license, so a collector that copied root-level files
    alone would still pass the SBOM join. These are the files it would have missed.
    """
    assert {"LICENSE-APACHE", "LICENSE-MIT", "jemalloc/COPYING"} <= set(
        component_files("uv", "cargo/tikv-jemalloc-sys")
    )
    # zstd is BSD-3-Clause OR GPL-2.0-only; both texts ship, and the BSD branch is the one taken.
    assert {"zstd/LICENSE", "zstd/COPYING"} <= set(component_files("uv", "cargo/zstd-sys"))
    assert {
        "aws-lc/LICENSE",
        "aws-lc/third_party/fiat/LICENSE",
        "aws-lc/third_party/jitterentropy/jitterentropy-library/LICENSE",
        "aws-lc/third_party/s2n-bignum/s2n-bignum-imported/LICENSE",
        "aws-lc/third_party/s2n-bignum/s2n-bignum-imported/NOTICE",
    } <= set(component_files("uv", "cargo/aws-lc-sys"))
    assert {"LICENSE", "LICENSE-BoringSSL", "third_party/fiat/LICENSE"} <= set(
        component_files("uv", "cargo/ring")
    )


def test_works_d2_compiles_into_glab_have_their_own_license_text() -> None:
    """d2's NOTICE.txt files only link to the MIT licenses of works whose code glab carries.

    d2's own LICENSE.txt satisfies the SBOM join, so nothing mechanical asks for these. The
    holders are checked so a fallback pointed at the wrong repository fails here too.
    """
    files = set(component_files("glab", "golang/oss.terrastruct.com/d2"))
    key = next(k for k in manifest("glab")["components"] if k.startswith("golang/oss.terrastruct.com/d2@"))
    holders = {
        "lib/textmeasure/faiface-pixel/LICENSE": "Copyright (c) 2016 Michal",
        "d2renderers/d2svg/github-markdown-css/license": "Copyright (c) Sindre Sorhus",
        "d2layouts/d2dagrelayout/dagre/LICENSE": "Copyright (c) 2012-2014 Chris Pettitt",
        "d2layouts/d2dagrelayout/graphlib/LICENSE": "Copyright (c) 2012-2014 Chris Pettitt",
        "d2layouts/d2dagrelayout/lodash/LICENSE": "Copyright OpenJS Foundation",
    }
    assert set(holders) <= files
    for path, holder in holders.items():
        text = (NOTICES / "glab" / "third-party" / key / path).read_text(encoding="utf-8")
        assert holder in text and "Permission is hereby granted" in text, path


def test_graphlib_bsd_header_d2_bundles_is_installed_whole() -> None:
    """The bundled graphlib keeps a BSD-3-Clause header its repository's MIT LICENSE lacks.

    Its binary-redistribution condition wants the notice, conditions and disclaimer in the
    documentation, so all three are checked, and nothing past the comment is shipped.
    """
    key = next(k for k in manifest("glab")["components"] if k.startswith("golang/oss.terrastruct.com/d2@"))
    path = "d2layouts/d2dagrelayout/graphlib/LICENSE-index-js-header"
    assert path in component_files("glab", "golang/oss.terrastruct.com/d2")
    text = (NOTICES / "glab" / "third-party" / key / path).read_text(encoding="utf-8")
    flat = " ".join(line.lstrip("/* ").strip() for line in text.splitlines())
    assert "Copyright (c) 2014, Chris Pettitt All rights reserved." in flat
    for condition in (
        "1. Redistributions of source code must retain the above copyright notice",
        "2. Redistributions in binary form must reproduce the above copyright notice",
        "3. Neither the name of the copyright holder nor the names of its contributors",
        "THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS \"AS IS\"",
        "EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.",
    ):
        assert condition in flat, condition
    assert text.startswith("/**\n") and text.endswith(" */\n")
    assert "require(" not in text


def test_toolchain_standard_library_notices_are_installed() -> None:
    """Go's and Rust's standard libraries are linked in; the SBOM join never asks for Rust's."""
    go = COLLECTION_INPUTS["go"]["version"]
    assert {"LICENSE", "PATENTS", "src/vendor/golang.org/x/crypto/LICENSE"} <= set(
        component_files("glab", "golang/stdlib")
    )
    rust = set(component_files("uv", "rust/std"))
    assert {"COPYRIGHT", "LICENSE-APACHE", "LICENSE-MIT"} <= rust
    # compiler_builtins carries LLVM compiler-rt code; hashbrown is std's own HashMap.
    assert "library/compiler-builtins/LICENSE.txt" in rust
    assert any(re.fullmatch(r"library/vendor/hashbrown-[^/]+/LICENSE-MIT", path) for path in rust)
    assert f"golang/stdlib@{go}" in manifest("glab")["components"]


def test_freetype_credit_is_in_the_documentation() -> None:
    """The FTL requires a credit in the documentation, which copying its text alone is not."""
    credits = (NOTICES / "glab" / "CREDITS").read_text(encoding="utf-8")
    assert "This software is based in part on the work of the FreeType Team." in credits
    assert "The FreeType\n    Project (www.freetype.org)" in credits
    files = component_files("glab", "golang/github.com/golang/freetype")
    assert "licenses/ftl.txt" in files
    key = next(k for k in manifest("glab")["components"] if "golang/freetype@" in k)
    assert "CREDITS" in manifest("glab")["components"][key]["files"]
    assert f"third-party/{key}/licenses/ftl.txt" in credits


def test_glab_copyright_is_its_own_mit_license_at_the_pinned_tag() -> None:
    glab = manifest("glab")
    version = COLLECTION_INPUTS["glab"]["version"]
    text = (NOTICES / "glab" / "copyright").read_text(encoding="utf-8")
    assert "MIT License" in text and "Copyright" in text
    assert glab["files"]["copyright"]["origin"] == (
        f"https://proxy.golang.org/gitlab.com/gitlab-org/cli/@v/v{version}.zip#LICENSE"
    )
    assert glab["components"][f"deb/glab@{version}"]["files"] == ["copyright"]


def test_no_source_code_is_installed() -> None:
    """License text only. MPL source availability is a pointer, never a copy."""
    reviewed = {
        f"third-party/{key}/{fallback.path}" for key, entries in NOTICE_FALLBACKS.items()
        for fallback in entries
    }
    for tree in ("glab", "uv"):
        for path in manifest(tree)["files"]:
            if path in reviewed:
                continue
            suffix = Path(path).suffix.lower()
            assert suffix not in SOURCE_SUFFIXES, f"{tree}/{path} looks like source code"


def test_header_guard_leaves_the_upstream_notices_unaltered() -> None:
    """Many notices are LICENSE.md; the project header on one would alter the notice."""
    assert "notices/" in header_guard.DEFAULT_EXCLUDED_PREFIXES
    for path in NOTICES.rglob("*"):
        if path.is_file():
            assert header_guard.HEADER_TOKENS[0].encode() not in path.read_bytes(), path


def test_notices_are_checked_out_byte_for_byte() -> None:
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "notices/** -text" in attributes


def test_dockerfile_installs_both_trees_under_usr_share_doc() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY notices/glab/ /usr/share/doc/glab/" in text
    assert "COPY notices/uv/ /usr/share/doc/uv/" in text
    assert IMAGE_BINARIES == {
        "/usr/bin/glab": "glab",
        "/usr/local/bin/uv": "uv",
        "/usr/local/bin/uvx": "uv",
    }


# --- The image check in the build ---------------------------------------------------------------


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_build(tmp_path: Path, check_exit: int) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run ci-build-image.sh on the publishing path with a fake docker and stubbed helpers."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(CI_BUILD_IMAGE, scripts / "ci-build-image.sh")
    _executable(scripts / "ci-smoke-image.sh", "#!/bin/sh\nexit 0\n")
    _executable(scripts / "generate-sbom.sh", "#!/bin/sh\nprintf '{\"sbom\": 1}' > \"$2\"\n")
    _executable(scripts / "ci-inspect-pushed-digest.sh", "#!/bin/sh\necho sha256:" + "a" * 64 + "\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "docker",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info) [ "${2:-}" = "--format" ] && echo '["name=rootless"]'; exit 0 ;;
  buildx) [ "$2" = "imagetools" ] && exit 1; exit 0 ;;
  run) cat > "$FAKE_RUN_STDIN"; exit "$FAKE_RUN_EXIT" ;;
esac
exit 0
""",
    )
    log = tmp_path / "docker.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_RUN_STDIN": str(tmp_path / "run-stdin"),
        "FAKE_RUN_EXIT": str(check_exit),
        "CI_REGISTRY_IMAGE": "registry.example.test/team/robot-dev-team",
        "CI_COMMIT_SHA": "0123456789abcdef",
        "CI_DEFAULT_BRANCH": "main",
        "CI_COMMIT_BRANCH": "main",
        "CI_COMMIT_REF_PROTECTED": "true",
        "CI_REGISTRY": "registry.example.test",
        "CI_REGISTRY_USER": "ci",
        "CI_REGISTRY_PASSWORD": "secret",
    }
    result = subprocess.run(
        ["sh", "scripts/ci-build-image.sh"], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    return result, log.read_text(encoding="utf-8").splitlines()


@pytest.mark.skipif(Path("/var/run/docker.sock").exists(), reason="the script refuses a host socket")
def test_build_checks_the_installed_notices_as_the_app_user_before_pushing(tmp_path: Path) -> None:
    result, calls = _run_build(tmp_path, check_exit=0)
    assert result.returncode == 0, result.stderr
    runs = [index for index, call in enumerate(calls) if call.startswith("run ")]
    pushes = [index for index, call in enumerate(calls) if call.startswith("push ")]
    assert len(runs) == 1 and len(pushes) == 1 and runs[0] < pushes[0]
    run = calls[runs[0]].split()
    assert run[run.index("--user") + 1] == "appuser"
    assert "--network" in run and run[run.index("--network") + 1] == "none"
    assert run[-4:] == ["scripts.third_party_notices", "check-image", "--sbom", "-"]
    # The generated SBOM, not some other document, is what the check reads.
    assert (tmp_path / "run-stdin").read_text(encoding="utf-8") == '{"sbom": 1}'


@pytest.mark.skipif(Path("/var/run/docker.sock").exists(), reason="the script refuses a host socket")
def test_a_failed_notice_check_stops_the_push(tmp_path: Path) -> None:
    result, calls = _run_build(tmp_path, check_exit=1)
    assert result.returncode != 0
    assert any(call.startswith("run ") for call in calls)
    assert not any(call.startswith(("login ", "push ")) for call in calls)


# --- Reading binaries ---------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def go_binary(go_version: str, main: tuple[str, str], lines: list[str], *, flags: int = 2) -> bytes:
    modinfo = f"path\t{main[0]}/cmd\nmod\t{main[0]}\t{main[1]}\t\n" + "".join(
        line + "\n" for line in lines
    )
    body = b"\x30" * 16 + modinfo.encode() + b"\x31" * 16
    header = GO_BUILDINFO_MAGIC + bytes([8, flags]) + b"\0" * 16
    version = go_version.encode()
    return b"\0" * 64 + header + _varint(len(version)) + version + _varint(len(body)) + body


def elf_binary(sections: Mapping[str, bytes], extra: bytes = b"") -> bytes:
    """A minimal little-endian ELF64 file: header, section contents, names, section headers."""
    names = [".shstrtab", *sections]
    table = b"\0" + b"".join(name.encode() + b"\0" for name in names)
    body = bytearray(64)
    placed: list[tuple[int, int, int]] = []
    for name, data in sections.items():
        placed.append((table.index(name.encode() + b"\0"), len(body), len(data)))
        body += data
    table_offset = len(body)
    body += table + extra
    section_offset = len(body)
    headers = [(0, 0, 0), (table.index(b".shstrtab\0"), table_offset, len(table)), *placed]
    for name_index, offset, size in headers:
        body += struct.pack("<IIQQQQIIQQ", name_index, 1, 0, 0, offset, size, 0, 0, 1, 0)
    body[0:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<Q", body, 0x28, section_offset)
    struct.pack_into("<HHH", body, 0x3A, 64, len(headers), 1)
    return bytes(body)


def rust_binary(packages: list[dict[str, Any]], commits: tuple[str, ...] = (RUSTC_COMMIT,)) -> bytes:
    audit = zlib.compress(json.dumps({"packages": packages}).encode())
    paths = b"".join(f"/rustc/{commit}/library/core/src/lib.rs\0".encode() for commit in commits)
    return elf_binary({".text": b"\x90" * 8, ".dep-v0": audit}, extra=paths)


UV_PACKAGES = [
    {"name": "uv", "version": "0.12.1", "source": "local", "root": True},
    {"name": "serde", "version": "1.0.0", "source": "crates.io"},
    {"name": "cc", "version": "1.2.0", "source": "crates.io", "kind": "build"},
]


def test_go_buildinfo_yields_toolchain_main_module_and_linked_modules() -> None:
    binary = go_binary(
        "go1.26.5",
        ("gitlab.com/gitlab-org/cli", "v1.111.0"),
        ["dep\tb.example/two\tv2.0.0\t", "dep\ta.example/one\tv1.0.0\t", "build\tCGO_ENABLED=0"],
    )
    build = read_go_buildinfo(binary)
    assert build.go_version == "go1.26.5"
    assert (build.main_module, build.main_version) == ("gitlab.com/gitlab-org/cli", "v1.111.0")
    assert build.deps == (("a.example/one", "v1.0.0"), ("b.example/two", "v2.0.0"))


def test_go_module_replacement_stands_in_for_the_module_it_replaces() -> None:
    binary = go_binary(
        "go1.26.5", ("m.example/main", "v1.0.0"),
        ["dep\ta.example/one\tv1.0.0\t", "=>\tfork.example/one\tv1.0.1\th1:x"],
    )
    assert read_go_buildinfo(binary).deps == (("fork.example/one", "v1.0.1"),)


def test_go_directory_replacement_fails_rather_than_dropping_the_module() -> None:
    binary = go_binary(
        "go1.26.5", ("m.example/main", "v1.0.0"),
        ["dep\ta.example/one\tv1.0.0\t", "=>\t../one\t\t"],
    )
    with pytest.raises(NoticeError, match="directory replacement"):
        read_go_buildinfo(binary)


def test_pre_118_go_buildinfo_is_refused() -> None:
    binary = go_binary("go1.17", ("m.example/main", "v1.0.0"), [], flags=0)
    with pytest.raises(NoticeError, match="predates Go 1.18"):
        read_go_buildinfo(binary)


def test_rust_build_skips_build_only_crates_and_reads_the_rustc_commit() -> None:
    build = read_rust_build(rust_binary(UV_PACKAGES))
    assert build.rustc_commit == RUSTC_COMMIT
    assert build.root == ("uv", "0.12.1")
    assert build.packages == (("serde", "1.0.0", "crates.io"), ("uv", "0.12.1", "local"))


def test_rust_build_needs_exactly_one_rustc_commit() -> None:
    with pytest.raises(NoticeError, match="one rustc commit"):
        read_rust_build(rust_binary(UV_PACKAGES, commits=(RUSTC_COMMIT, "f" * 40)))


def test_a_non_elf_file_has_no_sections() -> None:
    with pytest.raises(NoticeError, match="ELF64"):
        read_rust_build(b"#!/bin/sh\n")


# --- Archives and integrity ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("LICENSE", True),
        ("LICENSE-MIT", True),
        ("License-Apache", True),
        ("COPYING.LESSER", True),
        ("NOTICE.md", True),
        ("PATENTS", True),
        ("AUTHORS", True),
        ("UNLICENSE", True),
        ("jemalloc/COPYING", True),
        ("aws-lc/third_party/fiat/LICENSE", True),
        ("licenses/ftl.txt", True),
        ("LICENSES/MIT.txt", True),
        ("MPL-2.0.txt", True),
        ("LGPL-3.0-or-later.txt", True),
        # A licenses/ directory below the root is data: the spdx crate's license database.
        ("src/text/licenses/Unlicense", False),
        ("src/text/licenses/MIT", False),
        ("docs/MPL-2.0.txt", False),
        ("testdata/COPYING", False),
        ("license.go", False),
        ("src/license.rs", False),
        ("internal/copyright/copyright_test.go", False),
        ("noticeboard.txt", False),
        ("UNICODE.md", False),
        ("README.md", False),
    ],
)
def test_notice_file_selection(path: str, expected: bool) -> None:
    assert is_notice_path(path) is expected


@pytest.mark.parametrize("name", ["m@v1/../etc/passwd", "m@v1//x", "m@v1/"])
def test_unsafe_archive_member_names_fail(name: str) -> None:
    with pytest.raises(NoticeError, match="unsafe"):
        safe_relative(name, "m@v1/")


def test_go_proxy_escapes_upper_case() -> None:
    assert go_proxy_escape("github.com/BurntSushi/toml") == "github.com/!burnt!sushi/toml"


class FakeFetcher(Fetcher):
    def __init__(self, cache: Path, responses: Mapping[str, bytes]) -> None:
        super().__init__(cache)
        self.responses = responses

    def ensure(self, url: str) -> None:
        if url not in self.responses:
            raise NoticeError(f"unexpected fetch {url}")
        self.path(url).write_bytes(self.responses[url])


def module_zip(files: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_a_module_that_does_not_match_go_sum_is_refused(tmp_path: Path) -> None:
    good = module_zip({"a.example/one@v1.0.0/LICENSE": b"MIT"})
    expected = go_module_hash(zipfile.ZipFile(io.BytesIO(good)))
    url = "https://proxy.golang.org/a.example/one/@v/v1.0.0.zip"
    tampered = module_zip({"a.example/one@v1.0.0/LICENSE": b"not the file go.sum hashed"})

    assert verified_module(FakeFetcher(tmp_path / "a", {url: good}), "a.example/one", "v1.0.0", expected)
    with pytest.raises(NoticeError, match="hashes to"):
        verified_module(FakeFetcher(tmp_path / "b", {url: tampered}), "a.example/one", "v1.0.0", expected)


def test_a_download_that_does_not_match_its_pin_is_refused(tmp_path: Path) -> None:
    fetcher = FakeFetcher(tmp_path, {"https://example.test/x": b"bytes"})
    with pytest.raises(NoticeError, match="not the pinned"):
        pinned_bytes(fetcher, "https://example.test/x", "0" * 64)


HEADER_SOURCE = b"/**\n * Copyright (c) 2014, A Holder\n */\n\nvar lib = require('./lib');\n"
HEADER = b"/**\n * Copyright (c) 2014, A Holder\n */\n"


def header_fallback(**overrides: Any) -> Fallback:
    fields: dict[str, Any] = {
        "path": "LICENSE-index-js-header",
        "url": "https://example.test/index.js",
        "sha256": hashlib.sha256(HEADER_SOURCE).hexdigest(),
        "reason": "test",
        "lines": (1, 3),
        "header_sha256": hashlib.sha256(HEADER).hexdigest(),
        "embedded_in": "bundle.js",
    }
    return Fallback(**{**fields, **overrides})


def test_a_header_fallback_installs_only_the_pinned_lines(tmp_path: Path) -> None:
    fetcher = FakeFetcher(tmp_path, {"https://example.test/index.js": HEADER_SOURCE})
    fallback = header_fallback()
    bundle = {"bundle.js": b"code();" + HEADER + b"more();"}

    assert fallback_bytes(fetcher, fallback, bundle.__getitem__) == HEADER
    assert fallback.origin == "https://example.test/index.js#L1-L3"
    assert fallback.installed_sha256 == fallback.header_sha256


@pytest.mark.parametrize(
    ("overrides", "bundle", "message"),
    [
        ({"lines": (1, 4)}, {"bundle.js": HEADER}, "not the pinned header"),
        ({}, {"bundle.js": b"a bundle that dropped the header"}, "does not carry the header"),
        ({"embedded_in": ""}, {"bundle.js": HEADER}, "needs the file that embeds it"),
        ({"embedded_in": "gone.js"}, {"bundle.js": HEADER}, "has no gone.js"),
    ],
)
def test_a_header_fallback_fails_unless_it_is_the_pinned_text_that_ships(
    tmp_path: Path, overrides: dict[str, Any], bundle: dict[str, bytes], message: str
) -> None:
    fetcher = FakeFetcher(tmp_path, {"https://example.test/index.js": HEADER_SOURCE})
    with pytest.raises(NoticeError, match=message):
        fallback_bytes(fetcher, header_fallback(**overrides), bundle.__getitem__)


def test_a_header_fallback_needs_the_components_archive(tmp_path: Path) -> None:
    fetcher = FakeFetcher(tmp_path, {"https://example.test/index.js": HEADER_SOURCE})
    with pytest.raises(NoticeError, match="needs the file that embeds it"):
        fallback_bytes(fetcher, header_fallback(), None)


def test_a_component_without_a_notice_needs_a_reviewed_fallback(tmp_path: Path) -> None:
    notices = Notices(Fetcher(tmp_path))
    with pytest.raises(NoticeError, match="NOTICE_FALLBACKS"):
        notices.add_component("cargo/bare@1.0.0", "https://example.test", "sha256:0", [])


# --- The in-image check -------------------------------------------------------------------------


def spdx_package(purl_type: str, name: str, version: str, location: str) -> dict[str, Any]:
    return {
        "name": name,
        "versionInfo": version,
        "licenseDeclared": "NOASSERTION",
        "sourceInfo": f"acquired package info from binary: {location}",
        "externalRefs": [
            {"referenceType": "purl", "referenceLocator": f"pkg:{purl_type}/{name}@{version}"}
        ],
    }


@pytest.fixture
def image(tmp_path: Path) -> dict[str, Any]:
    """A doc root with a glab and a uv tree, two binaries they describe, and their SBOM."""
    glab_inputs = {"glab": {"version": "1.111.0"}, "go": {"version": "go1.26.5"}}
    uv_inputs = {"uv": {"version": "0.12.1"}, "rust": {"commit": RUSTC_COMMIT}}
    doc = tmp_path / "doc"
    doc.mkdir()
    fetcher = Fetcher(tmp_path / "cache")

    glab = Notices(fetcher)
    glab.add_component("deb/glab@1.111.0", "x", "h1:x", [glab.add_file("copyright", b"MIT", "x")])
    path = glab.add_file("third-party/golang/a.example/one@v1.0.0/LICENSE", b"BSD", "x")
    glab.add_component("golang/a.example/one@v1.0.0", "x", "h1:x", [path])
    write_tree(doc / "glab", "glab", glab, glab_inputs)

    uv = Notices(fetcher)
    path = uv.add_file("third-party/cargo/serde@1.0.0/LICENSE-MIT", b"MIT", "x")
    uv.add_component("cargo/serde@1.0.0", "x", "sha256:x", [path])
    path = uv.add_file("third-party/cargo/uv@0.12.1/LICENSE-MIT", b"MIT", "x")
    uv.add_component("cargo/uv@0.12.1", "x", "sha256:x", [path])
    write_tree(doc / "uv", "uv", uv, uv_inputs)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    glab_path, uv_path = bin_dir / "glab", bin_dir / "uv"
    glab_path.write_bytes(
        go_binary("go1.26.5", ("gitlab.com/gitlab-org/cli", "v1.111.0"), ["dep\ta.example/one\tv1.0.0\t"])
    )
    uv_path.write_bytes(rust_binary(UV_PACKAGES))
    sbom = {
        "packages": [
            spdx_package("golang", "a.example/one", "v1.0.0", str(glab_path)),
            spdx_package("cargo", "serde", "1.0.0", str(uv_path)),
            spdx_package("cargo", "uv", "0.12.1", str(uv_path)),
            spdx_package("deb", "bash", "5.2", "/var/lib/dpkg/status"),
        ]
    }
    binaries = {str(glab_path): "glab", str(uv_path): "uv"}
    return {"doc": doc, "sbom": sbom, "binaries": binaries, "glab": glab_path, "uv": uv_path}


def test_image_check_passes_when_every_linked_component_has_its_notice(image: dict[str, Any]) -> None:
    assert check_image(image["sbom"], image["doc"], image["binaries"]) == []


def test_image_check_fails_on_an_sbom_component_without_a_notice(image: dict[str, Any]) -> None:
    image["sbom"]["packages"].append(spdx_package("cargo", "tokio", "1.0.0", str(image["uv"])))
    assert check_image(image["sbom"], image["doc"], image["binaries"]) == [
        f"cargo/tokio@1.0.0 in {image['uv']}: no notice in the uv tree"
    ]


def test_image_check_fails_on_a_compiled_component_of_an_unknown_binary(image: dict[str, Any]) -> None:
    image["sbom"]["packages"].append(spdx_package("golang", "b.example/two", "v2", "/usr/bin/other"))
    problems = check_image(image["sbom"], image["doc"], image["binaries"])
    assert problems == ["golang/b.example/two@v2 in /usr/bin/other: no notices tree covers that binary"]


def test_image_check_fails_on_an_altered_or_missing_notice(image: dict[str, Any]) -> None:
    (image["doc"] / "uv" / "third-party/cargo/serde@1.0.0/LICENSE-MIT").write_bytes(b"edited")
    (image["doc"] / "glab" / "copyright").unlink()
    problems = check_image(image["sbom"], image["doc"], image["binaries"])
    assert any("serde@1.0.0/LICENSE-MIT: SHA-256 differs" in problem for problem in problems)
    assert any(problem.endswith("copyright: unreadable (No such file or directory)") for problem in problems)


def test_image_check_reads_the_binaries_not_only_the_sbom(image: dict[str, Any]) -> None:
    """A module Syft missed, or a binary built by another toolchain, still fails."""
    image["glab"].write_bytes(
        go_binary(
            "go1.26.6", ("gitlab.com/gitlab-org/cli", "v1.111.0"),
            ["dep\ta.example/one\tv1.0.0\t", "dep\tc.example/three\tv3.0.0\t"],
        )
    )
    problems = check_image(image["sbom"], image["doc"], image["binaries"])
    assert problems == [
        "the glab binary's toolchain is go1.26.6; the notices cover go1.26.5",
        "golang/c.example/three@v3.0.0 is linked into glab but has no notices",
    ]


def test_rust_toolchain_mismatch_is_reported() -> None:
    manifest_document = {
        "inputs": {"uv": {"version": "0.12.1"}, "rust": {"commit": "f" * 40}},
        "components": {"cargo/serde@1.0.0": {}, "cargo/uv@0.12.1": {}},
    }
    assert binary_problems("uv", rust_binary(UV_PACKAGES), manifest_document) == [
        f"the uv binary's toolchain is {RUSTC_COMMIT}; the notices cover {'f' * 40}"
    ]


def test_exhaustive_verification_rejects_an_unrecorded_file(image: dict[str, Any]) -> None:
    tree = image["doc"] / "uv"
    (tree / "third-party" / "stray").write_bytes(b"x")
    assert verify_tree(tree, load_manifest(tree), exhaustive=False) == []
    assert verify_tree(tree, load_manifest(tree), exhaustive=True) == [
        f"{tree / 'third-party/stray'}: not recorded in {MANIFEST_NAME}"
    ]


def test_check_image_command_reads_the_sbom_from_stdin(
    image: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(notices_module, "IMAGE_BINARIES", image["binaries"])
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(image["sbom"]).encode())))
    assert notices_module.main(["check-image", "--sbom", "-", "--doc-root", str(image["doc"])]) == 0
    assert "has notices in the image" in capsys.readouterr().out
