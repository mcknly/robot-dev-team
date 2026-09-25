"""Robot Dev Team Project
File: tests/test_release_tools.py
Description: Regression tests for protected release validation and publication.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import subprocess
import tarfile
import traceback
import urllib.error
from pathlib import Path
from typing import Any, Sequence

import pytest

from scripts import release_tools

SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = f"sha256:{'a' * 64}"
TARGET_DIGEST = f"sha256:{'b' * 64}"
# `generate-sbom.sh --source-name "$image_ref"` puts the scanned image reference here, and both
# ends of the SBOM path check it to bind the document's content to the image it is filed under.
SBOM_NAME = f"registry.example/team/robot-dev-team:{SHA}"
# A Syft SPDX document embeds a per-run namespace and timestamp, so the release path has to pass
# the built bytes through unchanged rather than re-derive them.
SBOM_BYTES = json.dumps(
    {
        "spdxVersion": "SPDX-2.3",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": SBOM_NAME,
        "documentNamespace": "https://anchore.com/syft/image/robot-dev-team-c0ffee",
        "creationInfo": {"created": "2026-07-29T00:00:00Z"},
    },
    indent=2,
).encode()


def staged_sbom(digest: str = DIGEST, content: bytes = SBOM_BYTES) -> dict[tuple[str, str], bytes]:
    """The digest-addressed SBOM package the build side stages for a release to copy."""
    return {(release_tools.digest_package_version(digest), "sbom.spdx.json"): content}


REGISTRY_IMAGE = "registry.example/team/robot-dev-team"
PINNED_GRYPE = release_tools.TOOL_RELEASES["grype"]["version"]
REPO_ROOT = Path(__file__).resolve().parents[1]
# The shipped policy's exact bytes. `release_publish` recomputes the policy fingerprint from
# this file, so fixture evaluations have to be produced against the same bytes or the release
# correctly rejects them as evidence from a different policy.
EXCEPTIONS_PATH = REPO_ROOT / release_tools.EXCEPTIONS_PATH
EXCEPTIONS_RAW = EXCEPTIONS_PATH.read_bytes()


def scan_match(
    identifier: str,
    *,
    severity: str = "High",
    fix_state: str = "fixed",
    package: str = "python",
    version: str = "3.12.13",
    package_type: str = "binary",
) -> dict[str, Any]:
    return {
        "vulnerability": {
            "id": identifier,
            "severity": severity,
            "fix": {"state": fix_state, "versions": ["3.14.6"] if fix_state == "fixed" else []},
        },
        "artifact": {"name": package, "version": version, "type": package_type},
    }


def scan_report(
    *,
    digest: str = DIGEST,
    repository: str = REGISTRY_IMAGE,
    matches: Sequence[dict[str, Any]] = (),
    built: str | None = None,
    grype_version: str | None = None,
    user_input: str | None = None,
    distro: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A Grype report shaped like the real thing, reduced to the fields the policy reads."""
    when = built or (
        (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return {
        "matches": list(matches),
        "source": {
            "type": "image",
            "target": {
                "userInput": user_input if user_input is not None else f"{repository}@{digest}",
                "manifestDigest": digest,
                "repoDigests": [f"{repository}@{digest}"],
                "tags": [],
            },
        },
        "distro": distro if distro is not None else {"name": "debian", "version": "13.6"},
        "descriptor": {
            "name": "grype",
            "version": grype_version or PINNED_GRYPE,
            "db": {
                "status": {
                    "schemaVersion": "v6.1.9",
                    "from": "https://grype.anchore.io/databases/v6/...",
                    "built": when,
                    "valid": True,
                }
            },
        },
    }


def evaluate(
    report: dict[str, Any] | None = None,
    *,
    exceptions: Sequence[release_tools.ScanException] = (),
    exceptions_raw: bytes = EXCEPTIONS_RAW,
    digest: str = DIGEST,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    document = report if report is not None else scan_report()
    return release_tools.evaluate_scan(
        document,
        digest=digest,
        repository=REGISTRY_IMAGE,
        commit=SHA,
        exceptions=exceptions,
        exceptions_raw=exceptions_raw,
        now=now or dt.datetime.now(dt.timezone.utc),
        report_sha256=hashlib.sha256(json.dumps(document).encode()).hexdigest(),
    )


def exception_entry(**overrides: Any) -> dict[str, Any]:
    entry = {
        "id": "CVE-2026-0001",
        "package": "python",
        "version": "3.12.13",
        "type": "binary",
        "owner": "@cavin",
        "rationale": "No fix on the branch we ship; migration tracked.",
        "expires": (dt.date.today() + dt.timedelta(days=30)).isoformat(),
        "tracking_issue": "#66",
    }
    entry.update(overrides)
    return {key: value for key, value in entry.items() if value is not None}


def parse_one(**overrides: Any) -> release_tools.ScanException:
    return release_tools.parse_exception_entry(exception_entry(**overrides), 1)


NO_EXCEPTIONS = Path("/nonexistent/vulnerability-exceptions.yaml")


def staged_scan(
    digest: str = DIGEST,
    *,
    report: dict[str, Any] | None = None,
    evaluation: dict[str, Any] | None = None,
    exceptions_raw: bytes = EXCEPTIONS_RAW,
) -> dict[tuple[str, str], bytes]:
    """The digest-addressed scan evidence the security stage stages for a release to verify."""
    document = report if report is not None else scan_report(digest=digest)
    record = (
        evaluation
        if evaluation is not None
        else evaluate(document, digest=digest, exceptions_raw=exceptions_raw)
    )
    version = release_tools.digest_package_version(digest)
    return {
        (version, release_tools.SCAN_REPORT_FILENAME): json.dumps(document).encode(),
        (version, release_tools.SCAN_EVALUATION_FILENAME): (
            json.dumps(record, indent=2, sort_keys=True) + "\n"
        ).encode(),
    }


class FakeApi:
    def __init__(
        self,
        files: dict[tuple[str, str], bytes] | None = None,
        *,
        job_user: dict[str, Any] | None = None,
        releases: dict[str, dict[str, Any]] | None = None,
        release_error: str | None = None,
        sbom_files: dict[tuple[str, str], bytes] | None = None,
        scan_files: dict[tuple[str, str], bytes] | None = None,
    ) -> None:
        self.files = files or {}
        # A separate store, like the separate Generic Package: entries here must never reach
        # release enumeration.
        self.sbom_files = sbom_files or {}
        # Passing scan evidence is the default so the SBOM and alias tests keep testing what
        # they are about. The gate's own fail-closed cases pass `scan_files={}` explicitly.
        self.scan_files = staged_scan() if scan_files is None else scan_files
        self.uploads: list[tuple[str, str, str, bytes]] = []
        self.releases = releases or {}
        self.release_error = release_error
        self.release_calls: list[dict[str, Any]] = []
        self.job_user = job_user or {
            "id": 42,
            "username": "release-operator",
        }

    def _store(self, package_name: str) -> dict[tuple[str, str], bytes]:
        if package_name == release_tools.PACKAGE_NAME:
            return self.files
        if package_name == release_tools.SBOM_PACKAGE_NAME:
            return self.sbom_files
        if package_name == release_tools.SCAN_PACKAGE_NAME:
            return self.scan_files
        raise AssertionError(f"unexpected package: {package_name}")

    def package_file(
        self,
        version: str,
        filename: str,
        *,
        package_name: str = release_tools.PACKAGE_NAME,
    ) -> bytes | None:
        return self._store(package_name).get((version, filename))

    def upload_package_file(
        self,
        version: str,
        filename: str,
        content: bytes,
        *,
        package_name: str = release_tools.PACKAGE_NAME,
    ) -> None:
        existing = self.package_file(version, filename, package_name=package_name)
        if existing is not None and existing != content:
            raise release_tools.ReleaseError("conflicting content")
        if existing is None:
            self._store(package_name)[(version, filename)] = content
            self.uploads.append((package_name, version, filename, content))

    def package_versions(self) -> list[str]:
        return sorted({version for version, _ in self.files})

    def asset_url(
        self,
        version: str,
        filename: str,
        *,
        package_name: str = release_tools.PACKAGE_NAME,
    ) -> str:
        if package_name == release_tools.PACKAGE_NAME:
            return f"https://gitlab.example/api/{version}/{filename}"
        return f"https://gitlab.example/api/{package_name}/{version}/{filename}"

    def release(self, tag: str) -> dict[str, Any] | None:
        if self.release_error is not None:
            raise release_tools.ReleaseError(self.release_error)
        return self.releases.get(tag)

    def create_release(
        self,
        *,
        tag: str,
        name: str,
        description: str,
        links: Any = (),
    ) -> dict[str, Any]:
        self.release_calls.append(
            {
                "action": "create",
                "tag": tag,
                "name": name,
                "description": description,
                "links": [dict(link) for link in links],
            }
        )
        return {}

    def update_release(self, *, tag: str, name: str, description: str) -> dict[str, Any]:
        self.release_calls.append(
            {"action": "update", "tag": tag, "name": name, "description": description}
        )
        return {}

    def add_release_link(self, *, tag: str, link: dict[str, str]) -> dict[str, Any]:
        self.release_calls.append({"action": "link", "tag": tag, "link": dict(link)})
        return {}

    def current_job_user(self) -> dict[str, Any]:
        return self.job_user


def release_record(*links: tuple[str, str]) -> dict[str, Any]:
    return {"assets": {"links": [{"name": name, "url": url} for name, url in links]}}


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: Any) -> bool:
        return False


def fake_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[tuple[str, str], bytes | int],
) -> list[tuple[str, str, bytes | None, dict[str, str]]]:
    """Serve canned API responses keyed by (method, path); an int value raises that status."""
    seen: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def urlopen(request: Any, timeout: int = 0) -> FakeResponse:
        path = request.full_url.split("/api/v4", 1)[-1]
        seen.append((request.get_method(), path, request.data, dict(request.headers)))
        try:
            outcome = responses[(request.get_method(), path)]
        except KeyError:  # pragma: no cover - a missing stub is a test bug
            raise AssertionError(f"unexpected API call: {request.get_method()} {path}") from None
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(
                request.full_url,
                outcome,
                "error",
                {},  # type: ignore[arg-type]
                io.BytesIO(b'{"message":"error"}'),
            )
        return FakeResponse(outcome)

    monkeypatch.setattr(release_tools.urllib.request, "urlopen", urlopen)
    return seen


def release_env(**overrides: str) -> dict[str, str]:
    values = {
        "CI_COMMIT_TAG": "v0.2.0",
        "CI_COMMIT_SHA": SHA,
        "CI_DEFAULT_BRANCH": "main",
        "CI_COMMIT_REF_PROTECTED": "true",
        "CI_REGISTRY": "registry.example",
        "CI_REGISTRY_IMAGE": "registry.example/team/robot-dev-team",
        "CI_REGISTRY_USER": "ci-user",
        "CI_REGISTRY_PASSWORD": "ci-password",
        "CI_API_V4_URL": "https://gitlab.example/api/v4",
        "CI_PROJECT_ID": "38",
        "CI_JOB_TOKEN": "job-token",
        "CI_PIPELINE_URL": "https://gitlab.example/pipelines/1",
        "CI_JOB_URL": "https://gitlab.example/jobs/2",
        "CI_PROJECT_URL": "https://gitlab.example/team/robot-dev-team",
    }
    values.update(overrides)
    return values


def dockerhub_probe_env(**overrides: str) -> dict[str, str]:
    values = release_env(
        CI_PIPELINE_SOURCE="push",
        CI_COMMIT_BRANCH="main",
        CI_PIPELINE_ID="187",
        IMAGE_PUBLISHED="true",
        IMAGE_DIGEST=DIGEST,
        DOCKERHUB_USERNAME="docker-user",
        DOCKERHUB_TOKEN="docker-token",
    )
    values.update(overrides)
    return values


def write_release_files(tmp_path: Path, version: str = "0.2.0") -> tuple[Path, Path]:
    pyproject = tmp_path / "pyproject.toml"
    changelog = tmp_path / "CHANGELOG.md"
    pyproject.write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    changelog.write_text(
        (
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            f"## [v{version}] - 2026-07-23\n\n"
            "### Features\n"
            "- Stable release support.\n\n"
            "## [v0.1.0] - 2025-10-22\n"
        ),
        encoding="utf-8",
    )
    return pyproject, changelog


def release_manifest(
    version: str,
    *,
    digest: str = DIGEST,
    source_commit: str = SHA,
    aliases: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "release_version": version,
        "git_tag": f"v{version}",
        "source_commit": source_commit,
        "image_digest": digest,
        "aliases": aliases or [],
    }


def write_yank_context(
    tmp_path: Path,
    *,
    version: str = "0.2.0",
    reason: str = "startup regression",
) -> Path:
    path = tmp_path / "release-context.json"
    path.write_text(
        json.dumps(
            {
                "operation": "yank",
                "yanked_version": version,
                "git_tag": f"v{version}-yank",
                "source_commit": SHA,
                "reason": reason,
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.2.0", (0, 2, 0)),
        ("10.20.30", (10, 20, 30)),
    ],
)
def test_version_parses_stable_semver(value: str, expected: tuple[int, int, int]) -> None:
    version = release_tools.Version.parse(value)
    assert (version.major, version.minor, version.patch) == expected


@pytest.mark.parametrize("value", ["v0.2.0", "01.2.3", "1.2", "1.2.3-rc.1", "1.2.3+meta"])
def test_version_rejects_nonstable_project_versions(value: str) -> None:
    with pytest.raises(release_tools.ReleaseError):
        release_tools.Version.parse(value)


@pytest.mark.parametrize("tag", ["0.2.0", "v01.2.3", "v1.2.3-rc.1", "version-1.2.3"])
def test_release_tag_requires_exact_stable_form(tag: str) -> None:
    with pytest.raises(release_tools.ReleaseError):
        release_tools.Version.from_tag(tag)


def test_moving_aliases_do_not_regress_on_backport() -> None:
    released = [
        release_tools.Version.parse("1.2.4"),
        release_tools.Version.parse("1.3.0"),
        release_tools.Version.parse("2.0.0"),
    ]
    assert release_tools.desired_moving_aliases(release_tools.Version.parse("1.2.5"), released) == (
        "1.2",
    )
    assert release_tools.desired_moving_aliases(release_tools.Version.parse("1.3.1"), released) == (
        "1.3",
        "1",
    )
    assert release_tools.desired_moving_aliases(release_tools.Version.parse("2.0.1"), released) == (
        "2.0",
        "2",
        "latest",
    )


def test_changelog_excerpt_stops_at_next_release(tmp_path: Path) -> None:
    _, changelog = write_release_files(tmp_path)
    excerpt, release_date = release_tools.changelog_excerpt(
        changelog, release_tools.Version.parse("0.2.0")
    )
    assert release_date == "2026-07-23"
    assert "Stable release support." in excerpt
    assert "v0.1.0" not in excerpt


def test_changelog_requires_matching_dated_section(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## [v0.2.0]\n", encoding="utf-8")
    with pytest.raises(release_tools.ReleaseError, match="dated"):
        release_tools.changelog_excerpt(changelog, release_tools.Version.parse("0.2.0"))


def test_validate_release_writes_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pyproject, changelog = write_release_files(tmp_path)
    output = tmp_path / "release-context.json"

    def fake_run(arguments: tuple[str, ...], **_: Any) -> subprocess.CompletedProcess[str]:
        stdout = "tag\n" if arguments[1:3] == ("cat-file", "-t") else "ok\n"
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(release_tools, "run", fake_run)
    monkeypatch.setattr(
        release_tools.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    context = release_tools.validate_release(
        pyproject_path=pyproject,
        changelog_path=changelog,
        output_path=output,
        environ=release_env(),
    )

    assert context["release_version"] == "0.2.0"
    assert context["source_commit"] == SHA
    assert json.loads(output.read_text()) == context


def test_validate_yank_tag_uses_annotation_as_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pyproject, changelog = write_release_files(tmp_path)
    output = tmp_path / "release-context.json"

    def fake_run(arguments: tuple[str, ...], **_: Any) -> subprocess.CompletedProcess[str]:
        if arguments[1:3] == ("cat-file", "-t"):
            stdout = "tag\n"
        elif arguments[1] == "for-each-ref":
            stdout = "Withdraw v0.2.0: startup regression\n"
        else:
            stdout = "ok\n"
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(release_tools, "run", fake_run)
    monkeypatch.setattr(
        release_tools.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    context = release_tools.validate_release(
        pyproject_path=pyproject,
        changelog_path=changelog,
        output_path=output,
        environ=release_env(CI_COMMIT_TAG="v0.2.0-yank"),
    )

    assert context["operation"] == "yank"
    assert context["yanked_version"] == "0.2.0"
    assert context["reason"] == "Withdraw v0.2.0: startup regression"


def test_validate_release_rejects_unprotected_tag(tmp_path: Path) -> None:
    pyproject, changelog = write_release_files(tmp_path)
    with pytest.raises(release_tools.ReleaseError, match="protected"):
        release_tools.validate_release(
            pyproject_path=pyproject,
            changelog_path=changelog,
            output_path=tmp_path / "context.json",
            environ=release_env(CI_COMMIT_REF_PROTECTED="false"),
        )


def test_validate_release_rejects_version_disagreement(tmp_path: Path) -> None:
    pyproject, changelog = write_release_files(tmp_path, "0.3.0")
    with pytest.raises(release_tools.ReleaseError, match="disagrees"):
        release_tools.validate_release(
            pyproject_path=pyproject,
            changelog_path=changelog,
            output_path=tmp_path / "context.json",
            environ=release_env(),
        )


def test_validate_release_rejects_lightweight_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject, changelog = write_release_files(tmp_path)
    monkeypatch.setattr(
        release_tools,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "commit\n", ""),
    )
    with pytest.raises(release_tools.ReleaseError, match="annotated"):
        release_tools.validate_release(
            pyproject_path=pyproject,
            changelog_path=changelog,
            output_path=tmp_path / "context.json",
            environ=release_env(),
        )


def test_validate_release_rejects_commit_outside_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject, changelog = write_release_files(tmp_path)
    monkeypatch.setattr(
        release_tools,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "tag\n", ""),
    )
    monkeypatch.setattr(
        release_tools.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", ""),
    )
    with pytest.raises(release_tools.ReleaseError, match="not contained"):
        release_tools.validate_release(
            pyproject_path=pyproject,
            changelog_path=changelog,
            output_path=tmp_path / "context.json",
            environ=release_env(),
        )


def test_apply_alias_refuses_immutable_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_tools, "crane_digest", lambda *args: TARGET_DIGEST)
    with pytest.raises(release_tools.ReleaseError, match="immutable release alias"):
        release_tools.apply_alias(
            Path("crane"),
            "registry.example/team/image",
            DIGEST,
            "0.2.0",
            immutable=True,
        )


def test_apply_alias_creates_only_after_explicit_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups = iter(
        (
            release_tools.ReleaseError("MANIFEST_UNKNOWN: manifest unknown"),
            DIGEST,
        )
    )

    def fake_digest(*_: Any) -> str:
        result = next(lookups)
        if isinstance(result, Exception):
            raise result
        return result

    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(release_tools, "crane_digest", fake_digest)
    monkeypatch.setattr(
        release_tools,
        "run",
        lambda arguments, **kwargs: (
            commands.append(tuple(arguments))
            or subprocess.CompletedProcess(arguments, 0, "", "")
        ),
    )

    release_tools.apply_alias(
        Path("crane"),
        "registry.example/team/image",
        DIGEST,
        "0.2.0",
        immutable=True,
    )

    assert commands == [
        (
            "crane",
            "tag",
            f"registry.example/team/image@{DIGEST}",
            "0.2.0",
        )
    ]


def test_apply_alias_aborts_on_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release_tools,
        "crane_digest",
        lambda *args: (_ for _ in ()).throw(
            release_tools.ReleaseError("registry lookup failed: TLS handshake timeout")
        ),
    )
    monkeypatch.setattr(
        release_tools,
        "run",
        lambda *args, **kwargs: pytest.fail("tag write must not be attempted"),
    )

    with pytest.raises(release_tools.ReleaseError, match="TLS handshake"):
        release_tools.apply_alias(
            Path("crane"),
            "registry.example/team/image",
            DIGEST,
            "0.2.0",
            immutable=True,
        )


def test_dockerhub_probe_copies_and_records_matching_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logins: list[tuple[str, str, str]] = []
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        release_tools,
        "crane_login",
        lambda _crane, registry, username, password: logins.append(
            (registry, username, password)
        ),
    )
    monkeypatch.setattr(release_tools, "crane_digest", lambda *_args: DIGEST)
    monkeypatch.setattr(release_tools, "crane_digest_if_exists", lambda *_args: None)
    monkeypatch.setattr(
        release_tools,
        "run",
        lambda arguments, **_kwargs: (
            commands.append(tuple(arguments))
            or subprocess.CompletedProcess(arguments, 0, "", "")
        ),
    )

    evidence = release_tools.probe_dockerhub_credentials(
        crane=Path("crane"),
        artifacts_dir=tmp_path,
        environ=dockerhub_probe_env(),
    )

    source = f"{REGISTRY_IMAGE}@{DIGEST}"
    destination = "docker.io/mcknly/robot-dev-team:ci-credential-probe-187"
    assert logins == [
        ("registry.example", "ci-user", "ci-password"),
        ("index.docker.io", "docker-user", "docker-token"),
    ]
    assert commands == [("crane", "copy", source, destination)]
    assert evidence["source_reference"] == source
    assert evidence["destination_reference"] == destination
    assert evidence["source_digest"] == evidence["destination_digest"] == DIGEST
    assert evidence["cleanup_required"] is True
    assert "docker-token" not in json.dumps(evidence)
    assert json.loads((tmp_path / "dockerhub-probe.json").read_text()) == evidence


