"""Robot Dev Team Project
File: tests/test_release_contract.py
Description: Repository-state checks that shipped release metadata agrees.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from scripts import release_tools

REPO_ROOT = Path(__file__).resolve().parents[1]
UNRELEASED_HEADING = "## [Unreleased]"
RELOCK_HINT = "run 'uv lock' after changing [project].version"
# A fully qualified base-image pin: readable tag *and* OCI index digest. The pattern is
# deliberately loose about the Debian codename so #59's bookworm -> trixie move does not have to
# touch this file, and strict about the digest so a tag-only reference cannot pass.
PYTHON_IMAGE_PIN = re.compile(
    r"python:(?P<version>\d+\.\d+\.\d+)-slim-(?P<codename>[a-z]+)@sha256:[0-9a-f]{64}"
)
# The APT suites the `Dockerfile` writes into /etc/apt/sources.list. Each is `<codename>`,
# `<codename>-updates`, or `<codename>-security` under a snapshot.debian.org archive.
DOCKERFILE_APT_SUITE = re.compile(
    r"snapshot\.debian\.org/archive/debian(?:-security)?/\$\{DEBIAN_SNAPSHOT\}/ "
    r"(?P<suite>[a-z]+)(?P<qualifier>-updates|-security)? main"
)
# Any whitespace-delimited token naming a Python image, however malformed. Collection is
# deliberately separated from validation: see `_python_image_references`.
PYTHON_IMAGE_TOKEN = re.compile(r"\S*python:\S*")
DOCKERFILE_PYTHON_IMAGE = re.compile(r"^ARG PYTHON_IMAGE=(?P<pin>\S+)$", re.MULTILINE)
# A line that begins a new top-level YAML key -- a job, a template, or `stages:`. Used to bound
# a job block, since anything indented under it belongs to that job and nothing else does.
TOP_LEVEL_KEY = re.compile(r"^[^\s#]")
# The one job that is deliberately not on the runtime interpreter. It executes the
# `requires-python` floor, so its pin must differ from the runtime's by design.
FLOOR_JOB = "compat_python_floor:"
# The checked-in reference SBOM. Not the document a release publishes -- `sbom_publish` stages
# the smoke-tested image's own bytes -- but the one a reader consults in the repository.
SBOM_PATH = Path("sbom") / "sbom.spdx.json"
# Syft records the interpreter as a `binary`-type package discovered on /usr/local/bin/pythonX.Y,
# which is the same artifact the CPython findings and their exceptions are scoped to.
SBOM_PYTHON_PACKAGE = "python"
# A Debian package reference in the reference SBOM, with the distro qualifier Syft derives from
# the image's own /etc/os-release: `pkg:deb/debian/base-files@13.8%2Bdeb13u6?arch=amd64&
# distro=debian-13`. The qualifier carries the major release, never the codename, which is why
# binding it to `PYTHON_IMAGE` needs `DEBIAN_RELEASES` below.
SBOM_DEB_PURL = re.compile(r"pkg:deb/debian/[^?\s]+\?(?P<qualifiers>\S+)")
SBOM_DEB_DISTRO = re.compile(r"(?:^|&)distro=debian-(?P<release>\d+)(?:\.\S*)?(?:&|$)")
# Debian codename -> major release. A table is a real maintenance cost -- the next distro hop
# fails this file until an entry is added -- and it is accepted for the same reason
# `EOL_DISTRO_RELEASES` is kept: the mapping is not derivable from anything in the tree, and
# the alternative is an artifact that cannot be checked against the pin at all. Failing loudly
# on an unknown codename is the point; the entry is one line and the message says so.
DEBIAN_RELEASES = {"bullseye": "11", "bookworm": "12", "trixie": "13", "forky": "14"}


def _load_sbom(root: Path) -> dict[str, Any]:
    """Return the parsed reference SBOM."""
    path = root / SBOM_PATH
    assert path.is_file(), f"release contract: {path} is missing"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssertionError(f"release contract: {path} is not readable JSON: {exc}") from exc
    assert isinstance(document, dict), f"release contract: {path} is not an SPDX document"
    return document


def _sbom_packages(root: Path) -> list[dict[str, Any]]:
    """Return the reference SBOM's SPDX package records."""
    packages = _load_sbom(root).get("packages")
    assert isinstance(packages, list) and packages, (
        f"release contract: {root / SBOM_PATH} carries no SPDX packages; regenerate it with "
        f"scripts/generate-sbom.sh"
    )
    return [entry for entry in packages if isinstance(entry, dict)]


