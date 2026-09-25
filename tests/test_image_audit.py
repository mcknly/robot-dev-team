"""Robot Dev Team Project
File: tests/test_image_audit.py
Description: Regression tests for the release image layer extraction and hostname scan (#51).
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import gzip
import io
import json
import stat
import tarfile
from pathlib import Path
from typing import Any

import pytest

from scripts import image_audit as ia

HOST = "gitlab.internal.test"
REGISTRY = "registry.internal.test"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
DIGEST = f"sha256:{'e' * 64}"


def layer_bytes(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for info, content in members:
            if content is not None:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            else:
                archive.addfile(info)
    return buffer.getvalue()


def layer(*members: tuple[tarfile.TarInfo, bytes | None]) -> io.BytesIO:
    return io.BytesIO(layer_bytes(*members))


def regular(name: str, content: bytes) -> tuple[tarfile.TarInfo, bytes]:
    return tarfile.TarInfo(name), content


def special(name: str, kind: bytes, target: str = "") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, None


# --- Extraction -------------------------------------------------------------------------------


def test_layer_digests_are_read_in_order() -> None:
    manifest = {"layers": [{"mediaType": OCI_LAYER, "digest": "sha256:a"},
                           {"mediaType": OCI_LAYER, "digest": "sha256:b"}]}

    assert ia.layer_digests(manifest) == ["sha256:a", "sha256:b"]


@pytest.mark.parametrize(
    "manifest",
    [
        # A multi-platform index has no layers of its own; auditing whichever platform it
        # resolved to would audit something the report does not name.
        {"manifests": [{"digest": "sha256:a"}]},
        {"layers": []},
        {"layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+zstd", "digest": "x"}]},
    ],
)
def test_anything_but_a_single_platform_gzip_manifest_is_refused(manifest: dict[str, Any]) -> None:
    with pytest.raises(ia.ImageAuditError):
        ia.layer_digests(manifest)


def test_only_content_is_written_and_every_skipped_member_is_recorded(tmp_path: Path) -> None:
    record = ia.LayerRecord(0, "sha256:a")
    stream = layer(
        regular("work/app/main.py", b"print('hi')\n"),
        regular("etc/.wh.removed.conf", b""),
        special("usr/bin/sh", tarfile.SYMTYPE, "/bin/dash"),
        special("usr/bin/hard", tarfile.LNKTYPE, "work/app/main.py"),
        special("dev/null", tarfile.CHRTYPE),
    )

    ia.extract_layer(stream, tmp_path, record)

    assert (tmp_path / "work/app/main.py").read_bytes() == b"print('hi')\n"
    assert not (tmp_path / "usr/bin/sh").exists()
    assert not (tmp_path / "dev/null").exists()
    assert record.files == 2
    # Names and targets of what was not written, so the hostname scan still reads them.
    assert record.links == [["usr/bin/sh", "/bin/dash"], ["usr/bin/hard", "work/app/main.py"]]
    assert record.special == ["dev/null"]
    # A whiteout is how a layer deletes a lower layer's file, so it is recorded, not ignored.
    assert record.whiteouts == ["etc/.wh.removed.conf"]


def test_a_path_escaping_the_destination_fails_closed(tmp_path: Path) -> None:
    destination = tmp_path / "layer"
    destination.mkdir()
    stream = layer(regular("../escaped.txt", b"x"))

    with pytest.raises(tarfile.TarError):
        ia.extract_layer(stream, destination, ia.LayerRecord(0, "sha256:a"))
    assert not (tmp_path / "escaped.txt").exists()


def test_images_must_be_named_by_digest(tmp_path: Path) -> None:
    with pytest.raises(ia.ImageAuditError, match="named by digest"):
        ia.extract_image(tmp_path / "crane", "registry.example/team/image:latest", tmp_path / "out")


def fake_crane(tmp_path: Path, *, blob: bytes | None, blob_error: str = "") -> Path:
    """A stand-in crane: `manifest`, `config`, and `blob` answered from files on disk."""
    data = tmp_path / "crane-data"
    data.mkdir()
    (data / "manifest").write_text(json.dumps({"layers": [{"mediaType": OCI_LAYER, "digest": "sha256:l0"}]}))
    (data / "config").write_text(json.dumps({"config": {"Env": ["PATH=/usr/bin"]}}))
    if blob is not None:
        (data / "blob").write_bytes(blob)
    script = tmp_path / "crane"
    script.write_text(
        "#!/bin/sh\n"
        f'case "$1" in\n'
        f'  manifest) cat "{data}/manifest" ;;\n'
        f'  config) cat "{data}/config" ;;\n'
        f'  blob) if [ -f "{data}/blob" ]; then cat "{data}/blob"; '
        f'else echo "{blob_error}" >&2; exit 1; fi ;;\n'
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_extraction_writes_the_layers_manifest_and_config(tmp_path: Path) -> None:
    crane = fake_crane(tmp_path, blob=layer_bytes(regular("work/app.py", b"x = 1\n")))
    dest = tmp_path / "out"

    records = ia.extract_image(crane, f"registry.example/team/image@{DIGEST}", dest)

    assert [r.files for r in records] == [1]
    assert (dest / "layer-00/work/app.py").read_text() == "x = 1\n"
    for name in ia.METADATA_FILES:
        assert (dest / name).is_file(), name
    # The manifest and config reach every puller, so they sit in the scan root.
    scan = ia.scan_hosts(dest, [HOST])
    ia.check_host_scan(dest, scan)
    assert scan.files == 1 + len(ia.METADATA_FILES)


def test_a_failed_download_reports_crane_s_error_not_the_empty_stream(tmp_path: Path) -> None:
    crane = fake_crane(tmp_path, blob=None, blob_error="UNAUTHORIZED: authentication required")

    with pytest.raises(ia.ImageAuditError, match="UNAUTHORIZED: authentication required"):
        ia.extract_image(crane, f"registry.example/team/image@{DIGEST}", tmp_path / "out")


def test_extraction_refuses_a_directory_that_already_holds_files(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "stale.txt").write_text("from an earlier run\n")

    with pytest.raises(ia.ImageAuditError, match="not empty"):
        ia.extract_image(tmp_path / "crane", f"registry.example/team/image@{DIGEST}", dest)


# --- Hostname scan ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "host"),
    [
        (HOST, HOST),
        (f"{REGISTRY}:5050", REGISTRY),
        ("GitLab.Internal.Test", HOST),
        ("[::1]:5050", "::1"),
        ("[fe80::1]", "fe80::1"),
        ("fe80::1", "fe80::1"),
    ],
)
def test_hosts_are_normalized_to_bare_lowercase_names(value: str, host: str) -> None:
    assert ia.normalize_host(value) == host


@pytest.mark.parametrize(
    "value",
    [
        f"https://{HOST}", f"{HOST}/team", "", " ",
        # Typos that would otherwise pass through as a literal no bare occurrence contains.
        f"{HOST}:", f"{HOST}:port", f"[{HOST}", f"{HOST}]", "[::1]:", "[::1]x", ":5050", "fe80::1:",
    ],
)
def test_a_url_an_empty_host_or_a_malformed_port_is_refused(value: str) -> None:
    """Searching for a literal URL would miss every bare occurrence of the host."""
    with pytest.raises(ia.ImageAuditError):
        ia.normalize_host(value)


def test_hosts_are_found_in_content_file_names_and_directory_names(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "work/clean.txt").write_text("nothing to see\n")
    (tmp_path / "work/wrapper").write_text("export GLAB_HOST=GitLab.Internal.Test\n")
    (tmp_path / "work/binary").write_bytes(b"\x00\xff" + REGISTRY.upper().encode() + b"\x00")
    (tmp_path / f"root/.cache/{HOST}").mkdir(parents=True)  # empty: only its name can leak
    (tmp_path / "work/link").symlink_to(tmp_path / "work/wrapper")

    result = ia.scan_hosts(tmp_path, [HOST, f"{REGISTRY}:5050"])

    # The symlink is not followed or counted: its target is scanned as itself.
    assert result.files == 3
    assert sorted(result.offenders) == [
        ("root/.cache/<host#1>", 0),
        ("work/binary", 1),
        ("work/wrapper", 0),
    ]


def test_a_registry_on_the_server_s_host_is_one_host_not_two(tmp_path: Path) -> None:
    (tmp_path / "leak.txt").write_text(f"{HOST}:5050/team/image\n")

    result = ia.scan_hosts(tmp_path, [HOST, f"{HOST}:5050"])

    assert result.offenders == [("leak.txt", 0)]


def test_link_names_and_targets_are_scanned_through_the_layer_record(tmp_path: Path) -> None:
    record = ia.LayerRecord(0, "sha256:a")
    ia.extract_layer(
        layer(special("etc/registry-link", tarfile.SYMTYPE, f"/opt/{REGISTRY}/config")),
        tmp_path / "layer-00", record,
    )
    (tmp_path / ia.LAYERS_FILE).write_text(json.dumps([record.__dict__]))

    result = ia.scan_hosts(tmp_path, [REGISTRY])

    assert result.offenders == [(ia.LAYERS_FILE, 0)]


def test_a_file_named_after_the_host_is_reported_without_printing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / f"{REGISTRY}.txt").write_text("")

    code = ia.main(["hosts", "--root", str(tmp_path), "--host", REGISTRY, "--no-extraction"])

    captured = capsys.readouterr()
    assert code == 1
    assert "HOST #1: <host#1>.txt" in captured.err
    assert REGISTRY not in captured.out + captured.err


def test_a_content_hit_is_reported_without_printing_the_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "leak.txt").write_text(f"https://{HOST}/api\n")

    code = ia.main(["hosts", "--root", str(tmp_path), "--host", HOST, "--no-extraction"])

    captured = capsys.readouterr()
    assert code == 1
    assert "HOST #1: leak.txt" in captured.err
    assert HOST not in captured.out + captured.err


@pytest.mark.parametrize("mode", [[], ["--no-extraction"]])
def test_a_scan_of_nothing_fails_rather_than_passing_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: list[str]
) -> None:
    assert ia.main(["hosts", "--root", str(tmp_path), "--host", HOST, *mode]) == 1
    assert "nothing was scanned" in capsys.readouterr().err


def extracted_tree(root: Path, *, recorded: int = 1, written: int = 1) -> Path:
    """What `extract` leaves: every metadata file, and a layer record for the files it wrote."""
    (root / ia.MANIFEST_FILE).write_text("{}\n")
    (root / ia.CONFIG_FILE).write_text("{}\n")
    (root / ia.LAYERS_FILE).write_text(json.dumps([{"index": 0, "files": recorded}]))
    (root / "layer-00").mkdir()
    for number in range(written):
        (root / f"layer-00/file-{number}.txt").write_text("nothing\n")
    return root


def test_a_clean_extracted_tree_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    extracted_tree(tmp_path)

    assert ia.main(["hosts", "--root", str(tmp_path), "--host", HOST, "--host", REGISTRY]) == 0
    assert "scanned 4 files: 0 findings" in capsys.readouterr().out


def test_a_scan_that_misses_extracted_files_fails(tmp_path: Path) -> None:
    """A wrong --root or a half-deleted tree must not read as clean."""
    extracted_tree(tmp_path, recorded=5, written=1)

    result = ia.scan_hosts(tmp_path, [HOST])

    with pytest.raises(ia.ImageAuditError, match="extraction recorded 8"):
        ia.check_host_scan(tmp_path, result)


@pytest.mark.parametrize("missing", ia.METADATA_FILES)
def test_an_extraction_missing_any_metadata_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    """A missing file must neither lower the expected count nor select the lenient mode."""
    (extracted_tree(tmp_path) / missing).unlink()

    assert ia.main(["hosts", "--root", str(tmp_path), "--host", HOST]) == 1
    assert f"missing {missing}" in capsys.readouterr().err


def test_an_arbitrary_tree_is_not_an_extraction_unless_said_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "one.txt").write_text("nothing\n")

    assert ia.main(["hosts", "--root", str(tmp_path), "--host", HOST]) == 1
    assert "not a complete extraction" in capsys.readouterr().err
    assert ia.main(["hosts", "--root", str(tmp_path), "--host", HOST, "--no-extraction"]) == 0


@pytest.mark.parametrize(
    "record",
    ["not json", "{}", "[]", '[{"index": 0}]', '[{"files": -1}]', '[{"files": "3"}]', '[{"files": true}]', "[1]"],
)
def test_a_malformed_layer_record_is_a_clean_error_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], record: str
) -> None:
    (extracted_tree(tmp_path) / ia.LAYERS_FILE).write_text(record)

    assert ia.main(["hosts", "--root", str(tmp_path), "--host", HOST]) == 1
    assert "[image] ERROR:" in capsys.readouterr().err


def test_a_non_utf8_member_name_is_scanned_not_a_crash(tmp_path: Path) -> None:
    record = ia.LayerRecord(0, "sha256:a")
    info = tarfile.TarInfo("placeholder")
    info.size = 1
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.GNU_FORMAT,
                      encoding="utf-8", errors="surrogateescape") as archive:
        info.name = "caf\udce9-" + HOST
        archive.addfile(info, io.BytesIO(b"x"))
    buffer.seek(0)
    ia.extract_layer(buffer, tmp_path, record)

    result = ia.scan_hosts(tmp_path, [HOST])

    assert result.files == 1
    assert [index for _, index in result.offenders] == [0]


# --- Secrets baseline -------------------------------------------------------------------------

EXCLUDE = {"path": "detect_secrets.filters.regex.should_exclude_file", "pattern": [ia.SECRETS_EXCLUDE]}
PROFILE = {
    "version": "1.5.0",
    "plugins_used": [{"name": "HexHighEntropyString", "limit": 3.0}],
    "filters_used": [{"path": "detect_secrets.filters.heuristic.is_likely_id_string"}, EXCLUDE],
}


def scan(results: dict[str, list[tuple[str, str, int]]], **profile: Any) -> dict[str, Any]:
    return {
        **PROFILE,
        **profile,
        "results": {
            path: [{"type": kind, "hashed_secret": hashed, "line_number": line}
                   for kind, hashed, line in hits]
            for path, hits in results.items()
        },
    }


AUDITED = scan({
    "layer-14/opt/venv/lib/python3.14/site-packages/pydantic/networks.py": [("Basic Auth Credentials", "aa", 137)],
    "layer-17/work/scripts/release_tools.py": [("Hex High Entropy String", "bb", 81)],
    "layer-19/work/scripts/release_tools.py": [("Hex High Entropy String", "bb", 81)],
})


def baseline(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "baseline.json"
    ia.write_baseline(AUDITED, digest="sha256:a", output=path)
    document: dict[str, Any] = json.loads(path.read_text())
    return document


def test_keys_drop_layer_numbers_and_lines_but_keep_the_string() -> None:
    assert ia.secret_keys(AUDITED) == {
        ("opt/venv/lib/python3.14/site-packages/pydantic/networks.py", "Basic Auth Credentials", "aa"),
        ("work/scripts/release_tools.py", "Hex High Entropy String", "bb"),
    }


def test_a_rebuild_with_shifted_layers_and_lines_is_already_classified(tmp_path: Path) -> None:
    rebuilt = scan({
        "layer-15/opt/venv/lib/python3.14/site-packages/pydantic/networks.py": [("Basic Auth Credentials", "aa", 140)],
        "layer-18/work/scripts/release_tools.py": [("Hex High Entropy String", "bb", 90)],
    })

    assert ia.compare_secrets(rebuilt, baseline(tmp_path)) == ia.SecretsComparison([], [])


@pytest.mark.parametrize(
    "extra",
    [
        # A new string at an already-classified path is a new finding, not a known one.
        ("layer-17/work/scripts/release_tools.py", ("Hex High Entropy String", "cc", 81)),
        # A known string at a new path is too: the classification was about that file.
        ("layer-17/work/app/config.py", ("Hex High Entropy String", "bb", 3)),
    ],
)
def test_anything_the_audit_did_not_classify_fails_the_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: tuple[str, tuple[str, str, int]]
) -> None:
    baseline_path = tmp_path / "baseline.json"
    ia.write_baseline(AUDITED, digest="sha256:a", output=baseline_path)
    current = json.loads(json.dumps(AUDITED))
    path, (kind, hashed, line) = extra
    current["results"].setdefault(path, []).append(
        {"type": kind, "hashed_secret": hashed, "line_number": line}
    )
    scan_path = tmp_path / "scan.json"
    scan_path.write_text(json.dumps(current))

    code = ia.main(["compare-secrets", "--scan", str(scan_path), "--baseline", str(baseline_path)])

    assert code == 1
    assert f"UNCLASSIFIED: {kind}: {path.split('/', 1)[1]}" in capsys.readouterr().err


def test_a_partial_scan_fails_on_every_baseline_hit_it_lost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One known hit is not coverage: a scan that skipped most of the image must not pass."""
    baseline_path = tmp_path / "baseline.json"
    ia.write_baseline(AUDITED, digest="sha256:a", output=baseline_path)
    partial = scan({"layer-14/opt/venv/lib/python3.14/site-packages/pydantic/networks.py":
                    [("Basic Auth Credentials", "aa", 137)]})
    scan_path = tmp_path / "scan.json"
    scan_path.write_text(json.dumps(partial))

    code = ia.main(["compare-secrets", "--scan", str(scan_path), "--baseline", str(baseline_path)])

    captured = capsys.readouterr()
    assert code == 1
    assert "LOST: Hex High Entropy String: work/scripts/release_tools.py" in captured.err
    assert "UNCLASSIFIED" not in captured.err
    assert "1 baseline hits are missing from the scan" in captured.out


