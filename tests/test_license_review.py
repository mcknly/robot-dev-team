"""Robot Dev Team Project
File: tests/test_license_review.py
Description: Regression tests for the release-SBOM license classification (#82).
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import license_review as lr

REPO_ROOT = Path(__file__).resolve().parents[1]
DIGEST = f"sha256:{'e' * 64}"


def package(
    name: str,
    version: str,
    *,
    purl: str | None,
    declared: str = "NOASSERTION",
    source: str = "acquired package info from the following paths: /usr/bin/tool",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": name,
        "versionInfo": version,
        "licenseDeclared": declared,
        "licenseConcluded": "NOASSERTION",
        "sourceInfo": source,
    }
    if purl is not None:
        record["externalRefs"] = [{"referenceType": "purl", "referenceLocator": purl}]
    return record


SOURCE_NAME = "registry.internal.test/team/robot-dev-team:0123456789abcdef"


def sbom(*packages: dict[str, Any]) -> dict[str, Any]:
    return {"spdxVersion": "SPDX-2.3", "name": SOURCE_NAME, "packages": list(packages)}


# --- SPDX expression classes ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("MIT", lr.PERMISSIVE),
        ("MIT OR Apache-2.0", lr.PERMISSIVE),
        # A distributor may pick the permissive alternative of a disjunction.
        ("MIT OR GPL-2.0-only", lr.PERMISSIVE),
        # A conjunction binds every part, so the most restrictive one wins.
        ("MIT AND GPL-2.0-only", lr.STRONG),
        ("(MIT OR Apache-2.0) AND MPL-2.0", lr.WEAK),
        # AND binds tighter than OR, as SPDX specifies.
        ("MIT OR MPL-2.0 AND GPL-3.0-only", lr.PERMISSIVE),
        ("GPL-2.0-or-later WITH Classpath-exception-2.0", lr.STRONG),
        ("LGPL-2.1-or-later", lr.WEAK),
        ("GFDL-1.3-only", lr.WEAK),
        ("Sleepycat", lr.STRONG),
        ("MS-PL", lr.WEAK),
        ("0BSD OR MIT OR Apache-2.0", lr.PERMISSIVE),
        ("LicenseRef-Mystery", lr.UNKNOWN),
        ("MIT AND LicenseRef-Mystery", lr.UNKNOWN),
    ],
)
def test_expression_class(expression: str, expected: int) -> None:
    assert lr.expression_class(expression) == expected


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        # Debian short names, as Syft passes them through.
        ("LicenseRef-Expat", lr.PERMISSIVE),
        ("LicenseRef-public-domain-md5", lr.PERMISSIVE),
        ("LicenseRef-BSD-3-clause-Regents", lr.PERMISSIVE),
        ("LicenseRef-BSLA", lr.PERMISSIVE),
        ("LicenseRef-MIT-License", lr.PERMISSIVE),
        ("LicenseRef-GPL", lr.STRONG),
        ("LicenseRef-GPL-2--with-link-exception", lr.STRONG),
        # LGPL must never be read as GPL, and a GNU name wins over a permissive-looking suffix.
        ("LicenseRef-LGPL", lr.WEAK),
        ("LicenseRef-LGPLv3--or-GPLv2-", lr.WEAK),
        ("LicenseRef-GFDL-NIV-1.3", lr.WEAK),
        ("LicenseRef-Artistic", lr.WEAK),
    ],
)
def test_debian_short_names_are_classified(identifier: str, expected: int) -> None:
    assert lr.license_class(identifier) == expected


@pytest.mark.parametrize("expression", ["", "(MIT", "MIT)", "MIT OR", ")MIT("])
def test_malformed_expressions_fail(expression: str) -> None:
    with pytest.raises(lr.LicenseReviewError):
        lr.expression_class(expression)


@pytest.mark.parametrize("expression", ["MIT?", "MIT!", "MIT/Apache-2.0", "MIT, Apache-2.0", "MIT;"])
def test_characters_the_tokenizer_cannot_place_fail_closed(expression: str) -> None:
    """`findall` skips what it cannot match; that must never yield a clean permissive result."""
    with pytest.raises(lr.LicenseReviewError, match="unsupported characters"):
        lr.expression_class(expression)


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        # Stems match exactly or followed by `-`, never as bare prefixes.
        ("LicenseRef-Thermal-Copyleft", lr.UNKNOWN),
        ("LicenseRef-BSD-3-clause-Regents", lr.PERMISSIVE),
        # Short stems are exact-only: only the names actually reviewed pass.
        ("LicenseRef-The", lr.PERMISSIVE),
        ("LicenseRef-The-Copyleft", lr.UNKNOWN),
        ("LicenseRef-PD-debian", lr.PERMISSIVE),
        ("LicenseRef-PD-something-else", lr.UNKNOWN),
        ("LicenseRef-PDX", lr.UNKNOWN),
        ("LicenseRef-IBM-as-is", lr.PERMISSIVE),
        ("LicenseRef-IBM-restricted", lr.UNKNOWN),
        ("LicenseRef-FSF-unlimited", lr.PERMISSIVE),
        ("LicenseRef-FSF-new", lr.UNKNOWN),
        ("LicenseRef-BSD3", lr.PERMISSIVE),
        ("LicenseRef-BSDish", lr.UNKNOWN),
        ("LicenseRef-customFSFUL", lr.PERMISSIVE),
        ("LicenseRef-customFSFwhatever", lr.UNKNOWN),
        # GNU All-Permissive, and the license of the GPL's own text: neither is copyleft.
        ("LicenseRef-GAP", lr.PERMISSIVE),
        ("LicenseRef-DONT-CHANGE-THE-GPL", lr.PERMISSIVE),
    ],
)
def test_debian_permissive_stems_are_anchored(identifier: str, expected: int) -> None:
    assert lr.license_class(identifier) == expected


# --- SBOM entries -----------------------------------------------------------------------------


def test_entries_are_keyed_by_type_name_and_version() -> None:
    entry = lr.entry_of(package("adler2", "2.0.1", purl="pkg:cargo/adler2@2.0.1"))

    assert entry.key == "cargo/adler2@2.0.1"
    assert entry.kind == "cargo"
    assert entry.location == "/usr/bin/tool"


def test_the_image_root_entry_never_carries_its_registry_name() -> None:
    """Its SPDX name is the canonical registry reference, which must not reach a tracked file."""
    root = package(
        "registry.internal.test/team/image:abc",
        "sha256:1234",
        purl="pkg:oci/image@sha256%3A1234?repository_url=registry.internal.test/team/image",
        source="",
    )

    entry = lr.entry_of(root)

    assert entry.key == "oci/image"
    assert "internal" not in entry.key + entry.name + entry.version


def test_purl_less_entries_are_keyed_by_path() -> None:
    launcher = package(
        "Simple Launcher",
        "1.1.0.14",
        purl=None,
        source="acquired package info from the following paths: /site-packages/pip/_vendor/distlib/t64.exe",
    )

    assert lr.entry_of(launcher).key == "file:/site-packages/pip/_vendor/distlib/t64.exe"


# --- Classification ---------------------------------------------------------------------------


def evidence(**entries: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {key.replace("__", "/"): value for key, value in entries.items()}


def test_every_entry_is_counted_once_including_duplicates() -> None:
    """uv and uvx embed the same crates; both copies are entries, and both are counted."""
    document = sbom(
        package("adler2", "2.0.1", purl="pkg:cargo/adler2@2.0.1",
                source="acquired package info from the following paths: /usr/local/bin/uv"),
        package("adler2", "2.0.1", purl="pkg:cargo/adler2@2.0.1",
                source="acquired package info from the following paths: /usr/local/bin/uvx"),
        package("click", "8.4.2", purl="pkg:pypi/click@8.4.2", declared="BSD-3-Clause"),
        package("mystery", "1.0", purl="pkg:cargo/mystery@1.0"),
    )
    records = {"cargo/adler2@2.0.1": {"license": "MIT OR Apache-2.0", "source": "crates.io"}}

    summary = lr.summarize(lr.classify(lr.sbom_entries(document), records), DIGEST, "0" * 64)

    assert summary["entries"] == 4
    assert summary["totals"]["resolved"] == 3
    assert summary["totals"]["unresolved"] == 1
    assert summary["totals"]["resolved"] + summary["totals"]["unresolved"] == 4
    assert summary["by_type"]["cargo"]["total"] == 3
    assert summary["unresolved"] == [
        {"key": "cargo/mystery@1.0", "reason": "no declared license in the SBOM and no evidence entry"}
    ]


def test_a_declared_license_wins_over_evidence() -> None:
    entries = lr.sbom_entries(sbom(package("click", "8.4.2", purl="pkg:pypi/click@8.4.2",
                                           declared="BSD-3-Clause")))
    records = {"pypi/click@8.4.2": {"license": "GPL-3.0-only", "source": "manual"}}

    [result] = lr.classify(entries, records)

    assert (result.license, result.source) == ("BSD-3-Clause", "sbom")


def test_evidence_without_a_license_stays_unresolved_with_its_reason() -> None:
    entries = lr.sbom_entries(sbom(package("orphan", "v1.0.0", purl="pkg:golang/orphan@v1.0.0")))
    records = {"golang/orphan@v1.0.0": {"license": None, "source": "deps.dev",
                                        "reason": "deps.dev detected no standard license"}}

    summary = lr.summarize(lr.classify(entries, records), DIGEST, "0" * 64)

    assert summary["unresolved"] == [
        {"key": "golang/orphan@v1.0.0", "reason": "deps.dev detected no standard license"}
    ]


def test_non_permissive_components_are_listed_with_every_location() -> None:
    document = sbom(
        package("option-ext", "0.2.0", purl="pkg:cargo/option-ext@0.2.0",
                source="acquired package info from the following paths: /usr/local/bin/uv"),
        package("option-ext", "0.2.0", purl="pkg:cargo/option-ext@0.2.0",
                source="acquired package info from the following paths: /usr/local/bin/uvx"),
    )
    records = {"cargo/option-ext@0.2.0": {"license": "MPL-2.0", "source": "crates.io"}}

    summary = lr.summarize(lr.classify(lr.sbom_entries(document), records), DIGEST, "0" * 64)

    assert summary["non_permissive"] == {
        "cargo/option-ext@0.2.0": {
            "license": "MPL-2.0",
            "class": "weak copyleft",
            "source": "crates.io",
            "locations": ["/usr/local/bin/uv", "/usr/local/bin/uvx"],
        }
    }


# --- Fetching ---------------------------------------------------------------------------------


def test_resolve_fetches_each_missing_key_once_and_paces_crates_io() -> None:
    document = sbom(
        package("adler2", "2.0.1", purl="pkg:cargo/adler2@2.0.1",
                source="acquired package info from the following paths: /usr/local/bin/uv"),
        package("adler2", "2.0.1", purl="pkg:cargo/adler2@2.0.1",
                source="acquired package info from the following paths: /usr/local/bin/uvx"),
        package("golang.org/x/net", "v0.40.0", purl="pkg:golang/golang.org/x/net@v0.40.0"),
        package("known", "1.0", purl="pkg:cargo/known@1.0"),
        package("declared", "1.0", purl="pkg:cargo/declared@1.0", declared="MIT"),
        package("deb-thing", "1.0", purl="pkg:deb/debian/deb-thing@1.0"),
    )
    records: dict[str, dict[str, Any]] = {"cargo/known@1.0": {"license": "MIT", "source": "crates.io"}}
    fetched: list[str] = []
    pauses: list[float] = []

    def fetch(url: str) -> Any:
        fetched.append(url)
        if "crates.io" in url:
            return {"version": {"license": "0BSD OR MIT OR Apache-2.0"}}
        return {"licenses": ["BSD-3-Clause"]}

    added = lr.resolve(lr.sbom_entries(document), records, fetch=fetch, retrieved="2026-09-23",
                       pause=pauses.append)

    assert added == ["cargo/adler2@2.0.1", "golang/golang.org/x/net@v0.40.0"]
    assert fetched == [
        "https://crates.io/api/v1/crates/adler2/2.0.1",
        "https://api.deps.dev/v3/systems/go/packages/golang.org%2Fx%2Fnet/versions/v0.40.0",
    ]
    assert pauses == [lr.CRATES_IO_INTERVAL]
    assert records["cargo/adler2@2.0.1"] == {
        "license": "0BSD OR MIT OR Apache-2.0",
        "source": "crates.io",
        "evidence": "https://crates.io/api/v1/crates/adler2/2.0.1",
        "retrieved": "2026-09-23",
    }
    # A Debian package is never fetched: its copyright file is the source, read by hand.
    assert "deb/debian/deb-thing@1.0" not in records


def test_deps_dev_joins_every_detected_license() -> None:
    entry = lr.Entry("golang/m@v1", "golang", "m", "v1", "", "NOASSERTION")

    record = lr.deps_dev_record(entry, lambda url: {"licenses": ["MIT", "Apache-2.0 OR MIT"]})

    assert record["license"] == "MIT AND (Apache-2.0 OR MIT)"
    assert lr.expression_class(record["license"]) == lr.PERMISSIVE


def test_unidentified_license_text_beside_a_standard_one_goes_to_review() -> None:
    """Recording only the recognisable half would read an unknown file as permissive."""
    entry = lr.Entry("golang/m@v1", "golang", "m", "v1", "", "NOASSERTION")

    record = lr.deps_dev_record(entry, lambda url: {"licenses": ["MIT", "non-standard"]})

    assert record["license"] is None
    assert "non-standard" in record["reason"]


@pytest.mark.parametrize("payload", [None, {"licenses": []}, {"licenses": ["non-standard"]}])
def test_deps_dev_without_a_license_records_a_reason(payload: Any) -> None:
    entry = lr.Entry("golang/m@v1", "golang", "m", "v1", "", "NOASSERTION")

    record = lr.deps_dev_record(entry, lambda url: payload)

    assert record["license"] is None
    assert record["reason"]


@pytest.mark.parametrize(
    ("stored", "normalized"),
    [("MIT/Apache-2.0", "MIT OR Apache-2.0"), ("Apache-2.0 / MIT", "Apache-2.0 OR MIT"),
     ("MIT OR Apache-2.0", "MIT OR Apache-2.0")],
)
def test_crates_io_legacy_slash_means_or(stored: str, normalized: str) -> None:
    entry = lr.Entry("cargo/c@1", "cargo", "c", "1", "", "NOASSERTION")

    record = lr.crates_io_record(entry, lambda url: {"version": {"license": stored}})

    assert record["license"] == normalized
    assert lr.expression_class(record["license"]) == lr.PERMISSIVE


def test_prune_keeps_only_evidence_the_sbom_needs() -> None:
    entries = lr.sbom_entries(sbom(
        package("adler2", "2.0.1", purl="pkg:cargo/adler2@2.0.1"),
        package("click", "8.4.2", purl="pkg:pypi/click@8.4.2", declared="BSD-3-Clause"),
    ))
    records: dict[str, dict[str, Any]] = {
        "cargo/adler2@2.0.1": {"license": "MIT", "source": "s"},
        "cargo/uv-internal@0.0.68": {"license": "MIT", "source": "s"},
        # Declared in the SBOM, so evidence for it is never consulted and is dropped too.
        "pypi/click@8.4.2": {"license": "MIT", "source": "s"},
    }

    dropped = lr.prune_evidence(entries, records)

    assert dropped == ["cargo/uv-internal@0.0.68", "pypi/click@8.4.2"]
    assert list(records) == ["cargo/adler2@2.0.1"]


@pytest.mark.parametrize("payload", [None, {"version": {"license": None}}])
def test_crates_io_without_a_license_records_a_reason(payload: Any) -> None:
    entry = lr.Entry("cargo/c@1", "cargo", "c", "1", "", "NOASSERTION")

    record = lr.crates_io_record(entry, lambda url: payload)

    assert record["license"] is None
    assert record["reason"]


def test_a_publisher_sbom_replaces_fetched_evidence_and_reports_disagreements() -> None:
    document = {
        "bomFormat": "CycloneDX",
        "components": [
            {"name": "adler2", "version": "2.0.1", "licenses": [{"expression": "0BSD OR MIT OR Apache-2.0"}]},
            {"name": "option-ext", "version": "0.2.0", "licenses": [{"license": {"id": "MPL-2.0"}}]},
            {"name": "unlicensed", "version": "1.0"},
        ],
    }
    records: dict[str, dict[str, Any]] = {
        "cargo/adler2@2.0.1": {"license": "MIT", "source": "crates.io", "evidence": "https://x"},
    }

    written, disagreed = lr.import_cyclonedx(
        document, records, kind="cargo", label="/opt/uv.cdx.json", retrieved="2026-09-23"
    )

    assert written == ["cargo/adler2@2.0.1", "cargo/option-ext@0.2.0"]
    assert records["cargo/adler2@2.0.1"] == {
        "license": "0BSD OR MIT OR Apache-2.0",
        "source": "publisher SBOM",
        "evidence": "/opt/uv.cdx.json",
        "retrieved": "2026-09-23",
    }
    assert records["cargo/option-ext@0.2.0"]["license"] == "MPL-2.0"
    # A component with no license is left for the registry lookup rather than recorded empty.
    assert "cargo/unlicensed@1.0" not in records
    assert disagreed == [
        "cargo/adler2@2.0.1: crates.io 'MIT' vs '0BSD OR MIT OR Apache-2.0'"
    ]


def test_a_publisher_sbom_never_overwrites_a_manual_entry() -> None:
    """Someone read that one by hand; the disagreement is reported, the entry is kept."""
    document = {"components": [{"name": "odd", "version": "1.0", "licenses": [{"expression": "MIT"}]}]}
    manual = {"license": "MIT AND Zlib", "source": "image", "evidence": "image:/usr/share/doc/odd"}
    records: dict[str, dict[str, Any]] = {"cargo/odd@1.0": dict(manual)}

    written, disagreed = lr.import_cyclonedx(
        document, records, kind="cargo", label="/opt/uv.cdx.json", retrieved="2026-09-23"
    )

    assert written == []
    assert records["cargo/odd@1.0"] == manual
    assert disagreed == ["cargo/odd@1.0: image 'MIT AND Zlib' vs 'MIT'"]


def test_the_import_command_pins_the_publisher_sbom_by_hash(tmp_path: Path) -> None:
    cyclonedx = tmp_path / "uv.cdx.json"
    raw = json.dumps({"components": [{"name": "c", "version": "1", "licenses": [{"expression": "MIT"}]}]})
    cyclonedx.write_text(raw, encoding="utf-8")
    evidence_path = tmp_path / "evidence.json"

    code = lr.main(["--evidence", str(evidence_path), "import-cyclonedx", "--cyclonedx", str(cyclonedx),
                    "--kind", "cargo", "--label", "image:/uv.cdx.json"])

    assert code == 0
    record = lr.load_evidence(evidence_path)["cargo/c@1"]
    assert record["evidence"] == f"image:/uv.cdx.json sha256:{hashlib.sha256(raw.encode()).hexdigest()}"
    assert record["retrieved"]


def test_several_license_choices_are_all_required() -> None:
    component = {"licenses": [{"license": {"id": "MIT"}}, {"expression": "Apache-2.0 OR MIT"}]}

    assert lr.cyclonedx_license(component) == "MIT AND (Apache-2.0 OR MIT)"


# --- The evidence file ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"schema_version": 9, "entries": {}}, "unsupported schema"),
        ({"schema_version": 1}, "no entries object"),
        ({"schema_version": 1, "entries": {"k": {"license": "MIT"}}}, "has no source"),
        ({"schema_version": 1, "entries": {"k": {"license": None, "source": "x"}}},
         "no license and no reason"),
    ],
)
def test_malformed_evidence_is_rejected(tmp_path: Path, document: dict[str, Any], message: str) -> None:
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(lr.LicenseReviewError, match=message):
        lr.load_evidence(path)


def test_evidence_round_trips_deterministically(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    records = {"b/x@1": {"license": "MIT", "source": "s"}, "a/y@1": {"license": "MIT", "source": "s"}}

    lr.write_evidence(path, records)
    first = path.read_bytes()
    lr.write_evidence(path, lr.load_evidence(path))

    assert path.read_bytes() == first
    assert list(json.loads(first)["entries"]) == ["a/y@1", "b/x@1"]


def test_the_committed_evidence_file_is_valid() -> None:
    records = lr.load_evidence(REPO_ROOT / lr.EVIDENCE_PATH)

    assert records
    for key, record in records.items():
        assert "/" in key or key.startswith("file:"), key
        if record.get("license"):
            # Every recorded expression must parse; an unparseable one would crash classify.
            lr.expression_class(record["license"])
        # Manual entries have to say where their evidence was read.
        assert record.get("evidence"), f"{key} records no evidence location"


def run_classify(tmp_path: Path, *packages: dict[str, Any], **options: str) -> tuple[int, Path]:
    sbom_path = tmp_path / "sbom.json"
    sbom_path.write_text(json.dumps(sbom(*packages)), encoding="utf-8")
    output = tmp_path / "summary.json"
    recorded = options.get("sbom_sha256", hashlib.sha256(sbom_path.read_bytes()).hexdigest())
    argv = ["--evidence", str(tmp_path / "none.json"), "classify", "--sbom", str(sbom_path),
            "--digest", DIGEST, "--source-name", options.get("source_name", SOURCE_NAME),
            "--sbom-sha256", recorded, "--output", str(output)]
    return lr.main(argv), output


CLICK = package("click", "8.4.2", purl="pkg:pypi/click@8.4.2", declared="BSD-3-Clause")


def test_classify_command_writes_a_summary_bound_to_the_sbom_bytes(tmp_path: Path) -> None:
    code, output = run_classify(tmp_path, CLICK)

    assert code == 0
    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["digest"] == DIGEST
    assert summary["totals"]["resolved"] == 1
    raw = (tmp_path / "sbom.json").read_bytes()
    assert summary["sbom_sha256"] == hashlib.sha256(raw).hexdigest()
    # The SPDX name carries the canonical registry host; it is checked, never written out.
    assert SOURCE_NAME not in output.read_text(encoding="utf-8")


def test_classify_fails_while_an_entry_is_unresolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A re-run against a new digest must not report success with entries unaccounted for."""
    code, output = run_classify(tmp_path, CLICK, package("mystery", "1.0", purl="pkg:cargo/mystery@1.0"))

    assert code == 1
    assert "UNRESOLVED: cargo/mystery@1.0" in capsys.readouterr().err
    assert json.loads(output.read_text(encoding="utf-8"))["totals"]["unresolved"] == 1