def _sbom_package_versions(root: Path, name: str) -> list[str]:
    """Return every ``versionInfo`` the reference SBOM records for ``name``."""
    return [
        str(entry.get("versionInfo"))
        for entry in _sbom_packages(root)
        if entry.get("name") == name
    ]


def _sbom_debian_releases(root: Path) -> dict[str, list[str]]:
    """Return the Debian release each deb package in the reference SBOM was built for.

    Keyed by release so a mixed document reports every value it carries rather than the first
    one found -- an SBOM naming two releases is a different and worse failure than one naming
    the wrong release, and the message should be able to say which it is.
    """
    releases: dict[str, list[str]] = {}
    for entry in _sbom_packages(root):
        references = entry.get("externalRefs")
        if not isinstance(references, list):
            continue
        for reference in references:
            if not isinstance(reference, dict):
                continue
            purl = SBOM_DEB_PURL.fullmatch(str(reference.get("referenceLocator", "")))
            if purl is None:
                continue
            distro = SBOM_DEB_DISTRO.search(purl.group("qualifiers"))
            release = distro.group("release") if distro else "<unqualified>"
            releases.setdefault(release, []).append(str(entry.get("name")))
    return releases


def _series(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _runtime_python_pin(root: Path) -> tuple[str, str]:
    """Return the (pin, interpreter version) the ``Dockerfile`` ships."""
    pin, pinned = _runtime_python_match(root)
    return pin, pinned.group("version")


def _runtime_python_match(root: Path) -> tuple[str, re.Match[str]]:
    """Return the ``Dockerfile``'s ``PYTHON_IMAGE`` pin and its parsed form."""
    dockerfile = root / "Dockerfile"
    match = DOCKERFILE_PYTHON_IMAGE.search(dockerfile.read_text(encoding="utf-8"))
    assert match, f"release contract: {dockerfile} declares no ARG PYTHON_IMAGE default"
    pin = match.group("pin")
    pinned = PYTHON_IMAGE_PIN.fullmatch(pin)
    assert pinned, (
        f"release contract: {dockerfile}'s PYTHON_IMAGE ({pin}) is not pinned to both a "
        f"readable python:X.Y.Z-slim-<codename> tag and an OCI index digest"
    )
    return pin, pinned


def _python_image_references(ci_text: str) -> list[tuple[int, str]]:
    """Return every ``(line number, token)`` in ``ci_text`` that names a Python image.

    Collection is on the bare ``python:`` token, *not* on `PYTHON_IMAGE_PIN`, and the two
    steps are separate on purpose. A scan that collects by the strict pattern can only ever
    report pins that already look right: `image: python:3.14.7-slim-bookworm` with the digest
    dropped, or the floating `python:3.14-slim-bookworm@sha256:...`, would simply never be
    collected, so they could not land in any later comparison and the test would pass. Scanning
    for the token and then requiring each hit to be well formed inverts that default from
    "an unrecognized shape is ignored" to "an unrecognized shape must be justified".

    Comment lines are skipped. A pin named in prose configures nothing, and the rationale
    comments around these jobs discuss the pins they exclude.
    """
    references: list[tuple[int, str]] = []
    for number, line in enumerate(ci_text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        content = line.split(" #", 1)[0]
        for match in PYTHON_IMAGE_TOKEN.finditer(content):
            references.append((number, match.group(0).strip("\"'")))
    return references


def _floor_python_pin(ci_text: str) -> tuple[int, str]:
    """Return the ``(line number, pin)`` used by the compatibility-floor job.

    The search is bounded to the job's own block. An unbounded search would run on into the
    next job when this one has no usable `image:` line, pick up the runtime pin there, and
    report "the floor is running the runtime pin" for a lane that declares no pin at all.
    """
    lines = ci_text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith(FLOOR_JOB)), None)
    assert start is not None, (
        f"release contract: .gitlab-ci.yml defines no {FLOOR_JOB[:-1]} job; the "
        f"requires-python floor is advertised in pyproject.toml, README.md and "
        f"docs/AGENT_ONBOARDING.md and has to be executed somewhere"
    )
    end = next(
        (index for index in range(start + 1, len(lines)) if TOP_LEVEL_KEY.match(lines[index])),
        len(lines),
    )
    references = [
        (start + offset, token)
        for offset, token in _python_image_references("\n".join(lines[start:end]))
    ]
    assert len(references) == 1, (
        f"release contract: {FLOOR_JOB[:-1]} names {len(references)} Python images; the "
        f"compatibility floor lane runs on exactly one interpreter and has to declare it"
    )
    number, token = references[0]
    assert PYTHON_IMAGE_PIN.fullmatch(token), (
        f"release contract: {FLOOR_JOB[:-1]} (.gitlab-ci.yml line {number}) declares {token}, "
        f"which is not pinned to both a readable python:X.Y.Z-slim-<codename> tag and an OCI "
        f"index digest"
    )
    return number, token