def test_dockerhub_probe_rejects_unprotected_pipeline_before_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        release_tools,
        "crane_login",
        lambda *_args: pytest.fail("credentials must not be used on an unprotected ref"),
    )

    with pytest.raises(release_tools.ReleaseError, match="protected default-branch push"):
        release_tools.probe_dockerhub_credentials(
            crane=Path("crane"),
            artifacts_dir=tmp_path,
            environ=dockerhub_probe_env(CI_COMMIT_REF_PROTECTED="false"),
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"IMAGE_PUBLISHED": "false"}, "not published"),
        ({"IMAGE_DIGEST": "sha256:not-a-digest"}, "not a manifest digest"),
    ],
)
def test_dockerhub_probe_rejects_unqualified_source_before_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
    message: str,
) -> None:
    monkeypatch.setattr(
        release_tools,
        "crane_login",
        lambda *_args: pytest.fail("credentials must not be used without a published digest"),
    )

    with pytest.raises(release_tools.ReleaseError, match=message):
        release_tools.probe_dockerhub_credentials(
            crane=Path("crane"),
            artifacts_dir=tmp_path,
            environ=dockerhub_probe_env(**overrides),
        )


@pytest.mark.parametrize("existing_digest", [DIGEST, TARGET_DIGEST])
def test_dockerhub_probe_refuses_any_existing_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_digest: str
) -> None:
    """A matching tag is rejected too: success must always come from a real write."""
    monkeypatch.setattr(release_tools, "crane_login", lambda *_args: None)
    monkeypatch.setattr(release_tools, "crane_digest", lambda *_args: DIGEST)
    monkeypatch.setattr(
        release_tools,
        "crane_digest_if_exists",
        lambda *_args: existing_digest,
    )
    monkeypatch.setattr(
        release_tools,
        "run",
        lambda *_args, **_kwargs: pytest.fail("an existing probe tag must not be written"),
    )

    with pytest.raises(release_tools.ReleaseError, match="already exists"):
        release_tools.probe_dockerhub_credentials(
            crane=Path("crane"),
            artifacts_dir=tmp_path,
            environ=dockerhub_probe_env(),
        )
    assert not (tmp_path / "dockerhub-probe.json").exists()


def test_dockerhub_probe_rejects_destination_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digests = iter((DIGEST, TARGET_DIGEST))
    monkeypatch.setattr(release_tools, "crane_login", lambda *_args: None)
    monkeypatch.setattr(release_tools, "crane_digest", lambda *_args: next(digests))
    monkeypatch.setattr(release_tools, "crane_digest_if_exists", lambda *_args: None)
    monkeypatch.setattr(
        release_tools,
        "run",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(arguments, 0, "", ""),
    )

    with pytest.raises(release_tools.ReleaseError, match="digest mismatch"):
        release_tools.probe_dockerhub_credentials(
            crane=Path("crane"),
            artifacts_dir=tmp_path,
            environ=dockerhub_probe_env(),
        )