def test_a_one_hit_scan_fails_against_the_committed_baseline() -> None:
    committed = json.loads((Path(__file__).resolve().parents[1] / "security/image-secrets-baseline.json")
                           .read_text(encoding="utf-8"))
    kept = committed["entries"][0]
    one_hit = {
        **committed["scanner"],
        "results": {f"layer-03/{kept['path']}": [{"type": kept["type"],
                                                   "hashed_secret": kept["hashed_secret"],
                                                   "line_number": 1}]},
    }

    result = ia.compare_secrets(one_hit, committed)

    assert result.unclassified == []
    assert len(result.lost) == len(committed["entries"]) - 1


@pytest.mark.parametrize(
    ("current", "message"),
    [
        # A newer scanner that drops or retunes a plugin reports fewer hits: that is not clean.
        (scan({"layer-1/x": [("Basic Auth Credentials", "aa", 1)]}, version="1.6.0"), "plugins and filters"),
        (scan({"layer-1/x": [("Basic Auth Credentials", "aa", 1)]}, plugins_used=[]), "plugins and filters"),
        # So does a changed filter: an extra exclusion suppresses hits while keeping known ones.
        (scan({"layer-1/x": [("Basic Auth Credentials", "aa", 1)]},
              filters_used=[*PROFILE["filters_used"],
                            {"path": "detect_secrets.filters.regex.should_exclude_file",
                             "pattern": ["^layer-17/"]}]), "plugins and filters"),
        (scan({"layer-1/x": [("Basic Auth Credentials", "aa", 1)]}, filters_used=[]), "plugins and filters"),
        # Nothing found at all, or nothing in common: it did not scan the image.
        (scan({}), "found nothing at all"),
        (scan({"layer-1/elsewhere.py": [("Secret Keyword", "zz", 1)]}), "shares no hit"),
        ({"results": {}}, "no scanner version"),
        ({"version": "1.5.0", "plugins_used": PROFILE["plugins_used"], "results": {}}, "or filter list"),
    ],
)
def test_a_scan_that_cannot_be_compared_fails_closed(
    tmp_path: Path, current: dict[str, Any], message: str
) -> None:
    with pytest.raises(ia.ImageAuditError, match=message):
        ia.compare_secrets(current, baseline(tmp_path))