def _load_toml(path: Path) -> dict[str, Any]:
    assert path.is_file(), f"release contract: {path} is missing"
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise AssertionError(f"release contract: {path} is not readable TOML: {exc}") from exc


def check_release_metadata(root: Path) -> None:
    """Assert the version, lockfile, and changelog in ``root`` describe one release.

    Failures are phrased for whoever prepared the release, since this is the first
    place a release-preparation mistake surfaces.
    """
    pyproject_path = root / "pyproject.toml"
    pyproject = _load_toml(pyproject_path)
    version = release_tools.project_version(pyproject_path)
    project_name = pyproject.get("project", {}).get("name")
    assert isinstance(project_name, str) and project_name, (
        f"release contract: {pyproject_path} declares no [project].name"
    )

    lock_path = root / "uv.lock"
    lock = _load_toml(lock_path)
    packages = lock.get("package")
    assert isinstance(packages, list), (
        f"release contract: {lock_path} has no readable [[package]] entries"
    )
    root_versions = [
        package.get("version")
        for package in packages
        if isinstance(package, dict)
        and package.get("name") == project_name
        and package.get("source") == {"editable": "."}
    ]
    assert root_versions, (
        f"release contract: {lock_path} holds no editable root entry for "
        f"'{project_name}'; {RELOCK_HINT}"
    )
    assert len(root_versions) == 1, (
        f"release contract: {lock_path} holds {len(root_versions)} editable root entries for "
        f"'{project_name}'; {RELOCK_HINT}"
    )
    assert root_versions[0] == str(version), (
        f"release contract: {lock_path} records {project_name} {root_versions[0]} while "
        f"{pyproject_path} declares {version}; {RELOCK_HINT}"
    )

    changelog_path = root / "docs" / "CHANGELOG.md"
    # changelog_excerpt() raises ReleaseError when no dated section matches the shipped
    # version, so this call is the "the section exists and is dated" half of the check.
    # It also returns the exact excerpt the tag pipeline publishes as the release notes.
    excerpt, release_date = release_tools.changelog_excerpt(changelog_path, version)
    expected_heading = f"## [v{version}] - {release_date}"

    lines = changelog_path.read_text(encoding="utf-8").splitlines()
    # Match whole lines, never substrings: release notes quote versions and headings
    # in prose, and a released section may repeat a version under a corrected date.
    release_headings = [
        line for line in lines if release_tools.CHANGELOG_HEADING.fullmatch(line) is not None
    ]
    version_headings = [
        line for line in release_headings if line.startswith(f"## [v{version}] - ")
    ]
    assert version_headings == [expected_heading], (
        f"release contract: {changelog_path} must hold exactly one dated section for "
        f"v{version}, found {version_headings}"
    )
    assert release_headings[0] == expected_heading, (
        f"release contract: {expected_heading} must be the topmost release section in "
        f"{changelog_path}, found {release_headings[0]}"
    )

    body = excerpt.split("\n", 1)[1].strip() if "\n" in excerpt else ""
    assert body, (
        f"release contract: {expected_heading} in {changelog_path} carries no release "
        "notes; this section becomes the GitLab Release description and the durable "
        "changelog.md package file"
    )

    assert lines.count(UNRELEASED_HEADING) == 1, (
        f"release contract: {changelog_path} must hold exactly one "
        f"'{UNRELEASED_HEADING}' heading, found {lines.count(UNRELEASED_HEADING)}"
    )
    assert lines.index(UNRELEASED_HEADING) < lines.index(expected_heading), (
        f"release contract: '{UNRELEASED_HEADING}' must stay above {expected_heading} "
        f"in {changelog_path}"
    )