def test_publish_promotes_digest_and_uploads_durable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi(sbom_files=staged_sbom())
    context = {
        "operation": "publish",
        "release_version": "0.2.0",
        "git_tag": "v0.2.0",
        "source_commit": SHA,
        "changelog": "## [v0.2.0] - 2026-07-23\n\n- Release.\n",
    }
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")
    aliases: list[str] = []

    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    monkeypatch.setattr(release_tools, "crane_login", lambda *args: None)
    monkeypatch.setattr(release_tools, "crane_digest", lambda *args: DIGEST)

    def fake_apply(
        crane: Path,
        repository: str,
        digest: str,
        alias: str,
        *,
        immutable: bool,
    ) -> dict[str, str]:
        aliases.append(alias)
        return {
            "name": alias,
            "reference": f"{repository}:{alias}",
            "digest": digest,
            "kind": "immutable" if immutable else "moving",
        }

    monkeypatch.setattr(release_tools, "apply_alias", fake_apply)

    manifest = release_tools.publish_release(
        context_path=context_path,
        artifacts_dir=tmp_path / "artifacts",
        crane=Path("crane"),
        exceptions_path=EXCEPTIONS_PATH,
        environ=release_env(),
    )

    assert aliases == ["0.2.0", "0.2", "0", "latest"]
    assert manifest["image_digest"] == DIGEST
    uploaded = {(version, filename) for _, version, filename, _ in api.uploads}
    assert ("0.2.0", "release-manifest.json") in uploaded
    assert ("0.2.0", "changelog.md") in uploaded
    # The version-scoped copy is the exact staged bytes, and it is written before the release
    # record links it.
    assert api.files[("0.2.0", "sbom.spdx.json")] == SBOM_BYTES
    assert (tmp_path / "artifacts" / "sbom.spdx.json").read_bytes() == SBOM_BYTES
    # The release record is written through the same job-token API client as the durable
    # package files: no CLI, and so no dependency on a git executable for project discovery.
    assert api.release_calls == [
        {
            "action": "create",
            "tag": "v0.2.0",
            "name": "Robot Dev Team v0.2.0",
            "description": "## [v0.2.0] - 2026-07-23\n\n- Release.\n",
            "links": [
                {
                    "name": "release-manifest.json",
                    "url": "https://gitlab.example/api/0.2.0/release-manifest.json",
                    "link_type": "other",
                },
                {
                    "name": "changelog.md",
                    "url": "https://gitlab.example/api/0.2.0/changelog.md",
                    "link_type": "other",
                },
                {
                    "name": "sbom.spdx.json",
                    "url": "https://gitlab.example/api/0.2.0/sbom.spdx.json",
                    "link_type": "other",
                },
                {
                    "name": "vulnerability-report.json",
                    "url": "https://gitlab.example/api/0.2.0/vulnerability-report.json",
                    "link_type": "other",
                },
                {
                    "name": "vulnerability-evaluation.json",
                    "url": "https://gitlab.example/api/0.2.0/vulnerability-evaluation.json",
                    "link_type": "other",
                },
            ],
        }
    ]


def test_publish_requires_api_context_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = {
        "operation": "publish",
        "release_version": "0.2.0",
        "git_tag": "v0.2.0",
        "source_commit": SHA,
        "changelog": "## [v0.2.0] - 2026-07-23\n\n- Release.\n",
    }
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")

    monkeypatch.setattr(
        release_tools,
        "crane_login",
        lambda *args: pytest.fail("registry login must not be attempted"),
    )
    environ = release_env()
    del environ["CI_PROJECT_ID"]

    with pytest.raises(release_tools.ReleaseError, match="CI_PROJECT_ID is required"):
        release_tools.publish_release(
            context_path=context_path,
            artifacts_dir=tmp_path / "artifacts",
            crane=Path("crane"),
            environ=environ,
        )


def test_publish_stops_when_the_release_record_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An API failure on the release read must abort before anything is mutated."""
    api = FakeApi(release_error="GitLab API GET /releases/v0.2.0 failed (500)")
    context = {
        "operation": "publish",
        "release_version": "0.2.0",
        "git_tag": "v0.2.0",
        "source_commit": SHA,
        "changelog": "## [v0.2.0] - 2026-07-23\n\n- Release.\n",
    }
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")

    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    monkeypatch.setattr(
        release_tools,
        "crane_login",
        lambda *args: pytest.fail("registry login must not be attempted"),
    )

    with pytest.raises(release_tools.ReleaseError, match="failed"):
        release_tools.publish_release(
            context_path=context_path,
            artifacts_dir=tmp_path / "artifacts",
            crane=Path("crane"),
            environ=release_env(),
        )

    assert api.uploads == []
    assert api.release_calls == []


def test_publish_rejects_unreadable_release_links_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present but unreadable asset payload must not be mistaken for "no links"."""
    api = FakeApi(releases={"v0.2.0": {"assets": {"links": [{"name": "changelog.md"}]}}})
    context = {
        "operation": "publish",
        "release_version": "0.2.0",
        "git_tag": "v0.2.0",
        "source_commit": SHA,
        "changelog": "## [v0.2.0] - 2026-07-23\n\n- Release.\n",
    }
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")

    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    monkeypatch.setattr(
        release_tools,
        "crane_login",
        lambda *args: pytest.fail("registry login must not be attempted"),
    )

    with pytest.raises(release_tools.ReleaseError, match="unreadable asset link"):
        release_tools.publish_release(
            context_path=context_path,
            artifacts_dir=tmp_path / "artifacts",
            crane=Path("crane"),
            environ=release_env(),
        )

    assert api.uploads == []
    assert api.release_calls == []


def test_publish_rejects_conflicting_release_links_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi(
        releases={
            "v0.2.0": release_record(
                ("changelog.md", "https://gitlab.example/somewhere-else/changelog.md")
            )
        }
    )
    context = {
        "operation": "publish",
        "release_version": "0.2.0",
        "git_tag": "v0.2.0",
        "source_commit": SHA,
        "changelog": "## [v0.2.0] - 2026-07-23\n\n- Release.\n",
    }
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")

    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    monkeypatch.setattr(
        release_tools,
        "crane_login",
        lambda *args: pytest.fail("registry login must not be attempted"),
    )

    with pytest.raises(release_tools.ReleaseError, match="different URL"):
        release_tools.publish_release(
            context_path=context_path,
            artifacts_dir=tmp_path / "artifacts",
            crane=Path("crane"),
            environ=release_env(),
        )

    assert api.uploads == []


def run_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api: FakeApi,
    *,
    digest: str = DIGEST,
    forbid_aliases: bool = False,
    exceptions_path: Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Drive publish_release with the registry mocked out, recording every alias write.

    `forbid_aliases` turns the first alias write into an immediate failure instead of a
    recorded one. The fail-closed tests need that rather than a post-hoc assertion on the
    returned list: on the failure path this helper raises before returning anything, so an
    alias moved out of order would otherwise go unobserved.
    """
    context = {
        "operation": "publish",
        "release_version": "0.2.0",
        "git_tag": "v0.2.0",
        "source_commit": SHA,
        "changelog": "## [v0.2.0] - 2026-07-23\n\n- Release.\n",
    }
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")
    aliases: list[str] = []

    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    monkeypatch.setattr(release_tools, "crane_login", lambda *args: None)
    monkeypatch.setattr(release_tools, "crane_digest", lambda *args: digest)

    def record_alias(
        crane: Path,
        repository: str,
        image_digest: str,
        alias: str,
        *,
        immutable: bool,
    ) -> dict[str, str]:
        if forbid_aliases:
            raise AssertionError(f"alias {alias} was written before the release could fail closed")
        aliases.append(alias)
        return {
            "name": alias,
            "reference": f"{repository}:{alias}",
            "digest": image_digest,
            "kind": "immutable" if immutable else "moving",
        }

    monkeypatch.setattr(release_tools, "apply_alias", record_alias)

    manifest = release_tools.publish_release(
        context_path=context_path,
        artifacts_dir=tmp_path / "artifacts",
        crane=Path("crane"),
        # Absolute, so the policy fingerprint is recomputed from the shipped file regardless of
        # the working directory pytest was started in.
        exceptions_path=exceptions_path or EXCEPTIONS_PATH,
        environ=release_env(),
    )
    return manifest, aliases


def test_publish_stops_when_the_digest_has_no_staged_sbom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release of a digest built before sbom_publish existed fails with nothing mutated."""
    api = FakeApi()

    with pytest.raises(release_tools.ReleaseError, match="no durable SBOM is published"):
        run_publish(tmp_path, monkeypatch, api, forbid_aliases=True)

    assert api.uploads == []
    assert api.release_calls == []


def test_publish_stops_when_the_staged_sbom_is_not_an_spdx_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi(sbom_files=staged_sbom(content=b'{"spdxVersion": "SPDX-2.2"}'))

    with pytest.raises(release_tools.ReleaseError, match="not an SPDX-2.3 document"):
        run_publish(tmp_path, monkeypatch, api, forbid_aliases=True)

    assert api.uploads == []
    assert api.release_calls == []


def test_publish_stops_when_the_staged_sbom_describes_another_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The package path is not the only binding: the document must name the released image.

    The manual-recovery upload documented in docs/RELEASING.md is unconstrained, so a document
    staged under the right digest is not by itself evidence that it describes that image.
    """
    foreign = json.dumps(
        {
            "spdxVersion": "SPDX-2.3",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": "registry.example/team/robot-dev-team:deadbeef",
        }
    ).encode()
    api = FakeApi(sbom_files=staged_sbom(content=foreign))

    with pytest.raises(release_tools.ReleaseError, match="describes 'registry.*deadbeef'"):
        run_publish(tmp_path, monkeypatch, api, forbid_aliases=True)

    assert api.uploads == []
    assert api.release_calls == []


def test_publish_accepts_a_manually_staged_sbom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery path: the release only requires the file to be present under the digest.

    Nothing records which run produced it, so an operator-uploaded SBOM for a digest whose
    pipeline predates `sbom_publish` completes the release exactly like a staged one.
    """
    # Byte-distinct from the pipeline document, but it still has to name the released image:
    # the recovery runbook's rescan passes --source-name for exactly that reason.
    recovered = json.dumps(
        {"spdxVersion": "SPDX-2.3", "SPDXID": "SPDXRef-DOCUMENT", "name": SBOM_NAME}
    ).encode()
    api = FakeApi(sbom_files=staged_sbom(content=recovered))

    manifest, aliases = run_publish(tmp_path, monkeypatch, api)

    assert manifest["image_digest"] == DIGEST
    assert aliases == ["0.2.0", "0.2", "0", "latest"]
    assert api.files[("0.2.0", "sbom.spdx.json")] == recovered


def test_publish_retry_reuses_the_existing_version_scoped_sbom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi(
        files={("0.2.0", "sbom.spdx.json"): SBOM_BYTES},
        sbom_files=staged_sbom(),
    )

    run_publish(tmp_path, monkeypatch, api)

    assert ("0.2.0", "sbom.spdx.json") not in {
        (version, filename) for _, version, filename, _ in api.uploads
    }
    assert api.files[("0.2.0", "sbom.spdx.json")] == SBOM_BYTES


def sbom_env(**overrides: str) -> dict[str, str]:
    """The build-side environment `publish_sbom` reads. An empty override omits the variable."""
    values = {
        "IMAGE_PUBLISHED": "true",
        "IMAGE_DIGEST": DIGEST,
        "CI_REGISTRY_IMAGE": "registry.example/team/robot-dev-team",
        "CI_COMMIT_SHA": SHA,
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value}


def test_publish_sbom_stages_exact_bytes_under_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi()
    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    sbom_path = tmp_path / "sbom.spdx.json"
    sbom_path.write_bytes(SBOM_BYTES)

    version = release_tools.publish_sbom(sbom_path=sbom_path, environ=sbom_env())

    assert version == f"sha256-{'a' * 64}"
    assert api.uploads == [
        (release_tools.SBOM_PACKAGE_NAME, version, "sbom.spdx.json", SBOM_BYTES)
    ]
    # The staging upload must not touch the release package, whose version list is what
    # release enumeration walks.
    assert api.files == {}


def test_publish_sbom_is_idempotent_and_fails_closed_on_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi()
    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    sbom_path = tmp_path / "sbom.spdx.json"
    sbom_path.write_bytes(SBOM_BYTES)
    environ = sbom_env()

    version = release_tools.publish_sbom(sbom_path=sbom_path, environ=environ)
    assert api.sbom_files[(version, "sbom.spdx.json")] == SBOM_BYTES
    assert len(api.uploads) == 1

    # A retried job re-uploads the identical artifact bytes: a no-op, not a conflict.
    release_tools.publish_sbom(sbom_path=sbom_path, environ=environ)
    assert len(api.uploads) == 1

    # A different document for the same digest is unexplained state.
    sbom_path.write_bytes(
        json.dumps(
            {"spdxVersion": "SPDX-2.3", "SPDXID": "SPDXRef-DOCUMENT", "name": SBOM_NAME, "x": 1}
        ).encode()
    )
    with pytest.raises(release_tools.ReleaseError, match="conflicting content"):
        release_tools.publish_sbom(sbom_path=sbom_path, environ=environ)


@pytest.mark.parametrize(
    ("environ", "content", "message"),
    [
        (sbom_env(IMAGE_PUBLISHED="false"), SBOM_BYTES, "was not published"),
        (sbom_env(IMAGE_PUBLISHED=""), SBOM_BYTES, "IMAGE_PUBLISHED is required"),
        (sbom_env(IMAGE_DIGEST=""), SBOM_BYTES, "IMAGE_DIGEST is required"),
        (sbom_env(CI_COMMIT_SHA=""), SBOM_BYTES, "CI_COMMIT_SHA is required"),
        (
            sbom_env(IMAGE_DIGEST="sha256:not-a-digest"),
            SBOM_BYTES,
            "not a valid image manifest digest",
        ),
        (
            sbom_env(IMAGE_DIGEST=DIGEST.replace(":", "-")),
            SBOM_BYTES,
            "not a valid image manifest digest",
        ),
        (sbom_env(), b"not json", "not valid JSON"),
        (sbom_env(), b"[]", "must be a JSON object"),
        (
            sbom_env(),
            b'{"spdxVersion": "SPDX-2.3"}',
            "carries no document SPDXID",
        ),
        # The document must name the image this pipeline built, not merely be well-formed SPDX.
        (
            sbom_env(),
            json.dumps(
                {
                    "spdxVersion": "SPDX-2.3",
                    "SPDXID": "SPDXRef-DOCUMENT",
                    "name": "robot-dev-team-app",
                }
            ).encode(),
            "describes 'robot-dev-team-app'",
        ),
    ],
)
def test_publish_sbom_rejects_unusable_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
    content: bytes,
    message: str,
) -> None:
    api = FakeApi()
    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    sbom_path = tmp_path / "sbom.spdx.json"
    sbom_path.write_bytes(content)

    with pytest.raises(release_tools.ReleaseError, match=message):
        release_tools.publish_sbom(sbom_path=sbom_path, environ=environ)

    assert api.uploads == []