def test_a_baseline_of_another_schema_is_refused() -> None:
    with pytest.raises(ia.ImageAuditError, match="unsupported schema"):
        ia.compare_secrets(AUDITED, {"schema_version": 1, "entries": []})


HEURISTIC = {"path": "detect_secrets.filters.heuristic.is_likely_id_string"}


@pytest.mark.parametrize(
    "filters",
    [
        # No exclusion: the per-build config would be baselined.
        [HEURISTIC],
        # The shapes detect-secrets 1.5.0 really emits: a second --exclude-files joins the same
        # record, while --exclude-lines and --exclude-secrets each add a record of their own.
        [HEURISTIC, {**EXCLUDE, "pattern": [ia.SECRETS_EXCLUDE, "^layer-0[1-9]/"]}],
        [HEURISTIC, EXCLUDE, {"path": "detect_secrets.filters.regex.should_exclude_line", "pattern": ["x"]}],
        [HEURISTIC, EXCLUDE, {"path": "detect_secrets.filters.regex.should_exclude_secret", "pattern": [".*"]}],
        # And a second, separate file-exclusion record, for completeness.
        [HEURISTIC, EXCLUDE, {**EXCLUDE, "pattern": ["^layer-.*"]}],
    ],
)
def test_a_baseline_requires_exactly_the_metadata_exclusion(
    tmp_path: Path, filters: list[dict[str, Any]]
) -> None:
    """Every later release is held to the baseline's filters, so nothing else may be excluded."""
    with pytest.raises(ia.ImageAuditError, match="exclude-files"):
        ia.write_baseline({**AUDITED, "filters_used": filters}, digest="sha256:a",
                          output=tmp_path / "b.json")