DEFAULT_CHANGELOG = (
    "# Changelog\n"
    "\n"
    f"{UNRELEASED_HEADING}\n"
    "\n"
    "## [v0.2.1] - 2026-07-28\n"
    "\n"
    "### Bug fixes\n"
    "- Publish the release record through the API.\n"
    "\n"
    "## [v0.2.0] - 2026-07-27\n"
    "\n"
    "### Features\n"
    "- Stable release support.\n"
)


def write_contract_tree(
    tmp_path: Path,
    *,
    version: str = "0.2.1",
    lock_version: str | None = None,
    lock_name: str = "robot-dev-team",
    changelog: str = DEFAULT_CHANGELOG,
) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "robot-dev-team"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        (
            "version = 1\n\n"
            "[[package]]\n"
            'name = "httpx"\n'
            'version = "0.28.1"\n'
            'source = { registry = "https://pypi.org/simple" }\n\n'
            "[[package]]\n"
            f'name = "{lock_name}"\n'
            f'version = "{lock_version or version}"\n'
            'source = { editable = "." }\n'
        ),
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    return tmp_path


def test_shipped_release_metadata_agrees() -> None:
    """Fail in the MR pipeline before an inconsistent release can be tagged."""
    check_release_metadata(REPO_ROOT)


def test_agreeing_tree_passes(tmp_path: Path) -> None:
    check_release_metadata(write_contract_tree(tmp_path))


def test_unreleased_section_may_accumulate_notes(tmp_path: Path) -> None:
    """Ordinary development adds notes under [Unreleased]; that is not a failure.

    Release preparation moves them down into the dated section (docs/RELEASING.md
    prep steps 2 and 3), and the empty-notes check below is what proves that
    happened. Requiring an empty [Unreleased] here would fail every feature MR.
    """
    changelog = DEFAULT_CHANGELOG.replace(
        f"{UNRELEASED_HEADING}\n\n",
        f"{UNRELEASED_HEADING}\n\n### Features\n- Something merged since v0.2.1.\n\n",
    )
    check_release_metadata(write_contract_tree(tmp_path, changelog=changelog))


def test_lockfile_version_drift_fails(tmp_path: Path) -> None:
    root = write_contract_tree(tmp_path, version="0.2.1", lock_version="0.2.0")
    with pytest.raises(AssertionError, match="uv lock"):
        check_release_metadata(root)


def test_missing_editable_root_entry_fails(tmp_path: Path) -> None:
    root = write_contract_tree(tmp_path, lock_name="renamed-project")
    with pytest.raises(AssertionError, match="no editable root entry"):
        check_release_metadata(root)


def test_unreadable_lockfile_packages_fail(tmp_path: Path) -> None:
    root = write_contract_tree(tmp_path)
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="no readable"):
        check_release_metadata(root)


def test_malformed_lockfile_toml_fails(tmp_path: Path) -> None:
    root = write_contract_tree(tmp_path)
    (root / "uv.lock").write_text("[[package]\nname = ", encoding="utf-8")
    with pytest.raises(AssertionError, match="uv.lock is not readable TOML"):
        check_release_metadata(root)


def test_duplicate_editable_root_entries_fail(tmp_path: Path) -> None:
    """Two root entries are a broken lockfile, not a version disagreement."""
    root = write_contract_tree(tmp_path)
    lock_path = root / "uv.lock"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8")
        + (
            "\n[[package]]\n"
            'name = "robot-dev-team"\n'
            'version = "0.2.1"\n'
            'source = { editable = "." }\n'
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="holds 2 editable root entries"):
        check_release_metadata(root)


def test_missing_lockfile_fails(tmp_path: Path) -> None:
    root = write_contract_tree(tmp_path)
    (root / "uv.lock").unlink()
    with pytest.raises(AssertionError, match="uv.lock is missing"):
        check_release_metadata(root)


def test_missing_release_section_fails(tmp_path: Path) -> None:
    changelog = DEFAULT_CHANGELOG.replace("## [v0.2.1] - 2026-07-28\n\n", "")
    root = write_contract_tree(tmp_path, changelog=changelog)
    with pytest.raises(release_tools.ReleaseError, match=r"dated '## \[v0\.2\.1\]"):
        check_release_metadata(root)