def test_publish_sbom_reports_a_missing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi()
    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))

    with pytest.raises(release_tools.ReleaseError, match="could not read the built SBOM"):
        release_tools.publish_sbom(
            sbom_path=tmp_path / "missing.spdx.json",
            environ=sbom_env(),
        )

    assert api.uploads == []


def test_package_helpers_address_the_requested_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SBOM package is a separate namespace, and only release enumeration is hardcoded."""
    api = release_tools.GitLabApi("https://gitlab.example/api/v4", "38", "job-token")
    sbom_version = release_tools.digest_package_version(DIGEST)
    release_path = "/packages/generic/robot-dev-team-release/0.2.0/sbom.spdx.json"
    sbom_path = f"/packages/generic/robot-dev-team-sbom/{sbom_version}/sbom.spdx.json"

    assert api.package_path("0.2.0", "sbom.spdx.json") == release_path
    assert (
        api.package_path(
            sbom_version,
            "sbom.spdx.json",
            package_name=release_tools.SBOM_PACKAGE_NAME,
        )
        == sbom_path
    )
    assert api.asset_url("0.2.0", "sbom.spdx.json").endswith(release_path)

    seen = fake_urlopen(
        monkeypatch,
        {
            ("GET", f"/projects/38{sbom_path}"): 404,
            ("PUT", f"/projects/38{sbom_path}"): b"{}",
        },
    )
    api.upload_package_file(
        sbom_version,
        "sbom.spdx.json",
        SBOM_BYTES,
        package_name=release_tools.SBOM_PACKAGE_NAME,
    )

    assert [(method, path) for method, path, _, _ in seen] == [
        ("GET", f"/projects/38{sbom_path}"),
        ("PUT", f"/projects/38{sbom_path}"),
    ]
    assert seen[1][2] == SBOM_BYTES


def test_package_versions_never_lists_another_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """`package_name` is a substring filter, so the returned name is checked, not trusted.

    A phantom version reaching `non_yanked_release_versions()` would be added to alias planning
    with no manifest check, silently suppressing `latest`/`X`/`X.Y` for a real release. The
    SBOM package does not match this query today; a future `robot-dev-team-release-*` would.
    """
    api = release_tools.GitLabApi("https://gitlab.example/api/v4", "38", "job-token")
    query = "package_type=generic&package_name=robot-dev-team-release&per_page=100&page=1"
    records = json.dumps(
        [
            {"name": "robot-dev-team-release", "version": "0.2.0"},
            {"name": "robot-dev-team-release-staging", "version": "0.9.9"},
            {"name": release_tools.SBOM_PACKAGE_NAME, "version": "sha256-" + "a" * 64},
        ]
    ).encode()
    fake_urlopen(monkeypatch, {("GET", f"/projects/38/packages?{query}"): records})

    assert api.package_versions() == ["0.2.0"]


def test_plan_release_skips_links_already_present() -> None:
    api = FakeApi(
        releases={
            "v0.2.0": release_record(
                ("release-manifest.json", "https://gitlab.example/api/0.2.0/release-manifest.json"),
                ("changelog.md", "https://gitlab.example/api/0.2.0/changelog.md"),
            )
        }
    )

    plan = release_tools.plan_release(api, "0.2.0", tag="v0.2.0")
    assert plan.exists is True
    assert plan.pending_links == ()

    yank_plan = release_tools.plan_release(api, "0.2.0", tag="v0.2.0", include_yank=True)
    assert [link["name"] for link in yank_plan.pending_links] == ["yank-record.json"]

    sbom_plan = release_tools.plan_release(api, "0.2.0", tag="v0.2.0", include_sbom=True)
    assert [link["name"] for link in sbom_plan.pending_links] == ["sbom.spdx.json"]
    # The link is version-scoped, so the plan stays resolvable before the digest is known.
    assert sbom_plan.pending_links[0]["url"] == "https://gitlab.example/api/0.2.0/sbom.spdx.json"


def test_plan_release_reports_a_missing_release() -> None:
    api = FakeApi()
    plan = release_tools.plan_release(api, "0.2.0", tag="v0.2.0")

    assert plan.exists is False
    assert [link["name"] for link in plan.pending_links] == [
        "release-manifest.json",
        "changelog.md",
    ]


@pytest.mark.parametrize(
    ("release", "message"),
    [
        ({}, "readable assets object"),
        ({"assets": []}, "readable assets object"),
        ({"assets": {}}, "readable link list"),
        ({"assets": {"links": {}}}, "readable link list"),
        ({"assets": {"links": ["changelog.md"]}}, "unreadable asset link"),
        ({"assets": {"links": [{"name": "changelog.md"}]}}, "unreadable asset link"),
        ({"assets": {"links": [{"name": 7, "url": "https://gitlab.example/x"}]}},
         "unreadable asset link"),
    ],
)
def test_existing_release_links_fails_closed_on_unreadable_payloads(
    release: dict[str, Any], message: str
) -> None:
    """A present release with an unreadable asset payload is not "no links"."""
    with pytest.raises(release_tools.ReleaseError, match=message):
        release_tools.existing_release_links(release)


def test_existing_release_links_accepts_an_absent_release_and_an_empty_list() -> None:
    assert release_tools.existing_release_links(None) == []
    assert release_tools.existing_release_links({"assets": {"links": []}}) == []


def test_pending_release_links_fails_closed_on_conflicting_state() -> None:
    desired = [{"name": "changelog.md", "url": "https://gitlab.example/api/0.2.0/changelog.md"}]

    with pytest.raises(release_tools.ReleaseError, match="different URL"):
        release_tools.pending_release_links(
            desired,
            [{"name": "changelog.md", "url": "https://gitlab.example/other/changelog.md"}],
        )

    with pytest.raises(release_tools.ReleaseError, match="already used by"):
        release_tools.pending_release_links(
            desired,
            [{"name": "notes.md", "url": "https://gitlab.example/api/0.2.0/changelog.md"}],
        )


def test_apply_release_plan_creates_a_missing_release_with_its_links() -> None:
    api = FakeApi()
    plan = release_tools.plan_release(api, "0.2.0", tag="v0.2.0")

    release_tools.apply_release_plan(api, plan, version="0.2.0", notes="Release notes\n")

    assert [call["action"] for call in api.release_calls] == ["create"]
    created = api.release_calls[0]
    assert created["tag"] == "v0.2.0"
    assert created["name"] == "Robot Dev Team v0.2.0"
    assert created["description"] == "Release notes\n"
    assert [link["name"] for link in created["links"]] == [
        "release-manifest.json",
        "changelog.md",
    ]


def test_apply_release_plan_updates_an_existing_release_and_adds_only_missing_links() -> None:
    api = FakeApi(
        releases={
            "v0.2.0": release_record(
                ("release-manifest.json", "https://gitlab.example/api/0.2.0/release-manifest.json"),
                ("changelog.md", "https://gitlab.example/api/0.2.0/changelog.md"),
            )
        }
    )
    plan = release_tools.plan_release(api, "0.2.0", tag="v0.2.0", include_yank=True)

    release_tools.apply_release_plan(api, plan, version="0.2.0", notes="Yanked\n")

    # The notes are replaced (a yank rewrites them) but the two links that already exist are
    # never re-sent: GitLab rejects duplicate release link names and URLs.
    assert api.release_calls == [
        {
            "action": "update",
            "tag": "v0.2.0",
            "name": "Robot Dev Team v0.2.0",
            "description": "Yanked\n",
        },
        {
            "action": "link",
            "tag": "v0.2.0",
            "link": {
                "name": "yank-record.json",
                "url": "https://gitlab.example/api/0.2.0/yank-record.json",
                "link_type": "other",
            },
        },
    ]


def test_apply_release_plan_sends_no_links_when_none_are_pending() -> None:
    api = FakeApi(
        releases={
            "v0.2.0": release_record(
                ("release-manifest.json", "https://gitlab.example/api/0.2.0/release-manifest.json"),
                ("changelog.md", "https://gitlab.example/api/0.2.0/changelog.md"),
            )
        }
    )
    plan = release_tools.plan_release(api, "0.2.0", tag="v0.2.0")

    release_tools.apply_release_plan(api, plan, version="0.2.0", notes="Release notes\n")

    assert [call["action"] for call in api.release_calls] == ["update"]


def test_release_writes_never_shell_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """The release record is written over the API; the release image ships no git or CLI."""
    monkeypatch.setattr(
        release_tools,
        "run",
        lambda *args, **kwargs: pytest.fail("release publication must not run a subprocess"),
    )
    seen = fake_urlopen(
        monkeypatch,
        {
            ("GET", "/projects/38/releases/v0.2.0"): 404,
            ("GET", "/projects/38/releases?per_page=1"): b"[]",
            ("POST", "/projects/38/releases"): b"{}",
        },
    )
    api = release_tools.GitLabApi("https://gitlab.example/api/v4", "38", "job-token")
    plan = release_tools.plan_release(api, "0.2.0", tag="v0.2.0")

    release_tools.apply_release_plan(api, plan, version="0.2.0", notes="Release notes\n")

    method, path, body, headers = seen[-1]
    assert (method, path) == ("POST", "/projects/38/releases")
    assert headers["Content-type"] == "application/json"
    assert headers["Job-token"] == "job-token"
    assert body is not None
    payload = json.loads(body)
    assert payload["tag_name"] == "v0.2.0"
    assert payload["name"] == "Robot Dev Team v0.2.0"
    assert payload["description"] == "Release notes\n"
    # No `ref`: the job only runs on an existing protected tag, and the API must never be
    # able to create a tag of its own.
    assert "ref" not in payload
    assert [link["name"] for link in payload["assets"]["links"]] == [
        "release-manifest.json",
        "changelog.md",
    ]


def test_release_read_separates_a_missing_release_from_an_unreadable_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A private project masks an unauthorized read as 404, so the 404 is confirmed."""
    api = release_tools.GitLabApi("https://gitlab.example/api/v4", "38", "job-token")

    fake_urlopen(
        monkeypatch,
        {
            ("GET", "/projects/38/releases/v0.2.0"): 404,
            ("GET", "/projects/38/releases?per_page=1"): b"[]",
        },
    )
    assert api.release("v0.2.0") is None

    fake_urlopen(
        monkeypatch,
        {
            ("GET", "/projects/38/releases/v0.2.0"): 404,
            ("GET", "/projects/38/releases?per_page=1"): 404,
        },
    )
    with pytest.raises(release_tools.ReleaseError, match="/releases"):
        api.release("v0.2.0")


def test_release_update_and_link_use_the_documented_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = fake_urlopen(
        monkeypatch,
        {
            ("PUT", "/projects/38/releases/v0.2.0"): b"{}",
            ("POST", "/projects/38/releases/v0.2.0/assets/links"): b"{}",
        },
    )
    api = release_tools.GitLabApi("https://gitlab.example/api/v4", "38", "job-token")

    api.update_release(tag="v0.2.0", name="Robot Dev Team v0.2.0", description="Notes\n")
    api.add_release_link(
        tag="v0.2.0",
        link={"name": "yank-record.json", "url": "https://gitlab.example/y", "link_type": "other"},
    )

    assert [(method, path) for method, path, _, _ in seen] == [
        ("PUT", "/projects/38/releases/v0.2.0"),
        ("POST", "/projects/38/releases/v0.2.0/assets/links"),
    ]
    assert json.loads(seen[0][2] or b"{}") == {
        "name": "Robot Dev Team v0.2.0",
        "description": "Notes\n",
    }
    assert json.loads(seen[1][2] or b"{}")["name"] == "yank-record.json"


def test_successful_releases_excludes_yanked_versions() -> None:
    api = FakeApi(
        {
            ("0.2.0", "release-manifest.json"): json.dumps(
                release_manifest("0.2.0")
            ).encode(),
            ("0.2.0", "yank-record.json"): b"{}",
            ("0.1.0", "release-manifest.json"): json.dumps(
                release_manifest("0.1.0")
            ).encode(),
        }
    )
    assert list(release_tools.successful_releases(api)) == [release_tools.Version.parse("0.1.0")]


def test_successful_releases_skips_malformed_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = FakeApi(
        {
            ("0.1.0", "release-manifest.json"): b"{not-json",
            ("0.2.0", "release-manifest.json"): json.dumps(
                release_manifest("0.2.0")
            ).encode(),
        }
    )

    assert list(release_tools.successful_releases(api)) == [
        release_tools.Version.parse("0.2.0")
    ]
    assert release_tools.non_yanked_release_versions(api) == {
        release_tools.Version.parse("0.1.0"),
        release_tools.Version.parse("0.2.0"),
    }
    assert "ignoring invalid release record 0.1.0" in capsys.readouterr().err