def test_classify_fails_while_a_license_needs_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unclassified license is resolved but not understood, so it must not pass silently."""
    code, output = run_classify(
        tmp_path, CLICK, package("weird", "1.0", purl="pkg:pypi/weird@1.0", declared="LicenseRef-Weird")
    )

    assert code == 1
    assert "NEEDS REVIEW: pypi/weird@1.0: LicenseRef-Weird" in capsys.readouterr().err
    assert json.loads(output.read_text(encoding="utf-8"))["needs_review"] == [
        {"key": "pypi/weird@1.0", "license": "LicenseRef-Weird"}
    ]


def test_classify_refuses_an_sbom_for_a_different_image(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, output = run_classify(tmp_path, CLICK, source_name="registry.internal.test/team/other:ffff")

    assert code == 1
    assert "describes a different image" in capsys.readouterr().err
    assert not output.exists()


def test_classify_refuses_bytes_other_than_the_registry_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, output = run_classify(tmp_path, CLICK, sbom_sha256=f"sha256:{'0' * 64}")

    assert code == 1
    assert "not the digest-keyed package file's" in capsys.readouterr().err
    assert not output.exists()


def test_classify_accepts_the_registry_recorded_hash_with_or_without_its_prefix(tmp_path: Path) -> None:
    sbom_path = tmp_path / "sbom.json"
    sbom_path.write_text(json.dumps(sbom(CLICK)), encoding="utf-8")
    recorded = hashlib.sha256(sbom_path.read_bytes()).hexdigest()

    assert run_classify(tmp_path, CLICK, sbom_sha256=recorded)[0] == 0
    assert run_classify(tmp_path, CLICK, sbom_sha256=f"sha256:{recorded}")[0] == 0


def test_classify_cannot_run_without_the_recorded_hash(tmp_path: Path) -> None:
    """Without it the digest is only a label, and the summary would read like a verified one."""
    sbom_path = tmp_path / "sbom.json"
    sbom_path.write_text(json.dumps(sbom(CLICK)), encoding="utf-8")

    with pytest.raises(SystemExit) as caught:
        lr.main(["classify", "--sbom", str(sbom_path), "--digest", DIGEST,
                 "--source-name", SOURCE_NAME])

    assert caught.value.code == 2


def test_an_sbom_that_is_not_utf8_fails_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sbom_path = tmp_path / "sbom.json"
    sbom_path.write_bytes(b'{"name": "\xff"}')

    code = lr.main(["--evidence", str(tmp_path / "none.json"), "classify", "--sbom", str(sbom_path),
                    "--digest", DIGEST, "--source-name", SOURCE_NAME,
                    "--sbom-sha256", hashlib.sha256(sbom_path.read_bytes()).hexdigest()])

    assert code == 1
    assert "[license] ERROR:" in capsys.readouterr().err