def test_duplicate_version_section_under_another_date_fails(tmp_path: Path) -> None:
    changelog = DEFAULT_CHANGELOG.replace(
        "## [v0.2.0] - 2026-07-27",
        "## [v0.2.1] - 2026-07-27",
    )
    root = write_contract_tree(tmp_path, changelog=changelog)
    with pytest.raises(AssertionError, match="exactly one dated section"):
        check_release_metadata(root)


def test_release_section_below_an_older_release_fails(tmp_path: Path) -> None:
    changelog = (
        "# Changelog\n"
        "\n"
        f"{UNRELEASED_HEADING}\n"
        "\n"
        "## [v0.2.0] - 2026-07-27\n"
        "\n"
        "- Stable release support.\n"
        "\n"
        "## [v0.2.1] - 2026-07-28\n"
        "\n"
        "- Publish the release record through the API.\n"
    )
    root = write_contract_tree(tmp_path, changelog=changelog)
    with pytest.raises(AssertionError, match="topmost release section"):
        check_release_metadata(root)


def test_release_section_without_notes_fails(tmp_path: Path) -> None:
    changelog = DEFAULT_CHANGELOG.replace(
        "## [v0.2.1] - 2026-07-28\n\n### Bug fixes\n- Publish the release record"
        " through the API.\n",
        "## [v0.2.1] - 2026-07-28\n",
    )
    root = write_contract_tree(tmp_path, changelog=changelog)
    with pytest.raises(AssertionError, match="carries no release notes"):
        check_release_metadata(root)


def test_missing_unreleased_heading_fails(tmp_path: Path) -> None:
    changelog = DEFAULT_CHANGELOG.replace(f"{UNRELEASED_HEADING}\n\n", "")
    root = write_contract_tree(tmp_path, changelog=changelog)
    with pytest.raises(AssertionError, match="exactly one '## \\[Unreleased\\]' heading"):
        check_release_metadata(root)


def test_unreleased_heading_below_the_release_section_fails(tmp_path: Path) -> None:
    changelog = (
        "# Changelog\n"
        "\n"
        "## [v0.2.1] - 2026-07-28\n"
        "\n"
        "- Publish the release record through the API.\n"
        "\n"
        f"{UNRELEASED_HEADING}\n"
    )
    root = write_contract_tree(tmp_path, changelog=changelog)
    with pytest.raises(AssertionError, match="must stay above"):
        check_release_metadata(root)


def test_prose_quoting_a_release_heading_does_not_fail(tmp_path: Path) -> None:
    """Substring counting used to trip over notes that quote a heading."""
    changelog = DEFAULT_CHANGELOG.replace(
        "- Publish the release record through the API.",
        "- Publish the release record through the API; supersedes"
        " `## [v0.2.1] - 2026-07-28` in the draft notes.",
    )
    check_release_metadata(write_contract_tree(tmp_path, changelog=changelog))


def test_security_stage_pyyaml_pin_matches_the_project() -> None:
    """The security jobs install PyYAML directly, so that pin can drift from the project's.

    They cannot use the lockfile: both release jobs run a bare slim-Python image with no
    project dependencies, and the exception file is the one YAML the gate reads. A scanner
    parsing the policy under a different PyYAML than the project resolves is exactly the kind
    of silent divergence this repository writes contract tests for.
    """
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    pinned = [
        dependency
        for dependency in project["project"]["dependencies"]
        if dependency.lower().startswith("pyyaml==")
    ]
    assert len(pinned) == 1, "pyyaml must stay an exactly pinned direct dependency"
    ci = (root / ".gitlab-ci.yml").read_text(encoding="utf-8")
    assert f'"{pinned[0]}"' in ci, (
        f"the security stage must install {pinned[0]}, the version the project pins"
    )


def test_shipped_exception_file_is_parseable() -> None:
    """The gate's day-one policy is checked in, so a malformed edit fails on the merge request
    rather than in the release pipeline."""
    root = Path(__file__).resolve().parents[1]
    exceptions, raw = release_tools.load_exceptions(root / release_tools.EXCEPTIONS_PATH)

    assert raw
    assert exceptions
    for entry in exceptions:
        assert entry.owner
        assert entry.rationale
        assert entry.tracking_issue