def test_yank_recomputes_aliases_and_records_api_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_manifest = release_manifest(
        "0.2.1",
        aliases=[
            {"name": "0.2.1", "kind": "immutable"},
            {"name": "0.2", "kind": "moving"},
            {"name": "0", "kind": "moving"},
            {"name": "latest", "kind": "moving"},
        ],
    )
    target_manifest = release_manifest(
        "0.2.0",
        digest=TARGET_DIGEST,
        source_commit="1" * 40,
    )
    api = FakeApi(
        {
            ("0.2.1", "release-manifest.json"): json.dumps(bad_manifest).encode(),
            ("0.2.1", "changelog.md"): b"## [v0.2.1] - 2026-07-23\n",
            ("0.2.0", "release-manifest.json"): json.dumps(target_manifest).encode(),
        },
        job_user={"id": 7, "username": "release-operator"},
        releases={
            "v0.2.1": release_record(
                ("release-manifest.json", "https://gitlab.example/api/0.2.1/release-manifest.json"),
                ("changelog.md", "https://gitlab.example/api/0.2.1/changelog.md"),
            )
        },
    )
    alias_digests = {"0.2": DIGEST, "0": f"sha256:{'c' * 64}", "latest": DIGEST}

    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    monkeypatch.setattr(release_tools, "crane_login", lambda *args: None)
    monkeypatch.setattr(
        release_tools,
        "crane_digest",
        lambda crane, reference: alias_digests[reference.rsplit(":", 1)[1]],
    )

    def fake_run(arguments: tuple[str, ...], **_: Any) -> subprocess.CompletedProcess[str]:
        if len(arguments) >= 4 and arguments[1] == "tag":
            alias_digests[arguments[3]] = TARGET_DIGEST
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(release_tools, "run", fake_run)
    env = release_env(
        CI_COMMIT_TAG="v0.2.1-yank",
        GITLAB_USER_LOGIN="spoofed-value-is-ignored",
    )
    context_path = write_yank_context(tmp_path, version="0.2.1")

    record = release_tools.yank_release(
        context_path=context_path,
        artifacts_dir=tmp_path / "artifacts",
        crane=Path("crane"),
        environ=env,
    )

    assert record["alias_targets"] == {"0.2": "0.2.0", "latest": "0.2.0"}
    assert record["aliases_skipped"] == ["0"]
    assert record["operator"] == {"id": 7, "username": "release-operator"}
    assert api.package_file("0.2.1", "yank-record.json") is not None
    assert [call["action"] for call in api.release_calls] == ["update", "link"]
    assert api.release_calls[0]["tag"] == "v0.2.1"
    assert api.release_calls[0]["description"].startswith("> **YANKED:**")
    # The original release already carries the manifest and changelog links; re-sending
    # them would be rejected as duplicates after the notes had already been updated. This
    # release predates the SBOM work and has no SBOM package file, so the yank must not
    # invent a link for one: GitLab does not check that a link URL resolves.
    assert api.release_calls[1]["link"]["name"] == "yank-record.json"


def test_yank_links_the_sbom_only_when_the_release_has_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release published with an SBOM keeps the link through a yank; one without stays without."""

    def yank(*, with_sbom: bool) -> FakeApi:
        files: dict[tuple[str, str], bytes] = {
            ("0.2.0", "release-manifest.json"): json.dumps(release_manifest("0.2.0")).encode(),
            ("0.2.0", "changelog.md"): b"## [v0.2.0] - 2026-07-23\n",
        }
        if with_sbom:
            files[("0.2.0", "sbom.spdx.json")] = SBOM_BYTES
        api = FakeApi(files)
        monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
        monkeypatch.setattr(release_tools, "crane_login", lambda *args: None)
        monkeypatch.setattr(release_tools, "crane_digest", lambda *args: DIGEST)
        release_tools.yank_release(
            context_path=write_yank_context(tmp_path, version="0.2.0"),
            artifacts_dir=tmp_path / "artifacts",
            crane=Path("crane"),
            environ=release_env(CI_COMMIT_TAG="v0.2.0-yank"),
        )
        return api

    with_sbom = yank(with_sbom=True)
    assert [call["links"] for call in with_sbom.release_calls] == [
        [
            {
                "name": "release-manifest.json",
                "url": "https://gitlab.example/api/0.2.0/release-manifest.json",
                "link_type": "other",
            },
            {
                "name": "changelog.md",
                "url": "https://gitlab.example/api/0.2.0/changelog.md",
                "link_type": "other",
            },
            {
                "name": "sbom.spdx.json",
                "url": "https://gitlab.example/api/0.2.0/sbom.spdx.json",
                "link_type": "other",
            },
            {
                "name": "yank-record.json",
                "url": "https://gitlab.example/api/0.2.0/yank-record.json",
                "link_type": "other",
            },
        ]
    ]

    without_sbom = yank(with_sbom=False)
    assert [
        link["name"] for call in without_sbom.release_calls for link in call["links"]
    ] == ["release-manifest.json", "changelog.md", "yank-record.json"]


def test_yank_requires_api_context_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        release_tools,
        "crane_login",
        lambda *args: pytest.fail("registry login must not be attempted"),
    )
    environ = release_env(CI_COMMIT_TAG="v0.2.0-yank")
    del environ["CI_PROJECT_ID"]

    with pytest.raises(release_tools.ReleaseError, match="CI_PROJECT_ID is required"):
        release_tools.yank_release(
            context_path=write_yank_context(tmp_path),
            artifacts_dir=tmp_path / "artifacts",
            crane=Path("crane"),
            environ=environ,
        )


def test_yank_stops_when_the_release_record_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The yank resolves its links before the aliases move, and an API failure stops it."""
    api = FakeApi(
        {
            ("0.2.0", "release-manifest.json"): json.dumps(release_manifest("0.2.0")).encode(),
            ("0.2.0", "changelog.md"): b"## [v0.2.0] - 2026-07-23\n",
        },
        release_error="GitLab API GET /releases/v0.2.0 failed (500)",
    )
    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    monkeypatch.setattr(
        release_tools,
        "crane_login",
        lambda *args: pytest.fail("registry login must not be attempted"),
    )
    monkeypatch.setattr(
        release_tools,
        "run",
        lambda *args, **kwargs: pytest.fail("tag write must not be attempted"),
    )

    with pytest.raises(release_tools.ReleaseError, match="failed"):
        release_tools.yank_release(
            context_path=write_yank_context(tmp_path),
            artifacts_dir=tmp_path / "artifacts",
            crane=Path("crane"),
            environ=release_env(CI_COMMIT_TAG="v0.2.0-yank"),
        )

    assert api.uploads == []
    assert api.release_calls == []


def test_yank_rejects_cross_line_replacement_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_manifest = release_manifest(
        "0.2.0",
        aliases=[
            {"name": "0.2", "kind": "moving"},
            {"name": "0", "kind": "moving"},
            {"name": "latest", "kind": "moving"},
        ],
    )
    prior_manifest = release_manifest(
        "0.1.0",
        digest=TARGET_DIGEST,
        source_commit="1" * 40,
    )
    api = FakeApi(
        {
            ("0.2.0", "release-manifest.json"): json.dumps(bad_manifest).encode(),
            ("0.2.0", "changelog.md"): b"## [v0.2.0] - 2026-07-23\n",
            ("0.1.0", "release-manifest.json"): json.dumps(prior_manifest).encode(),
        }
    )
    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    monkeypatch.setattr(release_tools, "crane_login", lambda *args: None)
    monkeypatch.setattr(release_tools, "crane_digest", lambda *args: DIGEST)
    monkeypatch.setattr(
        release_tools,
        "run",
        lambda *args, **kwargs: pytest.fail("tag write must not be attempted"),
    )

    with pytest.raises(release_tools.ReleaseError, match="lack a compatible"):
        release_tools.yank_release(
            context_path=write_yank_context(tmp_path),
            artifacts_dir=tmp_path / "artifacts",
            crane=Path("crane"),
            environ=release_env(
                CI_COMMIT_TAG="v0.2.0-yank",
            ),
        )