def test_the_runbook_scans_with_the_exclusion_the_baseline_binds() -> None:
    runbook = (Path(__file__).resolve().parents[1] / "docs/RELEASING.md").read_text(encoding="utf-8")

    assert f"--exclude-files '{ia.SECRETS_EXCLUDE}'" in runbook


def test_an_empty_scan_cannot_become_a_baseline(tmp_path: Path) -> None:
    with pytest.raises(ia.ImageAuditError, match="empty scan"):
        ia.write_baseline(scan({}), digest="sha256:a", output=tmp_path / "b.json")


def test_the_committed_baseline_is_readable_and_names_its_digest_and_scanner() -> None:
    document = json.loads((Path(__file__).resolve().parents[1] / "security/image-secrets-baseline.json")
                          .read_text(encoding="utf-8"))

    assert document["schema_version"] == ia.BASELINE_SCHEMA
    assert document["digest"].startswith("sha256:")
    assert document["scanner"]["version"] == "1.5.0"
    assert document["scanner"]["plugins_used"]
    assert EXCLUDE in document["scanner"]["filters_used"]
    assert not any(entry["path"] in ia.METADATA_FILES for entry in document["entries"])
    assert document["entries"]
    assert all(not entry["path"].startswith("layer-") for entry in document["entries"])


def test_gzip_helper_produces_real_layers() -> None:
    """Guard the fixture: the layers these tests build are gzip streams, as registry layers are."""
    assert gzip.decompress(layer_bytes(regular("a", b"b")))[:1]