def test_runtime_python_image_pins_agree() -> None:
    """Every job meant to mirror the runtime carries the `Dockerfile`'s exact Python pin.

    The pin is repeated across the `Dockerfile` and most `.gitlab-ci.yml` jobs, and nothing
    else notices when one copy is missed -- the interpreter that builds and signs a release
    would simply stop being the one it ships. The scan is over the raw file rather than over
    parsed job keys on purpose: `.security_scan` is a hidden template extended by two jobs, so
    enumerating jobs is exactly how the template reference gets missed. It collects on the
    `python:` token rather than on the pin pattern for the same reason one level down -- see
    `_python_image_references`.

    `compat_python_floor` is the one deliberate exclusion. It runs the `requires-python` floor,
    so it must *not* move when `PYTHON_IMAGE` moves -- but the exclusion is on the *version*
    axes only. Its codename follows the runtime's, and that half is asserted here too.
    """
    root = Path(__file__).resolve().parents[1]
    runtime_pin, runtime_pinned = _runtime_python_match(root)
    runtime_version = runtime_pinned.group("version")
    ci_path = root / ".gitlab-ci.yml"
    ci_text = ci_path.read_text(encoding="utf-8")
    floor_line, floor_pin = _floor_python_pin(ci_text)

    references = _python_image_references(ci_text)
    assert references, f"release contract: {ci_path} references no Python image"

    # Well formed first, equal second. A reference that is not fully qualified cannot be
    # meaningfully compared to anything: it resolves to whatever the registry serves that day.
    malformed = [
        (number, token)
        for number, token in references
        if PYTHON_IMAGE_PIN.fullmatch(token) is None
    ]
    assert not malformed, (
        f"release contract: {ci_path} names Python images that are not pinned to both a "
        f"readable python:X.Y.Z-slim-<codename> tag and an OCI index digest: "
        + ", ".join(f"line {number}: {token}" for number, token in malformed)
        + ". A floating minor or a tag with no digest defeats the equality check below, "
        "because the interpreter it resolves to is not fixed by this file."
    )

    assert floor_pin != runtime_pin, (
        f"release contract: {FLOOR_JOB[:-1]} runs the same pin as the runtime "
        f"({runtime_pin}), so the compatibility floor is not being executed"
    )

    borrowed = [number for number, token in references if token == floor_pin]
    assert borrowed == [floor_line], (
        f"release contract: the floor pin {floor_pin} also appears at .gitlab-ci.yml line(s) "
        f"{[number for number in borrowed if number != floor_line]}; it belongs to "
        f"{FLOOR_JOB[:-1]} (line {floor_line}) alone, and every other job mirrors the runtime."
    )
    stray = [
        (number, token)
        for number, token in references
        if token not in (runtime_pin, floor_pin)
    ]
    assert not stray, (
        f"release contract: {ci_path} carries Python pins that are neither the runtime pin "
        f"({runtime_pin}) nor the compatibility floor ({floor_pin}): "
        + ", ".join(f"line {number}: {token}" for number, token in stray)
        + ". Move every runtime-mirroring reference together with the Dockerfile."
    )

    project = _load_toml(root / "pyproject.toml")
    requires_python = project["project"]["requires-python"]
    floor_pinned = PYTHON_IMAGE_PIN.fullmatch(floor_pin)
    assert floor_pinned  # `_floor_python_pin` already required this; narrows the type here.
    floor_version = floor_pinned.group("version")
    floor_series = floor_version.rsplit(".", 1)[0]
    assert requires_python == f">={floor_series}", (
        f"release contract: pyproject.toml declares requires-python = '{requires_python}' "
        f"while {FLOOR_JOB[:-1]} executes {floor_version}; the lane exists to test the "
        f"declared floor, so the two move together"
    )
    assert _series(runtime_version) >= _series(floor_version), (
        f"release contract: the runtime interpreter {runtime_version} is older than the "
        f"declared floor {floor_version}"
    )

    # The version axes are excluded above; the codename is not. `compat_python_floor` runs a
    # support policy, and a codename encodes nothing about it -- so the lane follows the
    # runtime's distro, as its own comment block in `.gitlab-ci.yml` states. Without this the
    # only enforcement is that someone remembers on a hop that touches nothing else about the
    # lane, which leaves the floor lane on an end-of-life Debian: the exact condition #59
    # exists to end, reintroduced on the one job nobody rebuilds locally.
    assert floor_pinned.group("codename") == runtime_pinned.group("codename"), (
        f"release contract: {FLOOR_JOB[:-1]} (.gitlab-ci.yml line {floor_line}) runs "
        f"{floor_pin} while the Dockerfile's PYTHON_IMAGE is built on "
        f"{runtime_pinned.group('codename')}. The floor lane pins an interpreter series, not "
        f"a distribution: its minor and patch keep their own schedules, but its codename "
        f"moves with the runtime so the lane cannot be left behind on an EOL Debian."
    )