def test_download_tool_verifies_and_extracts_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_stream = io.BytesIO()
    with tarfile.open(fileobj=archive_stream, mode="w:gz") as archive:
        binary = b"binary"
        member = tarfile.TarInfo("bin/crane")
        member.size = len(binary)
        archive.addfile(member, io.BytesIO(binary))
    archive_bytes = archive_stream.getvalue()
    metadata = {
        "version": "test",
        "url": "https://example.test/crane.tar.gz",
        "sha256": release_tools.hashlib.sha256(archive_bytes).hexdigest(),
    }

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return archive_bytes

    monkeypatch.setitem(release_tools.TOOL_RELEASES, "crane", metadata)
    monkeypatch.setattr(release_tools.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    destination = release_tools.download_tool("crane", tmp_path)

    assert destination.read_bytes() == b"binary"
    assert destination.stat().st_mode & 0o111


def test_install_tools_can_select_a_subset() -> None:
    """The yank path installs crane alone, so withdrawal never waits on the scanner."""
    assert release_tools.YANK_TOOLS == ("crane",)
    assert "grype" in release_tools.TOOL_RELEASES
    assert "grype" not in release_tools.YANK_TOOLS


def test_install_tools_rejects_an_unpinned_tool(tmp_path: Path) -> None:
    with pytest.raises(release_tools.ReleaseError, match="no pinned release is configured"):
        release_tools.install_tools(tmp_path, ["trivy"])


# --- vulnerability policy ---------------------------------------------------------------


def test_fixable_high_blocks_the_release() -> None:
    evaluation = evaluate(scan_report(matches=[scan_match("CVE-2026-0001")]))

    assert evaluation["verdict"] == "fail"
    assert evaluation["counts"]["blocking"] == 1
    assert evaluation["blocking_findings"][0]["id"] == "CVE-2026-0001"


def test_unfixed_high_is_recorded_without_blocking() -> None:
    """The day-one rule is fix-state aware, and the evidence for tightening it must survive.

    This is deliberately not `--only-fixed` on the scanner: the finding stays in the report and
    is listed in the evaluation, so #60 can be argued from the durable record.
    """
    evaluation = evaluate(
        scan_report(
            matches=[
                scan_match("CVE-2026-0002", fix_state="not-fixed"),
                scan_match("CVE-2026-0003", fix_state="wont-fix", severity="Critical"),
            ]
        )
    )

    assert evaluation["verdict"] == "pass"
    assert evaluation["counts"]["unfixed_high_or_critical"] == 2
    assert {row["id"] for row in evaluation["unfixed_high_or_critical"]} == {
        "CVE-2026-0002",
        "CVE-2026-0003",
    }


def test_medium_never_blocks_whatever_its_fix_state() -> None:
    evaluation = evaluate(
        scan_report(
            matches=[
                scan_match("CVE-2026-0004", severity="Medium"),
                scan_match("CVE-2026-0005", severity="Low"),
                scan_match("CVE-2026-0006", severity="Negligible", fix_state="not-fixed"),
            ]
        )
    )

    assert evaluation["verdict"] == "pass"
    assert evaluation["counts"]["total"] == 3
    assert evaluation["counts"]["by_severity_and_fix_state"]["Medium"] == {"fixed": 1}


def test_exact_exception_absorbs_only_its_own_finding() -> None:
    report = scan_report(
        matches=[scan_match("CVE-2026-0001"), scan_match("CVE-2026-0009")],
    )

    evaluation = evaluate(report, exceptions=[parse_one()])

    assert evaluation["verdict"] == "fail"
    assert [row["id"] for row in evaluation["blocking_findings"]] == ["CVE-2026-0009"]
    assert evaluation["exceptions_applied"][0]["absorbed"] == ["CVE-2026-0001"]


def test_class_exception_lists_every_id_it_absorbed() -> None:
    """A class entry hides many ids behind one rationale, so the record must name them all."""
    report = scan_report(
        matches=[
            scan_match("GO-2023-1840", package="stdlib", version="go1.19.8", package_type="go-module"),
            scan_match("GO-2023-2185", package="stdlib", version="go1.19.8", package_type="go-module"),
        ]
    )
    entry = parse_one(
        id=None,
        **{"class": "go-stdlib-toolchain"},
        package="stdlib",
        version="go1.19.8",
        type="go-module",
    )

    evaluation = evaluate(report, exceptions=[entry])

    assert evaluation["verdict"] == "pass"
    assert evaluation["exceptions_applied"][0]["absorbed"] == ["GO-2023-1840", "GO-2023-2185"]


def test_exception_does_not_reach_another_installed_version() -> None:
    report = scan_report(matches=[scan_match("CVE-2026-0001", version="3.12.14")])

    with pytest.raises(release_tools.ReleaseError, match="no longer installed"):
        evaluate(report, exceptions=[parse_one()])


def test_expired_exception_fails_closed() -> None:
    report = scan_report(matches=[scan_match("CVE-2026-0001")])
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    with pytest.raises(release_tools.ReleaseError, match="expired on"):
        evaluate(report, exceptions=[parse_one(expires=yesterday)])


def test_exception_expiring_today_is_still_valid() -> None:
    """The boundary is inclusive: an entry is live through the whole of its expiry date."""
    report = scan_report(matches=[scan_match("CVE-2026-0001")])

    # UTC, as the evaluation is: a local date is a day behind UTC between local and UTC midnight
    # west of Greenwich, and the test then failed on the one boundary it exists to pin.
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    evaluation = evaluate(report, exceptions=[parse_one(expires=today)])

    assert evaluation["verdict"] == "pass"


def test_exception_matching_only_non_blocking_findings_fails_closed() -> None:
    """An entry that suppresses nothing still reads as an accepted risk to whoever reviews it."""
    report = scan_report(matches=[scan_match("CVE-2026-0001", severity="Medium")])

    with pytest.raises(release_tools.ReleaseError, match="matches only findings that do not block"):
        evaluate(report, exceptions=[parse_one()])


def test_unused_exception_fails_closed() -> None:
    """The usual way an exception file rots is entries outliving the findings that justified
    them, with nothing ever failing. So an entry matching nothing is a hard error."""
    with pytest.raises(release_tools.ReleaseError, match="matches no finding"):
        evaluate(scan_report(), exceptions=[parse_one()])


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"package": "py*"}, "must not contain a wildcard"),
        ({"id": "CVE-2026-*"}, "must not contain a wildcard"),
        ({"package": None}, "package is required"),
        ({"owner": None}, "owner is required"),
        ({"rationale": None}, "rationale is required"),
        ({"expires": None}, "expires is required"),
        ({"expires": "soon"}, "expires must be an ISO date"),
    ],
)
def test_parser_rejects_malformed_entries(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(release_tools.ReleaseError, match=message):
        parse_one(**overrides)


def test_parser_rejects_a_bare_vulnerability_id() -> None:
    """An id with no artifact scope would follow the CVE onto any future package or version."""
    with pytest.raises(release_tools.ReleaseError, match="package is required"):
        release_tools.parse_exception_entry(
            {
                "id": "CVE-2026-0001",
                "owner": "@cavin",
                "rationale": "no",
                "expires": "2099-01-01",
            },
            1,
        )


def test_parser_rejects_an_entry_that_is_both_forms() -> None:
    with pytest.raises(release_tools.ReleaseError, match="exactly one of id or class"):
        parse_one(**{"class": "go-stdlib-toolchain"})


def test_parser_rejects_an_unknown_field() -> None:
    """A typo in a field name must fail rather than silently disable the field it meant."""
    with pytest.raises(release_tools.ReleaseError, match="unknown field"):
        parse_one(expiry="2099-01-01")


@pytest.mark.parametrize(
    "overrides",
    [
        {"tracking_issue": None},
        {"id": None, "tracking_issue": None, "class": "go-stdlib-toolchain"},
    ],
    ids=["exact", "class"],
)
def test_every_exception_requires_a_tracking_issue(overrides: dict[str, Any]) -> None:
    """An accepted finding with no tracking issue has nowhere for its remediation to live."""
    with pytest.raises(release_tools.ReleaseError, match="tracking_issue is required"):
        parse_one(**overrides)


def test_exception_file_must_declare_its_version() -> None:
    with pytest.raises(release_tools.ReleaseError, match="must declare version"):
        release_tools.parse_exceptions({"exceptions": []})


def test_absent_exception_file_is_an_empty_policy(tmp_path: Path) -> None:
    exceptions, raw = release_tools.load_exceptions(tmp_path / "missing.yaml")

    assert exceptions == ()
    assert raw == b""


def test_shipped_exception_file_parses_and_covers_the_accepted_findings() -> None:
    """The current content of the gate, checked in rather than described.

    One group now, and it is exact rather than a class entry. The glab five share one root
    cause and clear on a single toolchain bump -- the class form's usual case -- but glab is
    staying in the image, so a class entry on stdlib@go1.26.5 would be unbounded over every
    future Go stdlib disclosure against the same build.

    The CPython ten that shipped alongside them (#66) are gone: the 3.14 runtime migration
    cleared every one, so they were deleted rather than re-scoped. Asserting the exact count
    here is what makes an entry's arrival or departure a decision rather than a diff nobody
    reads.
    """
    exceptions, raw = release_tools.load_exceptions(release_tools.EXCEPTIONS_PATH)

    assert raw
    assert len(exceptions) == 5
    assert {entry.kind for entry in exceptions} == {"exact"}
    assert all(entry.owner == "@cavin" for entry in exceptions)

    by_issue: dict[str, set[tuple[str, str, str]]] = {}
    for entry in exceptions:
        by_issue.setdefault(entry.tracking_issue, set()).add(entry.artifact_scope)

    assert by_issue == {"#71": {("stdlib", "go1.26.5", "go-module")}}
    assert sum(entry.tracking_issue == "#71" for entry in exceptions) == 5


def test_report_must_bind_to_the_released_digest() -> None:
    """The package path binds nothing: the manual-recovery upload is unconstrained."""
    with pytest.raises(release_tools.ReleaseError, match="not taken against"):
        evaluate(scan_report(digest=TARGET_DIGEST), digest=DIGEST)


def test_report_scanned_by_tag_is_rejected() -> None:
    """Tags move. A scan of `image:1.2.3` is not evidence about the digest being released."""
    with pytest.raises(release_tools.ReleaseError, match="A tag reference is not acceptable"):
        evaluate(scan_report(user_input=f"{REGISTRY_IMAGE}:0.2.0"))


def test_report_resolving_another_manifest_is_rejected() -> None:
    report = scan_report()
    report["source"]["target"]["manifestDigest"] = TARGET_DIGEST

    with pytest.raises(release_tools.ReleaseError, match="resolved"):
        evaluate(report)


def test_stale_database_fails_closed() -> None:
    """A scan that silently ran against an old database is not a passing scan."""
    old = (
        (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=72))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    with pytest.raises(release_tools.ReleaseError, match="older than the permitted"):
        evaluate(scan_report(built=old))


def test_invalid_database_fails_closed() -> None:
    report = scan_report()
    report["descriptor"]["db"]["status"]["valid"] = False

    with pytest.raises(release_tools.ReleaseError, match="reported itself invalid"):
        evaluate(report)


def test_report_from_an_unpinned_scanner_is_rejected() -> None:
    """The pin is what makes the threshold reproducible, not only what makes it trustworthy."""
    with pytest.raises(release_tools.ReleaseError, match="not the pinned"):
        evaluate(scan_report(grype_version="0.106.0"))


def test_malformed_report_is_rejected() -> None:
    report = scan_report()
    report["matches"] = "not-a-list"

    with pytest.raises(release_tools.ReleaseError, match="no match list"):
        evaluate(report)


def test_report_without_an_image_source_is_rejected() -> None:
    with pytest.raises(release_tools.ReleaseError, match="does not describe an image source"):
        evaluate({"source": {"type": "directory"}, "matches": [], "descriptor": {}})


def test_eol_distro_is_recorded_rather_than_swallowed() -> None:
    """Under-reporting is the one failure mode a CVE gate cannot detect on its own.

    The runtime left bookworm in #59, so this is now driven by an explicit Debian 12 report
    rather than the default fixture. That is the case that has to keep working: scan evidence
    is digest-keyed and outlives the image it describes, so a yank or a forensic
    re-evaluation can still hand this evaluator a report taken against a bookworm release.
    """
    evaluation = evaluate(scan_report(distro={"name": "debian", "version": "12.15"}))

    assert evaluation["distro"]["end_of_life"] is True
    assert "#59" in evaluation["distro"]["note"]
    assert "WARNING" in release_tools.summarize_evaluation(evaluation)


def test_supported_distro_is_not_flagged() -> None:
    """The shipped runtime, via the default fixture, must not carry the EOL annotation."""
    evaluation = evaluate()

    assert evaluation["distro"] == {
        "name": "debian",
        "version": "13.6",
        "end_of_life": False,
        "note": None,
    }
    assert "WARNING" not in release_tools.summarize_evaluation(evaluation)


def test_evaluation_records_the_policy_and_scanner_identity() -> None:
    evaluation = evaluate(exceptions_raw=b"version: 1\n")

    assert evaluation["policy"]["version"] == release_tools.SCAN_POLICY_VERSION
    assert evaluation["policy"]["exceptions_sha256"]
    assert evaluation["policy"]["sha256"]
    assert evaluation["scanner"]["version"] == PINNED_GRYPE
    assert evaluation["scanner"]["archive_sha256"] == (
        release_tools.TOOL_RELEASES["grype"]["sha256"]
    )
    assert evaluation["database"]["age_check"]["passed"] is True


# --- the release gate -------------------------------------------------------------------


def test_publish_stops_when_the_digest_has_no_scan_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi(sbom_files=staged_sbom(), scan_files={})

    with pytest.raises(release_tools.ReleaseError, match="no durable vulnerability evidence"):
        run_publish(tmp_path, monkeypatch, api, forbid_aliases=True)

    assert api.uploads == []
    assert api.release_calls == []


def test_publish_stops_when_the_staged_evaluation_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = scan_report(matches=[scan_match("CVE-2026-0001")])
    api = FakeApi(
        sbom_files=staged_sbom(),
        scan_files=staged_scan(report=report, evaluation=evaluate(report)),
    )

    with pytest.raises(release_tools.ReleaseError, match="did not pass the vulnerability policy"):
        run_publish(tmp_path, monkeypatch, api, forbid_aliases=True)

    assert api.uploads == []


def test_publish_stops_when_the_evaluation_binds_another_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staged under the right digest is not the same as evaluating the right digest."""
    foreign = evaluate(scan_report(digest=TARGET_DIGEST), digest=TARGET_DIGEST)
    api = FakeApi(sbom_files=staged_sbom(), scan_files=staged_scan(evaluation=foreign))

    with pytest.raises(release_tools.ReleaseError, match="evaluates"):
        run_publish(tmp_path, monkeypatch, api, forbid_aliases=True)

    assert api.uploads == []


def test_publish_stops_when_the_evaluation_came_from_another_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = evaluate()
    record["scanner"]["version"] = "0.106.0"
    api = FakeApi(sbom_files=staged_sbom(), scan_files=staged_scan(evaluation=record))

    with pytest.raises(release_tools.ReleaseError, match="not produced by the pinned"):
        run_publish(tmp_path, monkeypatch, api, forbid_aliases=True)

    assert api.uploads == []


def test_publish_copies_the_staged_scan_evidence_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copied, never re-derived: a report embeds a scan time, so regenerating it would break
    the durable content check on an ordinary retry -- with the aliases already moved."""
    staged = staged_scan()
    api = FakeApi(sbom_files=staged_sbom(), scan_files=staged)
    version = release_tools.digest_package_version(DIGEST)

    _, aliases = run_publish(tmp_path, monkeypatch, api)

    assert aliases == ["0.2.0", "0.2", "0", "latest"]
    for filename in (
        release_tools.SCAN_REPORT_FILENAME,
        release_tools.SCAN_EVALUATION_FILENAME,
    ):
        assert api.files[("0.2.0", filename)] == staged[(version, filename)]
        assert (tmp_path / "artifacts" / filename).read_bytes() == staged[(version, filename)]


def run_publish_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api: FakeApi,
    *,
    scanner: Any = None,
) -> dict[str, Any]:
    """Drive publish_scan with crane and the scanner mocked out.

    `scanner` defaults to a stub that fails the test if it is called: most of these cases are
    about *not* re-scanning, and a re-scan that silently happened would otherwise look like a
    pass.
    """

    def refuse(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("the scanner must not run on this path")

    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    monkeypatch.setattr(release_tools, "crane_login", lambda *args: None)
    monkeypatch.setattr(release_tools, "crane_digest", lambda *args: DIGEST)
    monkeypatch.setattr(release_tools, "validate_grype_registry_auth", lambda *args, **kwargs: None)
    monkeypatch.setattr(release_tools, "run_grype", scanner or refuse)
    # An empty policy: these cases are about the staging and retry mechanics, and the shipped
    # exception file's entries would fail hygiene against these synthetic reports.
    return release_tools.publish_scan(
        grype=Path("grype"),
        crane=Path("crane"),
        artifacts_dir=tmp_path / "artifacts",
        exceptions_path=NO_EXCEPTIONS,
        environ=release_env(),
    )


def test_publish_scan_retry_reuses_staged_evidence_without_rescanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read before scanning. A retry of a job that already staged its evidence must be a no-op:
    the evaluation embeds a timestamp, so re-deriving it would conflict with the durable content
    check rather than succeed idempotently."""
    staged = staged_scan(exceptions_raw=b"")
    api = FakeApi(scan_files=dict(staged))

    evaluation = run_publish_scan(tmp_path, monkeypatch, api)

    assert evaluation["verdict"] == "pass"
    assert api.uploads == []
    version = release_tools.digest_package_version(DIGEST)
    written = (tmp_path / "artifacts" / release_tools.SCAN_EVALUATION_FILENAME).read_bytes()
    assert written == staged[(version, release_tools.SCAN_EVALUATION_FILENAME)]


def test_publish_scan_recovers_a_report_staged_without_its_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reachable partial state, since the report is uploaded first.

    Re-scanning would produce different bytes and be rejected by the content check, so the
    staged report is re-used verbatim and only the evaluation is derived again.
    """
    staged = staged_scan(exceptions_raw=b"")
    version = release_tools.digest_package_version(DIGEST)
    report_bytes = staged[(version, release_tools.SCAN_REPORT_FILENAME)]
    api = FakeApi(scan_files={(version, release_tools.SCAN_REPORT_FILENAME): report_bytes})

    evaluation = run_publish_scan(tmp_path, monkeypatch, api)

    assert evaluation["verdict"] == "pass"
    assert evaluation["report_sha256"] == hashlib.sha256(report_bytes).hexdigest()
    uploaded = [filename for _, _, filename, _ in api.uploads]
    assert uploaded == [release_tools.SCAN_EVALUATION_FILENAME]


def test_publish_scan_stops_on_an_evaluation_with_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreachable through the normal upload order, so it means something was staged by hand.
    Re-scanning would strand the existing evaluation against a report it never evaluated."""
    version = release_tools.digest_package_version(DIGEST)
    staged = staged_scan(exceptions_raw=b"")
    api = FakeApi(
        scan_files={
            (version, release_tools.SCAN_EVALUATION_FILENAME): staged[
                (version, release_tools.SCAN_EVALUATION_FILENAME)
            ]
        }
    )

    with pytest.raises(release_tools.ReleaseError, match="evaluation with no report"):
        run_publish_scan(tmp_path, monkeypatch, api)

    assert api.uploads == []


def test_publish_scan_stages_nothing_when_the_policy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing verdict must not become durable evidence: a later retry after adding an
    exception would produce different bytes and conflict with what is already stored."""
    report = scan_report(matches=[scan_match("CVE-2026-0001")])

    def scan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["report_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["report_path"].write_bytes(json.dumps(report).encode())
        return report

    api = FakeApi(scan_files={})

    with pytest.raises(release_tools.ReleaseError, match="did not pass"):
        run_publish_scan(tmp_path, monkeypatch, api, scanner=scan)

    assert api.uploads == []


def test_publish_scan_stages_both_documents_on_a_clean_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = scan_report(matches=[scan_match("CVE-2026-0002", fix_state="not-fixed")])

    def scan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["report_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["report_path"].write_bytes(json.dumps(report).encode())
        return report

    api = FakeApi(scan_files={})

    evaluation = run_publish_scan(tmp_path, monkeypatch, api, scanner=scan)

    assert evaluation["verdict"] == "pass"
    uploaded = [filename for _, _, filename, _ in api.uploads]
    assert uploaded == [
        release_tools.SCAN_REPORT_FILENAME,
        release_tools.SCAN_EVALUATION_FILENAME,
    ]


def test_run_grype_reports_a_failed_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scanner that cannot run must be distinguishable from a policy failure."""

    def failing(arguments: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise release_tools.ReleaseError("grype registry:... failed: database is unreachable")

    monkeypatch.setattr(release_tools, "run", failing)

    with pytest.raises(release_tools.ReleaseError, match="database is unreachable"):
        release_tools.run_grype(
            Path("grype"),
            f"{REGISTRY_IMAGE}@{DIGEST}",
            report_path=tmp_path / "report.json",
            config_path=release_tools.grype_config(tmp_path / "grype.yaml"),
            environ={},
        )


def test_run_grype_reports_a_missing_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit zero with no output is a tooling failure, not an empty result set."""
    monkeypatch.setattr(
        release_tools,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )

    with pytest.raises(release_tools.ReleaseError, match="wrote no report"):
        release_tools.run_grype(
            Path("grype"),
            f"{REGISTRY_IMAGE}@{DIGEST}",
            report_path=tmp_path / "report.json",
            config_path=release_tools.grype_config(tmp_path / "grype.yaml"),
            environ={},
        )


def test_registry_credentials_never_reach_the_command_line() -> None:
    """Grype 0.116.1 binds the GRYPE_ prefix even though its config help names SYFT_.

    Credentials go through the environment because argv appears in job logs.
    """
    env = release_tools.registry_scan_environment(release_env())

    assert env["GRYPE_REGISTRY_AUTH_AUTHORITY"] == "registry.example"
    assert env["GRYPE_REGISTRY_AUTH_USERNAME"] == "ci-user"
    assert env["GRYPE_REGISTRY_AUTH_PASSWORD"] == "ci-password"


def test_resolved_grype_auth_accepts_the_expected_masked_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolved values may be masked; only authority and non-emptiness are contractual."""
    config_path = release_tools.grype_config(tmp_path / "grype.yaml")
    scan_env = {"GRYPE_REGISTRY_AUTH_PASSWORD": "credential-sentinel"}
    seen: dict[str, Any] = {}

    def resolve(arguments: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["arguments"] = arguments
        seen["environ"] = kwargs["env"]
        return subprocess.CompletedProcess(
            arguments,
            0,
            "registry:\n"
            "  auth:\n"
            "    - authority: registry.example\n"
            "      username: '*******'\n"
            "      password: '*******'\n",
            "",
        )

    monkeypatch.setattr(release_tools.subprocess, "run", resolve)

    release_tools.validate_grype_registry_auth(
        Path("grype"),
        config_path=config_path,
        environ=scan_env,
        authority="registry.example",
    )

    assert seen["arguments"] == (
        "grype",
        "config",
        "--load",
        "--config",
        str(config_path),
    )
    assert seen["environ"] == scan_env


@pytest.mark.parametrize(
    ("resolved", "message"),
    (
        ("registry:\n  auth: []\n", "no entry for registry.example"),
        (
            "registry:\n"
            "  auth:\n"
            "    - authority: another.example\n"
            "      username: user\n"
            "      password: password\n",
            "no entry for registry.example",
        ),
        (
            "registry:\n"
            "  auth:\n"
            "    - authority: registry.example\n"
            "      username: ''\n"
            "      password: password\n",
            "empty credentials",
        ),
    ),
)
def test_resolved_grype_auth_requires_the_exact_authority_and_credentials(
    resolved: str,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release_tools.subprocess,
        "run",
        lambda arguments, **kwargs: subprocess.CompletedProcess(arguments, 0, resolved, ""),
    )

    with pytest.raises(release_tools.ReleaseError, match=message):
        release_tools.validate_grype_registry_auth(
            Path("grype"),
            config_path=tmp_path / "grype.yaml",
            environ={},
            authority="registry.example",
        )


@pytest.mark.parametrize(
    ("resolved", "message"),
    (
        ("[]\n", "not a mapping"),
        ("registry: []\n", "no registry mapping"),
        ("registry:\n  auth: {}\n", "registry.auth is not a list"),
        ("registry:\n  auth:\n    - malformed\n", "non-mapping entry"),
    ),
)
def test_resolved_grype_auth_rejects_unrecognized_document_shapes(
    resolved: str,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release_tools.subprocess,
        "run",
        lambda arguments, **kwargs: subprocess.CompletedProcess(arguments, 0, resolved, ""),
    )

    with pytest.raises(release_tools.ReleaseError, match=message):
        release_tools.validate_grype_registry_auth(
            Path("grype"),
            config_path=tmp_path / "grype.yaml",
            environ={},
            authority="registry.example",
        )


def test_resolved_grype_auth_never_discloses_stdout_on_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "credential-that-must-not-reach-the-log"
    resolved = f"registry:\n  auth:\n    - password: {secret}\n"
    monkeypatch.setattr(
        release_tools.subprocess,
        "run",
        lambda arguments, **kwargs: subprocess.CompletedProcess(arguments, 2, resolved, ""),
    )

    with pytest.raises(release_tools.ReleaseError) as caught:
        release_tools.validate_grype_registry_auth(
            Path("grype"),
            config_path=tmp_path / "grype.yaml",
            environ={},
            authority="registry.example",
        )

    assert secret not in str(caught.value)
    assert "exit 2" in str(caught.value)


def test_resolved_grype_auth_never_discloses_stdout_on_unreadable_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PyYAML parse error quotes the source, so it must not survive anywhere on the exception.

    `from None` is not enough: it clears `__cause__` and leaves the quoting error in
    `__context__`, where a traceback-formatting caller still prints it.
    """
    secret = "credential-that-must-not-reach-the-log"
    resolved = f"registry:\n  auth:\n    - password: abc: {secret}\n"
    monkeypatch.setattr(
        release_tools.subprocess,
        "run",
        lambda arguments, **kwargs: subprocess.CompletedProcess(arguments, 0, resolved, ""),
    )

    with pytest.raises(release_tools.ReleaseError) as caught:
        release_tools.validate_grype_registry_auth(
            Path("grype"),
            config_path=tmp_path / "grype.yaml",
            environ={},
            authority="registry.example",
        )

    assert "unreadable YAML" in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    formatted = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert secret not in formatted


def test_scan_target_must_be_under_the_authority_the_credentials_are_scoped_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound credentials for one registry say nothing about a pull from another."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        pytest.fail("the scan must not resolve auth or pull for a foreign authority")

    monkeypatch.setattr(release_tools, "validate_grype_registry_auth", refuse)
    monkeypatch.setattr(release_tools, "run_grype", refuse)
    environ = dict(release_env())
    environ["CI_REGISTRY_IMAGE"] = "another.example/group/project"

    with pytest.raises(release_tools.ReleaseError, match="not under the registry authority"):
        release_tools.scan_digest(
            grype=Path("grype"),
            digest=DIGEST,
            artifacts_dir=tmp_path / "artifacts",
            exceptions_path=NO_EXCEPTIONS,
            environ=environ,
        )


def test_scan_uses_one_config_and_environment_for_auth_guard_and_grype(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must validate the exact configuration and environment used by the scan.

    Order is part of the contract: a guard that ran after the pull would not gate anything.
    """
    seen: dict[str, Any] = {}
    order: list[str] = []
    grype = Path("grype")

    def guard(
        actual_grype: Path,
        *,
        config_path: Path,
        environ: dict[str, str],
        authority: str,
    ) -> None:
        seen["guard_grype"] = actual_grype
        seen["guard_config"] = config_path
        seen["guard_environ"] = environ
        seen["authority"] = authority
        order.append("guard")

    def scan(
        actual_grype: Path,
        reference: str,
        *,
        report_path: Path,
        config_path: Path,
        environ: dict[str, str],
    ) -> dict[str, Any]:
        seen["scan_grype"] = actual_grype
        seen["scan_config"] = config_path
        seen["scan_environ"] = environ
        seen["reference"] = reference
        order.append("scan")
        report = scan_report()
        report_path.write_bytes(json.dumps(report).encode())
        return report

    monkeypatch.setattr(release_tools, "validate_grype_registry_auth", guard)
    monkeypatch.setattr(release_tools, "run_grype", scan)

    release_tools.scan_digest(
        grype=grype,
        digest=DIGEST,
        artifacts_dir=tmp_path / "artifacts",
        exceptions_path=NO_EXCEPTIONS,
        environ=release_env(),
    )

    assert seen["guard_grype"] is seen["scan_grype"] is grype
    assert seen["guard_config"] is seen["scan_config"]
    assert seen["guard_environ"] is seen["scan_environ"]
    assert seen["authority"] == "registry.example"
    assert seen["reference"] == f"{REGISTRY_IMAGE}@{DIGEST}"
    assert order == ["guard", "scan"]


def test_existing_report_recovery_bypasses_auth_guard_and_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-evaluating staged bytes remains registry-free and needs no live credentials."""
    report_bytes = json.dumps(scan_report()).encode()

    def refuse(*args: Any, **kwargs: Any) -> None:
        pytest.fail("auth resolution and the scanner must not run for a staged report")

    monkeypatch.setattr(release_tools, "validate_grype_registry_auth", refuse)
    monkeypatch.setattr(release_tools, "run_grype", refuse)

    _, recovered_report, _ = release_tools.scan_digest(
        grype=Path("grype"),
        digest=DIGEST,
        artifacts_dir=tmp_path / "artifacts",
        exceptions_path=NO_EXCEPTIONS,
        environ=release_env(),
        existing_report=report_bytes,
    )

    assert recovered_report == report_bytes
    assert not (tmp_path / "artifacts" / "grype.yaml").exists()


def test_offline_evaluate_runs_the_policy_without_a_scanner(tmp_path: Path) -> None:
    """The pre-merge check. The gate cannot run on a merge request -- no image is built -- while
    the exception file's hygiene rules are hard failures, so an exception edit needs some way to
    be validated before it merges."""
    report = scan_report(matches=[scan_match("CVE-2026-0001")])
    report_path = tmp_path / "report.json"
    report_path.write_bytes(json.dumps(report).encode())
    exceptions_path = tmp_path / "exceptions.yaml"
    exceptions_path.write_text(
        "version: 1\nexceptions:\n"
        '  - id: "CVE-2026-0001"\n'
        '    package: "python"\n'
        '    version: "3.12.13"\n'
        '    type: "binary"\n'
        '    owner: "@cavin"\n'
        '    rationale: "No fix on the branch we ship."\n'
        f'    expires: "{(dt.date.today() + dt.timedelta(days=30)).isoformat()}"\n'
        '    tracking_issue: "#66"\n',
        encoding="utf-8",
    )

    evaluation = release_tools.evaluate_saved_report(
        report_path=report_path, exceptions_path=exceptions_path
    )

    assert evaluation["verdict"] == "pass"
    assert evaluation["image_digest"] == DIGEST
    assert evaluation["exceptions_applied"][0]["absorbed"] == ["CVE-2026-0001"]


def stale_database_report(**kwargs: Any) -> dict[str, Any]:
    built = (
        (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return scan_report(built=built, **kwargs)


def test_offline_evaluate_accepts_a_report_older_than_the_release_limit(tmp_path: Path) -> None:
    """The documented workflow points at default-branch artifacts kept for 30 days, so they are
    routinely past the 48-hour release limit. Database age is a property of the evidence, and
    this mode does not judge the evidence."""
    report_path = tmp_path / "report.json"
    report_path.write_bytes(json.dumps(stale_database_report()).encode())

    evaluation = release_tools.evaluate_saved_report(
        report_path=report_path, exceptions_path=NO_EXCEPTIONS
    )

    assert evaluation["verdict"] == "pass"
    assert evaluation["policy_only"] is True
    assert evaluation["database"]["age_check"]["passed"] is False
    assert "policy check, not release evidence" in release_tools.summarize_evaluation(evaluation)


def test_a_policy_only_evaluation_is_never_release_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turning off the evidence checks is recorded in the document, not just in the caller, so
    a policy-only result that someone stages by hand cannot be read as a passing gate."""
    report_path = tmp_path / "report.json"
    report_path.write_bytes(json.dumps(scan_report()).encode())
    record = release_tools.evaluate_saved_report(
        report_path=report_path, exceptions_path=NO_EXCEPTIONS
    )
    api = FakeApi(
        sbom_files=staged_sbom(),
        scan_files=staged_scan(evaluation=record),
    )

    with pytest.raises(release_tools.ReleaseError, match="offline policy check"):
        run_publish(tmp_path, monkeypatch, api, forbid_aliases=True)

    assert api.uploads == []


def test_publish_rejects_a_hash_matched_report_describing_another_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hash binds the evaluation to *a* report; this binds that report to *this* image.

    `evaluate --report <wrong file> --digest <right digest>` produces exactly this pair from one
    mistyped path while following the recovery runbook, and it is internally consistent.
    """
    foreign_report = scan_report(digest=TARGET_DIGEST)
    # A valid evaluation for the digest being released, re-pointed at a report describing a
    # different one. The hash check passes because the pair is internally consistent; only
    # parsing the report catches it.
    record = evaluate()
    record["report_sha256"] = hashlib.sha256(json.dumps(foreign_report).encode()).hexdigest()
    api = FakeApi(
        sbom_files=staged_sbom(),
        scan_files=staged_scan(report=foreign_report, evaluation=record),
    )

    with pytest.raises(release_tools.ReleaseError, match="not taken against"):
        run_publish(tmp_path, monkeypatch, api, forbid_aliases=True)

    assert api.uploads == []


def expired_evidence(days: int = 1) -> dict[str, Any]:
    """Evidence that was valid when it was produced and has since lapsed."""
    report = scan_report(matches=[scan_match("CVE-2026-0001")])
    record = evaluate(report, exceptions=[parse_one()], exceptions_raw=b"")
    record["exceptions_applied"][0]["expires"] = (
        dt.date.today() - dt.timedelta(days=days)
    ).isoformat()
    return {"report": report, "evaluation": record}


def test_publish_rejects_reused_evidence_with_a_lapsed_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence is keyed by digest and deliberately reusable, so expiry has to be re-checked
    when it is consumed. Enforcing it only at production time would let a later release of the
    same digest pass on an acceptance that ran out in between."""
    evidence = expired_evidence()
    api = FakeApi(
        sbom_files=staged_sbom(),
        scan_files=staged_scan(report=evidence["report"], evaluation=evidence["evaluation"]),
    )

    with pytest.raises(release_tools.ReleaseError, match="which expired on"):
        run_publish(
            tmp_path,
            monkeypatch,
            api,
            forbid_aliases=True,
            exceptions_path=NO_EXCEPTIONS,
        )

    assert api.uploads == []


def test_publish_scan_retry_rejects_reused_evidence_with_a_lapsed_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same check on the scan job's own retry path: one validator, not a subset."""
    evidence = expired_evidence()
    api = FakeApi(
        scan_files=staged_scan(report=evidence["report"], evaluation=evidence["evaluation"])
    )

    with pytest.raises(release_tools.ReleaseError, match="which expired on"):
        run_publish_scan(tmp_path, monkeypatch, api)

    assert api.uploads == []


def test_reused_evidence_with_an_unreadable_expiry_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = expired_evidence()
    evidence["evaluation"]["exceptions_applied"][0]["expires"] = "whenever"
    api = FakeApi(
        sbom_files=staged_sbom(),
        scan_files=staged_scan(report=evidence["report"], evaluation=evidence["evaluation"]),
    )

    with pytest.raises(release_tools.ReleaseError, match="no readable expiry"):
        run_publish(
            tmp_path,
            monkeypatch,
            api,
            forbid_aliases=True,
            exceptions_path=NO_EXCEPTIONS,
        )

    assert api.uploads == []


def test_shadow_diagnostic_ignores_non_blocking_findings() -> None:
    """Only blocking findings are ever absorbed, so only those can be shadowed."""
    report = scan_report(
        matches=[
            scan_match("CVE-2026-0001"),
            scan_match("CVE-2026-0007", severity="Medium"),
        ]
    )
    exact_medium = parse_one(id="CVE-2026-0007")
    exact_high = parse_one(id="CVE-2026-0001")

    with pytest.raises(release_tools.ReleaseError, match="matches only findings that do not"):
        evaluate(report, exceptions=[exact_high, exact_medium])


def test_offline_evaluate_catches_an_exception_that_no_longer_matches(tmp_path: Path) -> None:
    """Moving an excepted artifact invalidates every entry scoped to it, at once.

    #66 is the case that proved this in anger: the 3.14 hop retired all ten CPython entries in
    one commit. The same shape is queued behind #71, so the report here is the shipped file's
    remaining artifact rebuilt on a patched toolchain. It has to be discoverable before the
    merge, because neither the image build nor the scan runs on a merge request.
    """
    report = scan_report(
        matches=[
            scan_match(
                "GO-2026-5026",
                package="stdlib",
                version="go1.26.6",
                package_type="go-module",
            )
        ]
    )
    report_path = tmp_path / "report.json"
    report_path.write_bytes(json.dumps(report).encode())

    with pytest.raises(release_tools.ReleaseError, match="no longer installed"):
        release_tools.evaluate_saved_report(
            report_path=report_path, exceptions_path=EXCEPTIONS_PATH
        )


def test_shadowed_exception_says_so(tmp_path: Path) -> None:
    report = scan_report(matches=[scan_match("CVE-2026-0001")])
    exact = parse_one()
    shadowed = parse_one(id=None, **{"class": "cpython-3.12"})

    with pytest.raises(release_tools.ReleaseError, match="is shadowed by the earlier entry"):
        evaluate(report, exceptions=[exact, shadowed])


def test_expiring_exceptions_warn_before_they_fail() -> None:
    """Ten entries expiring on one date would otherwise announce themselves as a red pipeline."""
    report = scan_report(matches=[scan_match("CVE-2026-0001")])
    soon = (dt.date.today() + dt.timedelta(days=5)).isoformat()

    evaluation = evaluate(report, exceptions=[parse_one(expires=soon)])
    summary = release_tools.summarize_evaluation(evaluation)

    assert evaluation["verdict"] == "pass"
    assert "expires in 5 day(s)" in summary


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"policy": {"version": 1, "sha256": "0" * 64}}, "different policy than this commit"),
        ({"scanner": {"version": PINNED_GRYPE, "archive_sha256": "0" * 64}}, "archive checksum"),
        ({"report_sha256": "0" * 64}, "computed from a different report"),
    ],
)
def test_publish_rejects_tampered_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, Any],
    message: str,
) -> None:
    """The evaluation is only evidence if it is bound to the report, the policy, and the binary
    that produced it. A version string alone is self-reported."""
    record = evaluate()
    record.update(mutation)
    api = FakeApi(sbom_files=staged_sbom(), scan_files=staged_scan(evaluation=record))

    with pytest.raises(release_tools.ReleaseError, match=message):
        run_publish(tmp_path, monkeypatch, api, forbid_aliases=True)

    assert api.uploads == []