def test_apt_suites_match_the_base_image_codename() -> None:
    """The three snapshot suites name the same Debian release `PYTHON_IMAGE` is built on.

    These are two independent literals describing one distribution, and they disagree
    silently. Pointing a bookworm base at trixie suites does not fail the build: APT installs
    the named packages from the frozen archive perfectly happily, and the result is a mixed
    image nothing downstream flags. The reverse -- a base bumped to a new codename with the
    suites left behind -- is the one #59 had to make by hand, and it is the more likely
    direction, since the base pin moves on a security cadence and the suites move only on a
    distro migration.

    The `apt-get indextargets` assertion in the `Dockerfile` is not this check: it governs
    where indexes come from (every URI under `snapshot.debian.org`), not which release they
    describe. Both are needed.
    """
    root = Path(__file__).resolve().parents[1]
    _, pinned = _runtime_python_match(root)
    codename = pinned.group("codename")

    dockerfile = root / "Dockerfile"
    suites = DOCKERFILE_APT_SUITE.findall(dockerfile.read_text(encoding="utf-8"))
    assert len(suites) == 3, (
        f"release contract: {dockerfile} configures {len(suites)} snapshot.debian.org APT "
        f"suite(s), expected 3 (<codename>, -updates, -security)"
    )
    assert [suite for suite, _ in suites] == [codename] * 3, (
        f"release contract: {dockerfile} pins a {codename} base image but configures APT "
        + ", ".join(f"{suite}{qualifier}" for suite, qualifier in suites)
        + ". The base image and the snapshot suites describe one Debian release and move "
        "together; a mismatch installs packages from a different release than the base was "
        "built on, and no build step fails on it."
    )
    assert sorted(qualifier for _, qualifier in suites) == ["", "-security", "-updates"], (
        f"release contract: {dockerfile} must configure exactly {codename}, "
        f"{codename}-updates and {codename}-security; the three are archived independently "
        f"and dropping one silently drops a class of update"
    )


def test_reference_sbom_records_the_runtime_interpreter() -> None:
    """`sbom/sbom.spdx.json` describes the interpreter `PYTHON_IMAGE` actually ships.

    Step 3 of the update checklist in `docs/DEPENDENCY_MANAGEMENT.md` says to regenerate the
    SBOM whenever the dependency footprint moves, and nothing enforced it: `header_guard.py`
    skips `sbom/`, and the document is not read by any other check. The artifact went stale
    across a full runtime hop and stayed silent (#73), which is the most misleading direction
    for it to drift in -- it advertised an interpreter the image no longer shipped.

    Scoped to the interpreter rather than the whole document on purpose. A byte-for-byte
    comparison is not available here: regeneration needs a Docker daemon, and Syft stamps a
    per-run `documentNamespace` and timestamp, so the document is not reproducible from the
    tree. The interpreter version is the one field that moves on every pin bump and can be
    derived from a file this test can read, which is what makes it a mechanical check rather
    than a remembered step -- the same purpose as the two tests either side of it.

    The release path is deliberately unaffected: `sbom_publish` stages the smoke-tested
    image's own bytes and `publish_release()` copies exactly those, so this governs the
    checked-in reference artifact only.
    """
    root = Path(__file__).resolve().parents[1]
    _, runtime_version = _runtime_python_pin(root)
    recorded = _sbom_package_versions(root, SBOM_PYTHON_PACKAGE)

    assert recorded, (
        f"release contract: {SBOM_PATH} records no '{SBOM_PYTHON_PACKAGE}' package, so it "
        f"does not describe this image at all; regenerate it with scripts/generate-sbom.sh"
    )
    assert set(recorded) == {runtime_version}, (
        f"release contract: {SBOM_PATH} records {SBOM_PYTHON_PACKAGE} "
        + ", ".join(sorted(set(recorded)))
        + f" while the Dockerfile's PYTHON_IMAGE ships {runtime_version}. Regenerate the SBOM "
        "from the built image in the same change as the pin move: "
        "scripts/generate-sbom.sh <image-ref> sbom/sbom.spdx.json <source-name>"
    )