@pytest.mark.parametrize(
    "field, container",
    [
        ("severity", "vulnerability"),
        ("id", "vulnerability"),
        ("name", "artifact"),
        ("version", "artifact"),
        ("type", "artifact"),
    ],
)
def test_malformed_match_fields_fail_closed(field: str, container: str) -> None:
    """Coercing these with str() would turn a missing severity into "not High/Critical" --
    malformed output producing a passing verdict."""
    match = scan_match("CVE-2026-0001")
    match[container][field] = None

    with pytest.raises(release_tools.ReleaseError, match="no readable"):
        evaluate(scan_report(matches=[match]))


def test_unreadable_fix_state_fails_closed() -> None:
    match = scan_match("CVE-2026-0001")
    match["vulnerability"]["fix"]["state"] = 7

    with pytest.raises(release_tools.ReleaseError, match="unreadable fix state"):
        evaluate(scan_report(matches=[match]))


def test_empty_fix_state_is_recorded_as_unknown_and_does_not_block() -> None:
    """Grype emits `"state": ""` for some matches -- two on the current image -- so this is
    scanner output, not corruption. Unknown is not a known-available fix, so it does not block,
    and recording it as `unknown` keeps it visible in the counts."""
    match = scan_match("CVE-2026-0001")
    match["vulnerability"]["fix"]["state"] = ""

    evaluation = evaluate(scan_report(matches=[match]))

    assert evaluation["verdict"] == "pass"
    assert evaluation["counts"]["by_severity_and_fix_state"]["High"] == {"unknown": 1}