def test_reference_sbom_records_the_base_image_distro() -> None:
    """`sbom/sbom.spdx.json` describes the Debian release `PYTHON_IMAGE` is built on.

    The interpreter check above cannot see this MR's own class of drift. #59 is a distro-only
    hop -- `PYTHON_VERSION` stays 3.14.7 by design -- so a reference SBOM left on the bookworm
    image would satisfy it verbatim while describing 125 packages the image no longer ships.
    That is the same asymmetry `test_apt_suites_match_the_base_image_codename` exists for: the
    base pin moves on a security cadence and the codename moves only on a migration, so the
    codename is the axis that goes stale without anything downstream noticing.

    Read off the deb purls' `distro` qualifier, which Syft derives from the image's own
    /etc/os-release rather than from anything this repository states -- so the artifact is
    evidence about the built image, not a restatement of the pin. The codename-to-major hop
    goes through `DEBIAN_RELEASES`; an unknown codename fails here rather than passing
    vacuously.
    """
    root = Path(__file__).resolve().parents[1]
    _, pinned = _runtime_python_match(root)
    codename = pinned.group("codename")

    expected = DEBIAN_RELEASES.get(codename)
    assert expected is not None, (
        f"release contract: PYTHON_IMAGE is built on Debian '{codename}', which "
        f"DEBIAN_RELEASES in {Path(__file__).name} does not know. Add "
        f"'{codename}': '<major>' to that table so the reference SBOM can be checked against "
        f"the pin; an unmapped codename would otherwise skip the check silently."
    )

    found = _sbom_debian_releases(root)
    assert found, (
        f"release contract: {SBOM_PATH} records no Debian packages at all, so it does not "
        f"describe this image; regenerate it with scripts/generate-sbom.sh"
    )
    unexpected = {release: names for release, names in found.items() if release != expected}
    assert not unexpected, (
        f"release contract: {SBOM_PATH} records deb packages built for Debian "
        + ", ".join(
            f"{release} ({len(names)} package(s), e.g. {sorted(names)[0]})"
            for release, names in sorted(unexpected.items())
        )
        + f" while PYTHON_IMAGE is built on {codename} (Debian {expected}). Regenerate the "
        "SBOM from the built image in the same change as the base move: "
        "scripts/generate-sbom.sh <image-ref> sbom/sbom.spdx.json <source-name>"
    )


def test_python_exceptions_are_scoped_to_the_runtime_interpreter() -> None:
    """A `python` exception left at the previous interpreter version matches nothing.

    An exception matching no finding is a hard failure in the release evaluator, and neither
    the image build nor the scan runs on a merge request -- so without this check a runtime
    hop that forgot to re-scope a surviving entry goes green, merges, and then breaks the
    default branch. The interpreter version is read out of `PYTHON_IMAGE` so the two cannot
    drift.
    """
    root = Path(__file__).resolve().parents[1]
    _, runtime_version = _runtime_python_pin(root)
    exceptions, _ = release_tools.load_exceptions(root / release_tools.EXCEPTIONS_PATH)

    # Scoped to `binary`, the type the CPython interpreter findings carry (they are matched on
    # /usr/local/bin/pythonX.Y). The exception schema carries `type` precisely to keep artifact
    # namespaces apart, so a `python` entry of some other type is a different artifact and is
    # not what this guard is about.
    stale = [
        entry
        for entry in exceptions
        if entry.package == "python"
        and entry.package_type == "binary"
        and entry.version != runtime_version
    ]
    assert not stale, (
        "release contract: "
        + ", ".join(f"{entry.package}@{entry.version}" for entry in stale)
        + f" is scoped to an interpreter the image no longer ships (now {runtime_version}). "
        "Re-scope surviving entries to the new version in the same commit as the pin move, "
        "or delete the ones the hop cleared."
    )