def test_yank_succeeds_with_no_scan_evidence_and_no_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Withdrawal must always be available.

    A gate on the yank path would make a bad release impossible to pull during an Anchore
    outage -- or, worse, impossible precisely when the CVE motivating the yank is the one
    failing the scan. Nothing here installs or invokes the scanner.
    """
    api = FakeApi(
        {
            ("0.2.0", "release-manifest.json"): json.dumps(release_manifest("0.2.0")).encode(),
            ("0.2.0", "changelog.md"): b"## [v0.2.0] - 2026-07-23\n",
        },
        scan_files={},
    )
    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    monkeypatch.setattr(release_tools, "crane_login", lambda *args: None)
    monkeypatch.setattr(release_tools, "crane_digest", lambda *args: DIGEST)
    monkeypatch.setattr(
        release_tools,
        "download_tool",
        lambda *args, **kwargs: pytest.fail("the yank path must not download the scanner"),
    )

    record = release_tools.yank_release(
        context_path=write_yank_context(tmp_path, version="0.2.0"),
        artifacts_dir=tmp_path / "artifacts",
        crane=Path("crane"),
        environ=release_env(CI_COMMIT_TAG="v0.2.0-yank"),
    )

    assert record["yanked_version"] == "0.2.0"
    created = [call for call in api.release_calls if call["action"] == "create"]
    linked = {link["name"] for link in created[0]["links"]}
    assert linked == {"release-manifest.json", "changelog.md", "yank-record.json"}


def test_yank_links_the_scan_evidence_only_when_the_release_has_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitLab does not check that a link URL resolves, so a release published before this
    landed must not gain a permanent 404 when its links are reconciled."""
    files: dict[tuple[str, str], bytes] = {
        ("0.2.0", "release-manifest.json"): json.dumps(release_manifest("0.2.0")).encode(),
        ("0.2.0", "changelog.md"): b"## [v0.2.0] - 2026-07-23\n",
        ("0.2.0", release_tools.SCAN_REPORT_FILENAME): b"{}",
        ("0.2.0", release_tools.SCAN_EVALUATION_FILENAME): b"{}",
    }
    api = FakeApi(files)
    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda env: (api, {}))
    monkeypatch.setattr(release_tools, "crane_login", lambda *args: None)
    monkeypatch.setattr(release_tools, "crane_digest", lambda *args: DIGEST)

    release_tools.yank_release(
        context_path=write_yank_context(tmp_path, version="0.2.0"),
        artifacts_dir=tmp_path / "artifacts",
        crane=Path("crane"),
        environ=release_env(CI_COMMIT_TAG="v0.2.0-yank"),
    )

    created = [call for call in api.release_calls if call["action"] == "create"]
    linked = {link["name"] for link in created[0]["links"]}
    assert release_tools.SCAN_REPORT_FILENAME in linked
    assert release_tools.SCAN_EVALUATION_FILENAME in linked
