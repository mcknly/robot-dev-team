#!/usr/bin/env python3
"""Robot Dev Team Project
File: scripts/release_tools.py
Description: Validate, publish, and reconcile protected stable image releases.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PACKAGE_NAME = "robot-dev-team-release"
# The build side stages one SBOM per published image digest. It is a separate package so the
# version list of PACKAGE_NAME keeps meaning "the set of releases" -- release enumeration reads
# that list on every publish and yank.
SBOM_PACKAGE_NAME = "robot-dev-team-sbom"
SBOM_FILENAME = "sbom.spdx.json"
# The scan evidence is staged per image digest for the same reason the SBOM is: the release
# package's version list is what alias planning walks, and it must keep meaning "the set of
# releases". `publish_release()` copies the staged bytes into the version-scoped package.
SCAN_PACKAGE_NAME = "robot-dev-team-scan"
SCAN_REPORT_FILENAME = "vulnerability-report.json"
SCAN_EVALUATION_FILENAME = "vulnerability-evaluation.json"
SCAN_EVALUATION_SCHEMA = 1
SCAN_POLICY_VERSION = 1
EXCEPTIONS_PATH = Path("security/vulnerability-exceptions.yaml")
# Day-one blocking rule (#50): a High or Critical finding blocks when a fix exists. Unfixed and
# wont-fix High/Critical are recorded and do not block; tightening is tracked with a date in #60.
# This is deliberately not `--only-fixed` on the scanner -- the report keeps every match and only
# the blocking decision reads fix state, so the evidence for #60 survives in the durable record.
BLOCKING_SEVERITIES = ("Critical", "High")
BLOCKING_FIX_STATES = ("fixed",)
MAX_DB_BUILT_AGE = dt.timedelta(hours=48)
# Grype reports that an EOL distro's vulnerability data "may be incomplete or outdated". That is
# the one failure mode a CVE gate cannot detect on its own, so it is recorded in the durable
# evaluation rather than swallowed. Derived from the report's own distro block instead of scraped
# from stderr, so it is deterministic and testable. The runtime left Debian 12 in #59; the entry
# stays because the table is knowledge about distributions, not a description of what this image
# currently ships. Reports are digest-keyed evidence that outlives the image they describe -- a
# yank or a forensic re-evaluation can hand this evaluator a report against an older release, and
# an entry deleted the moment it stopped matching would make that report read as clean.
EOL_DISTRO_RELEASES = {
    ("debian", "12"): (
        "Debian 12 (bookworm) is end-of-life; the runtime migrated to Debian 13 (trixie) in #59"
    ),
}
IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
STABLE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
YANK_TAG = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-yank$"
)
CHANGELOG_HEADING = re.compile(
    r"^## \[v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\] - "
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})$"
)
TOOL_RELEASES = {
    "crane": {
        "version": "0.21.7",
        "url": (
            "https://github.com/google/go-containerregistry/releases/download/"
            "v0.21.7/go-containerregistry_Linux_x86_64.tar.gz"
        ),
        "sha256": "1a57bc98207fa1c0d04bf760699099e26f8383499bfd55b99c1b919a928a7230",
    },
    # The Grype pin is not only a supply-chain control: it is what makes the blocking threshold
    # reproducible. Go matching changed materially between 0.106 and 0.116 (symbol reachability),
    # which moved this image's High/Critical count by more than any dependency bump has. Treat a
    # bump as a change that requires re-measuring the residue, not a routine update. Also
    # re-verify the resolved `registry.auth` YAML shape enforced by
    # `validate_grype_registry_auth()`; that output is version-specific.
    "grype": {
        "version": "0.116.1",
        "url": (
            "https://github.com/anchore/grype/releases/download/"
            "v0.116.1/grype_0.116.1_linux_amd64.tar.gz"
        ),
        "sha256": "0122df7b655981abe547ad3d2190d65551dac6a2bfc80b4dc2a989b5d0587458",
    },
}
# `install-tools` installs every pinned CLI by default. The yank path deliberately installs only
# crane: withdrawal must stay available when Grype's release asset or vulnerability database is
# unreachable, which is exactly the outage during which a bad release most needs pulling.
YANK_TOOLS = ("crane",)
DOCKERHUB_REGISTRY = "index.docker.io"
# Crane resolves docker.io to index.docker.io for credentials; the docker.io form is what the
# evidence records and what docs/RELEASING.md and #52 compare by hand.
DOCKERHUB_REPOSITORY = "docker.io/mcknly/robot-dev-team"
DOCKERHUB_PROBE_TAG_PREFIX = "ci-credential-probe-"


class ReleaseError(RuntimeError):
    """A release contract or publication failure."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> Version:
        match = re.fullmatch(
            r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
            value,
        )
        if match is None:
            raise ReleaseError(f"not a stable SemVer version: {value}")
        return cls(*(int(part) for part in match.groups()))

    @classmethod
    def from_tag(cls, tag: str) -> Version:
        match = STABLE_TAG.fullmatch(tag)
        if match is None:
            raise ReleaseError(f"release tag must match vX.Y.Z with no prerelease suffix: {tag}")
        return cls(*(int(part) for part in match.groups()))

    @classmethod
    def from_yank_tag(cls, tag: str) -> Version:
        match = YANK_TAG.fullmatch(tag)
        if match is None:
            raise ReleaseError(f"yank tag must match vX.Y.Z-yank: {tag}")
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def aliases(self) -> tuple[str, str, str]:
        return (f"{self.major}.{self.minor}", str(self.major), "latest")


def require_env(names: Iterable[str], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    values: dict[str, str] = {}
    for name in names:
        value = source.get(name, "")
        if not value:
            raise ReleaseError(f"{name} is required")
        values[name] = value
    return values


def run(
    arguments: Sequence[str],
    *,
    input_text: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(arguments),
        input=input_text,
        capture_output=True,
        text=True,
        env=None if environ is None else dict(environ),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ReleaseError(f"{' '.join(arguments[:3])} failed: {detail}")
    return result


def validate_protected_annotated_tag(env: Mapping[str, str]) -> str:
    if env["CI_COMMIT_REF_PROTECTED"] != "true":
        raise ReleaseError("release tags must be protected")
    tag_ref = f"refs/tags/{env['CI_COMMIT_TAG']}"
    tag_type = run(("git", "cat-file", "-t", tag_ref)).stdout.strip()
    if tag_type != "tag":
        raise ReleaseError(f"{env['CI_COMMIT_TAG']} must be an annotated Git tag")
    return tag_ref


def validate_default_branch_ancestry(env: Mapping[str, str]) -> None:
    default_ref = f"origin/{env['CI_DEFAULT_BRANCH']}"
    run(("git", "rev-parse", "--verify", default_ref))
    ancestry = subprocess.run(
        ("git", "merge-base", "--is-ancestor", env["CI_COMMIT_SHA"], default_ref),
        capture_output=True,
        text=True,
    )
    if ancestry.returncode == 1:
        raise ReleaseError(
            f"tagged commit {env['CI_COMMIT_SHA']} is not contained in {default_ref}"
        )
    if ancestry.returncode != 0:
        raise ReleaseError(ancestry.stderr.strip() or "could not verify default-branch ancestry")


def annotated_tag_message(tag_ref: str) -> str:
    message = run(("git", "for-each-ref", "--format=%(contents)", tag_ref)).stdout.strip()
    if not message:
        raise ReleaseError("yank tag annotation must contain the withdrawal reason")
    return message


def project_version(path: Path) -> Version:
    import tomllib

    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        value = data["project"]["version"]
    except (FileNotFoundError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"could not read [project].version from {path}: {exc}") from exc
    if not isinstance(value, str):
        raise ReleaseError(f"[project].version in {path} must be a string")
    return Version.parse(value)


def changelog_excerpt(path: Path, version: Version) -> tuple[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseError(f"could not read {path}: {exc}") from exc

    expected = f"v{version}"
    start: int | None = None
    release_date = ""
    for index, line in enumerate(lines):
        match = CHANGELOG_HEADING.fullmatch(line)
        if match is None:
            continue
        found = Version(*(int(part) for part in match.groups()[:3]))
        if found != version:
            continue
        try:
            dt.date.fromisoformat(match.group(4))
        except ValueError as exc:
            raise ReleaseError(f"invalid changelog date for {expected}: {match.group(4)}") from exc
        start = index
        release_date = match.group(4)
        break

    if start is None:
        raise ReleaseError(f"{path} must contain a dated '## [{expected}] - YYYY-MM-DD' section")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    excerpt = "\n".join(lines[start:end]).strip() + "\n"
    return excerpt, release_date


def validate_release(
    *,
    pyproject_path: Path,
    changelog_path: Path,
    output_path: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = require_env(
        (
            "CI_COMMIT_TAG",
            "CI_COMMIT_SHA",
            "CI_DEFAULT_BRANCH",
            "CI_COMMIT_REF_PROTECTED",
            "CI_REGISTRY_IMAGE",
        ),
        environ,
    )
    if YANK_TAG.fullmatch(env["CI_COMMIT_TAG"]):
        tag_ref = validate_protected_annotated_tag(env)
        validate_default_branch_ancestry(env)
        yanked = Version.from_yank_tag(env["CI_COMMIT_TAG"])
        context = {
            "schema_version": 1,
            "operation": "yank",
            "yanked_version": str(yanked),
            "git_tag": env["CI_COMMIT_TAG"],
            "source_commit": env["CI_COMMIT_SHA"],
            "reason": annotated_tag_message(tag_ref),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(context, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return context

    version = Version.from_tag(env["CI_COMMIT_TAG"])
    declared = project_version(pyproject_path)
    if declared != version:
        raise ReleaseError(
            f"tag {env['CI_COMMIT_TAG']} disagrees with [project].version {declared}"
        )
    validate_protected_annotated_tag(env)
    validate_default_branch_ancestry(env)

    excerpt, release_date = changelog_excerpt(changelog_path, version)
    context = {
        "schema_version": 1,
        "operation": "publish",
        "release_version": str(version),
        "git_tag": env["CI_COMMIT_TAG"],
        "source_commit": env["CI_COMMIT_SHA"],
        "source_image": f"{env['CI_REGISTRY_IMAGE']}:{env['CI_COMMIT_SHA']}",
        "release_date": release_date,
        "changelog": excerpt,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(context, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return context


class GitLabApi:
    def __init__(self, base_url: str, project_id: str, job_token: str):
        self.base_url = base_url.rstrip("/")
        self.project_id = urllib.parse.quote(project_id, safe="")
        self.job_token = job_token

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        allow_missing: bool = False,
    ) -> bytes | None:
        headers = {"JOB-TOKEN": self.job_token}
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            f"{self.base_url}/projects/{self.project_id}{path}",
            data=body,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw: bytes = response.read()
                return raw
        except urllib.error.HTTPError as exc:
            if allow_missing and exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", errors="replace")
            raise ReleaseError(f"GitLab API {method} {path} failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise ReleaseError(f"GitLab API {method} {path} failed: {exc}") from exc

    def current_job_user(self) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/job",
            method="GET",
            headers={"JOB-TOKEN": self.job_token},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ReleaseError(f"GitLab current-job lookup failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise ReleaseError(f"GitLab current-job lookup failed: {exc}") from exc
        job = json_object(raw, "GitLab current job")
        user = job.get("user")
        if not isinstance(user, dict):
            raise ReleaseError("GitLab current job did not identify its user")
        if not isinstance(user.get("id"), int) or not isinstance(user.get("username"), str):
            raise ReleaseError("GitLab current job returned an invalid user identity")
        return user

    def package_path(
        self,
        version: str,
        filename: str,
        *,
        package_name: str = PACKAGE_NAME,
    ) -> str:
        parts = (
            urllib.parse.quote(package_name, safe=""),
            urllib.parse.quote(version, safe=""),
            urllib.parse.quote(filename, safe=""),
        )
        return f"/packages/generic/{parts[0]}/{parts[1]}/{parts[2]}"

    def package_file(
        self,
        version: str,
        filename: str,
        *,
        package_name: str = PACKAGE_NAME,
    ) -> bytes | None:
        return self.request(
            "GET",
            self.package_path(version, filename, package_name=package_name),
            allow_missing=True,
        )

    def upload_package_file(
        self,
        version: str,
        filename: str,
        content: bytes,
        *,
        package_name: str = PACKAGE_NAME,
    ) -> None:
        existing = self.package_file(version, filename, package_name=package_name)
        if existing is not None:
            if existing != content:
                raise ReleaseError(
                    f"durable release file already exists with different content: "
                    f"{package_name}/{version}/{filename}"
                )
            return
        self.request(
            "PUT",
            self.package_path(version, filename, package_name=package_name),
            body=content,
        )

    def package_versions(self) -> list[str]:
        # Deliberately scoped to PACKAGE_NAME: this list is what release enumeration walks, and
        # the digest-addressed SBOM package must never appear in it. GitLab's package_name
        # filter is a substring search, not an exact match, so the returned name is checked
        # rather than trusted -- a future `robot-dev-team-release-*` package would otherwise
        # match the query and inject a phantom version into alias planning.
        versions: set[str] = set()
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {
                    "package_type": "generic",
                    "package_name": PACKAGE_NAME,
                    "per_page": "100",
                    "page": str(page),
                }
            )
            raw = self.request("GET", f"/packages?{query}")
            assert raw is not None
            try:
                records = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ReleaseError("GitLab package list returned invalid JSON") from exc
            if not isinstance(records, list):
                raise ReleaseError("GitLab package list did not return an array")
            for record in records:
                if not isinstance(record, dict) or record.get("name") != PACKAGE_NAME:
                    continue
                if isinstance(record.get("version"), str):
                    versions.add(record["version"])
            if len(records) < 100:
                break
            page += 1
        return sorted(versions)

    def asset_url(
        self,
        version: str,
        filename: str,
        *,
        package_name: str = PACKAGE_NAME,
    ) -> str:
        path = self.package_path(version, filename, package_name=package_name)
        return f"{self.base_url}/projects/{self.project_id}{path}"

    def release_path(self, tag: str) -> str:
        return f"/releases/{urllib.parse.quote(tag, safe='')}"

    def release(self, tag: str) -> dict[str, Any] | None:
        raw = self.request("GET", self.release_path(tag), allow_missing=True)
        if raw is None:
            # A project the token may not read releases on answers 404 exactly like a tag
            # that has no release, so treating the 404 as "nothing published" would fail
            # open. Reading the collection separates the two: it succeeds when releases are
            # readable and raises otherwise, before anything has been mutated.
            self.request("GET", "/releases?per_page=1")
            return None
        return json_object(raw, f"GitLab release {tag}")

    def json_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw = self.request(
            method,
            path,
            body=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )
        assert raw is not None
        return json_object(raw, f"GitLab API {method} {path} response")

    def create_release(
        self,
        *,
        tag: str,
        name: str,
        description: str,
        links: Sequence[Mapping[str, str]] = (),
    ) -> dict[str, Any]:
        # No `ref` is sent: the release job only ever runs on a protected tag that already
        # exists, and omitting it means the API can attach the release to that tag but can
        # never create a tag of its own.
        payload: dict[str, Any] = {
            "tag_name": tag,
            "name": name,
            "description": description,
        }
        if links:
            payload["assets"] = {"links": [dict(link) for link in links]}
        return self.json_request("POST", "/releases", payload)

    def update_release(self, *, tag: str, name: str, description: str) -> dict[str, Any]:
        return self.json_request(
            "PUT",
            self.release_path(tag),
            {"name": name, "description": description},
        )

    def add_release_link(self, *, tag: str, link: Mapping[str, str]) -> dict[str, Any]:
        return self.json_request(
            "POST",
            f"{self.release_path(tag)}/assets/links",
            dict(link),
        )


def json_object(raw: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{description} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"{description} must be a JSON object")
    return value


def successful_releases(api: GitLabApi) -> dict[Version, dict[str, Any]]:
    releases: dict[Version, dict[str, Any]] = {}
    for value in api.package_versions():
        try:
            version = Version.parse(value)
        except ReleaseError:
            continue
        raw = api.package_file(value, "release-manifest.json")
        if raw is None or api.package_file(value, "yank-record.json") is not None:
            continue
        try:
            manifest = json_object(raw, f"release manifest {value}")
            validate_release_manifest(manifest, version)
        except ReleaseError as exc:
            print(f"[release] WARNING: ignoring invalid release record {value}: {exc}", file=sys.stderr)
            continue
        releases[version] = manifest
    return releases


def non_yanked_release_versions(api: GitLabApi) -> set[Version]:
    versions: set[Version] = set()
    for value in api.package_versions():
        try:
            version = Version.parse(value)
        except ReleaseError:
            continue
        if api.package_file(value, "yank-record.json") is None:
            versions.add(version)
    return versions


def validate_release_manifest(manifest: Mapping[str, Any], version: Version) -> None:
    if manifest.get("release_version") != str(version):
        raise ReleaseError(f"release manifest version mismatch for {version}")
    if manifest.get("git_tag") != f"v{version}":
        raise ReleaseError(f"release manifest Git tag mismatch for {version}")
    source_commit = manifest.get("source_commit")
    if not isinstance(source_commit, str) or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ReleaseError(f"release {version} has an invalid source commit")
    validated_manifest_digest(manifest, version)


def desired_moving_aliases(candidate: Version, released: Iterable[Version]) -> tuple[str, ...]:
    versions = set(released)
    versions.add(candidate)
    aliases: list[str] = []
    if candidate == max(
        v for v in versions if (v.major, v.minor) == (candidate.major, candidate.minor)
    ):
        aliases.append(f"{candidate.major}.{candidate.minor}")
    if candidate == max(v for v in versions if v.major == candidate.major):
        aliases.append(str(candidate.major))
    if candidate == max(versions):
        aliases.append("latest")
    return tuple(aliases)


def crane_digest(crane: Path, reference: str) -> str:
    digest = run((str(crane), "digest", reference)).stdout.strip()
    if IMAGE_DIGEST.fullmatch(digest) is None:
        raise ReleaseError(
            f"registry returned an invalid manifest digest for {reference}: {digest}"
        )
    return digest


def crane_digest_if_exists(crane: Path, reference: str) -> str | None:
    try:
        return crane_digest(crane, reference)
    except ReleaseError as exc:
        detail = str(exc).lower()
        missing_markers = (
            "manifest unknown",
            "name unknown",
            "status code 404",
            "404 not found",
        )
        if any(marker in detail for marker in missing_markers):
            return None
        raise


def crane_login(crane: Path, registry: str, username: str, password: str) -> None:
    run(
        (str(crane), "auth", "login", registry, "--username", username, "--password-stdin"),
        input_text=password,
    )


def probe_dockerhub_credentials(
    *,
    crane: Path,
    artifacts_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Copy one qualified main image to a disposable Docker Hub tag and verify it."""
    env = require_env(
        (
            "CI_PIPELINE_SOURCE",
            "CI_COMMIT_BRANCH",
            "CI_DEFAULT_BRANCH",
            "CI_COMMIT_REF_PROTECTED",
            "CI_COMMIT_SHA",
            "CI_PIPELINE_ID",
            "CI_PIPELINE_URL",
            "CI_JOB_URL",
            "CI_REGISTRY",
            "CI_REGISTRY_IMAGE",
            "CI_REGISTRY_USER",
            "CI_REGISTRY_PASSWORD",
            "IMAGE_PUBLISHED",
            "IMAGE_DIGEST",
            "DOCKERHUB_USERNAME",
            "DOCKERHUB_TOKEN",
        ),
        environ,
    )
    if (
        env["CI_PIPELINE_SOURCE"] != "push"
        or env["CI_COMMIT_BRANCH"] != env["CI_DEFAULT_BRANCH"]
        or env["CI_COMMIT_REF_PROTECTED"] != "true"
    ):
        raise ReleaseError("Docker Hub credential probes require a protected default-branch push")
    if re.fullmatch(r"[0-9a-f]{40}", env["CI_COMMIT_SHA"]) is None:
        raise ReleaseError(f"CI_COMMIT_SHA is not a full commit SHA: {env['CI_COMMIT_SHA']}")
    if re.fullmatch(r"[1-9][0-9]*", env["CI_PIPELINE_ID"]) is None:
        raise ReleaseError(f"CI_PIPELINE_ID is not a positive integer: {env['CI_PIPELINE_ID']}")
    if env["IMAGE_PUBLISHED"] != "true":
        raise ReleaseError("the source image was not published by this pipeline")
    source_digest = env["IMAGE_DIGEST"]
    if IMAGE_DIGEST.fullmatch(source_digest) is None:
        raise ReleaseError(f"IMAGE_DIGEST is not a manifest digest: {source_digest}")

    crane_login(crane, env["CI_REGISTRY"], env["CI_REGISTRY_USER"], env["CI_REGISTRY_PASSWORD"])
    crane_login(crane, DOCKERHUB_REGISTRY, env["DOCKERHUB_USERNAME"], env["DOCKERHUB_TOKEN"])

    source_reference = f"{env['CI_REGISTRY_IMAGE']}@{source_digest}"
    resolved_source = crane_digest(crane, source_reference)
    if resolved_source != source_digest:
        raise ReleaseError(
            f"source image {source_reference} resolves to {resolved_source}, not {source_digest}"
        )

    probe_tag = f"{DOCKERHUB_PROBE_TAG_PREFIX}{env['CI_PIPELINE_ID']}"
    destination_reference = f"{DOCKERHUB_REPOSITORY}:{probe_tag}"
    existing = crane_digest_if_exists(crane, destination_reference)
    # Even a matching tag is rejected: reading a public tag needs no push right and `crane auth
    # login` never contacts the registry, so only a write to an absent tag proves the token.
    if existing is not None:
        raise ReleaseError(
            f"Docker Hub probe tag {destination_reference} already exists at {existing}; "
            "delete it in Docker Hub and retry, or probe from a newer main pipeline"
        )
    print(f"[release] Docker Hub probe cleanup target: {destination_reference}")
    sys.stdout.flush()
    run((str(crane), "copy", source_reference, destination_reference))

    destination_digest = crane_digest(crane, destination_reference)
    if destination_digest != source_digest:
        raise ReleaseError(
            f"Docker Hub probe digest mismatch: {destination_reference} resolves to "
            f"{destination_digest}, not {source_digest}"
        )

    evidence = {
        "schema_version": 1,
        "operation": "dockerhub-credential-probe",
        "source_commit": env["CI_COMMIT_SHA"],
        "source_reference": source_reference,
        "source_digest": source_digest,
        "destination_reference": destination_reference,
        "destination_digest": destination_digest,
        "pipeline_id": int(env["CI_PIPELINE_ID"]),
        "pipeline_url": env["CI_PIPELINE_URL"],
        "job_url": env["CI_JOB_URL"],
        "cleanup_required": True,
    }
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "dockerhub-probe.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return evidence


def apply_alias(
    crane: Path,
    repository: str,
    source_digest: str,
    alias: str,
    *,
    immutable: bool,
) -> dict[str, str]:
    destination = f"{repository}:{alias}"
    existing = crane_digest_if_exists(crane, destination)
    if immutable and existing is not None and existing != source_digest:
        raise ReleaseError(
            f"immutable release alias {destination} already points to {existing}, "
            f"not {source_digest}"
        )
    if existing != source_digest:
        run((str(crane), "tag", f"{repository}@{source_digest}", alias))
    verified = crane_digest(crane, destination)
    if verified != source_digest:
        raise ReleaseError(f"alias verification failed for {destination}: {verified}")
    return {
        "name": alias,
        "reference": destination,
        "digest": verified,
        "kind": "immutable" if immutable else "moving",
    }


def load_context(path: Path) -> dict[str, Any]:
    try:
        context = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"could not read validated release context {path}: {exc}") from exc
    if not isinstance(context, dict):
        raise ReleaseError("validated release context must be a JSON object")
    return context


def gitlab_api_from_env(
    environ: Mapping[str, str] | None = None,
) -> tuple[GitLabApi, dict[str, str]]:
    env = require_env(("CI_API_V4_URL", "CI_PROJECT_ID", "CI_JOB_TOKEN"), environ)
    return GitLabApi(env["CI_API_V4_URL"], env["CI_PROJECT_ID"], env["CI_JOB_TOKEN"]), env


def digest_package_version(digest: str) -> str:
    """Map an image manifest digest onto its Generic Package version.

    GitLab rejects `sha256:<hex>` as a package version but accepts the hyphenated form, so the
    digest binding lives in the version rather than the filename.
    """
    if IMAGE_DIGEST.fullmatch(digest) is None:
        raise ReleaseError(f"not a valid image manifest digest: {digest}")
    return digest.replace(":", "-", 1)


def validate_sbom_document(raw: bytes, description: str, *, expected_name: str) -> None:
    """Check that bytes are the SPDX document the build side is supposed to have produced.

    `expected_name` binds the document's content to the image it is filed under. The package
    path alone does not: the documented manual-recovery upload is unconstrained, so without
    this check any SPDX file could be staged for any digest. `scripts/generate-sbom.sh` passes
    `--source-name "$image_ref"`, which puts `${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHA}` in the
    document's top-level `name` -- the same string both ends of this path derive independently.

    The content is never normalized or regenerated: the release copy must be the exact bytes
    scanned from the smoke-tested image, and the durable-file content check compares them byte
    for byte on every retry.
    """
    document = json_object(raw, description)
    if document.get("spdxVersion") != "SPDX-2.3":
        raise ReleaseError(f"{description} is not an SPDX-2.3 document")
    identifier = document.get("SPDXID")
    if not isinstance(identifier, str) or not identifier:
        raise ReleaseError(f"{description} carries no document SPDXID")
    name = document.get("name")
    if name != expected_name:
        raise ReleaseError(f"{description} describes {name!r}, not {expected_name!r}")


def release_sbom_for_digest(api: GitLabApi, digest: str, *, expected_name: str) -> bytes:
    version = digest_package_version(digest)
    raw = api.package_file(version, SBOM_FILENAME, package_name=SBOM_PACKAGE_NAME)
    if raw is None:
        raise ReleaseError(
            f"no durable SBOM is published for {digest}: expected "
            f"{SBOM_PACKAGE_NAME}/{version}/{SBOM_FILENAME} (see docs/RELEASING.md)"
        )
    validate_sbom_document(raw, f"durable SBOM for {digest}", expected_name=expected_name)
    return raw


def publish_sbom(
    *,
    sbom_path: Path,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Stage the built SBOM under its image digest so a later tag pipeline can reach it.

    Runs on the protected default branch after the image is published. Nothing is derived from
    the SBOM here and nothing is rewritten -- the exact artifact bytes are uploaded, so a retry
    is a no-op against the content check rather than a conflict.
    """
    source_env = os.environ if environ is None else environ
    env = require_env(
        ("IMAGE_PUBLISHED", "IMAGE_DIGEST", "CI_REGISTRY_IMAGE", "CI_COMMIT_SHA"), source_env
    )
    if env["IMAGE_PUBLISHED"] != "true":
        raise ReleaseError("refusing to publish an SBOM for an image that was not published")
    version = digest_package_version(env["IMAGE_DIGEST"])
    try:
        raw = sbom_path.read_bytes()
    except OSError as exc:
        raise ReleaseError(f"could not read the built SBOM {sbom_path}: {exc}") from exc
    validate_sbom_document(
        raw,
        f"built SBOM {sbom_path}",
        expected_name=f"{env['CI_REGISTRY_IMAGE']}:{env['CI_COMMIT_SHA']}",
    )

    api, _ = gitlab_api_from_env(source_env)
    api.upload_package_file(version, SBOM_FILENAME, raw, package_name=SBOM_PACKAGE_NAME)
    return version


@dataclass(frozen=True)
class ScanFinding:
    """One scanner match, reduced to the fields the policy reads."""

    identifier: str
    severity: str
    fix_state: str
    package: str
    version: str
    package_type: str

    @property
    def artifact(self) -> tuple[str, str, str]:
        return (self.package, self.version, self.package_type)

    @property
    def blocks(self) -> bool:
        return self.severity in BLOCKING_SEVERITIES and self.fix_state in BLOCKING_FIX_STATES

    def record(self) -> dict[str, str]:
        return {
            "id": self.identifier,
            "severity": self.severity,
            "fix_state": self.fix_state,
            "package": self.package,
            "version": self.version,
            "type": self.package_type,
        }


@dataclass(frozen=True)
class ScanException:
    """One reviewed exception entry.

    Two forms, both scoped to an exact package + version + type. The `exact` form names one
    vulnerability id. The `class` form names a root cause instead, and exists because a single
    remediation can produce dozens of ids -- but it is unbounded over ids for that artifact, so
    it silently absorbs anything new disclosed against the same build. Prefer `exact` unless the
    package is on its way out of the image (#58's gosu case, not #66's CPython case).
    """

    kind: str
    identifier: str
    class_name: str
    package: str
    version: str
    package_type: str
    owner: str
    rationale: str
    expires: dt.date
    tracking_issue: str

    @property
    def artifact_scope(self) -> tuple[str, str, str]:
        return (self.package, self.version, self.package_type)

    @property
    def label(self) -> str:
        scope = f"{self.package}@{self.version} ({self.package_type})"
        if self.kind == "class":
            return f"class {self.class_name} on {scope}"
        return f"{self.identifier} on {scope}"

    def covers(self, finding: ScanFinding) -> bool:
        if finding.artifact != (self.package, self.version, self.package_type):
            return False
        return self.kind == "class" or finding.identifier == self.identifier

    def record(self, absorbed: Sequence[str]) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.identifier,
            "class": self.class_name,
            "package": self.package,
            "version": self.version,
            "type": self.package_type,
            "owner": self.owner,
            "rationale": self.rationale,
            "expires": self.expires.isoformat(),
            "tracking_issue": self.tracking_issue,
            # Every id a class absorbed is listed so the blast radius stays visible in the
            # durable record rather than hiding behind one line of prose.
            "absorbed": list(absorbed),
        }


EXCEPTION_COMMON_KEYS = frozenset(
    {"package", "version", "type", "owner", "rationale", "expires", "tracking_issue"}
)
EXCEPTION_WILDCARDS = ("*", "?")


def _exception_text(
    entry: Mapping[str, Any],
    field: str,
    where: str,
    *,
    scoped: bool = False,
) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ReleaseError(f"{where}: {field} is required and must be a non-empty string")
    text = value.strip()
    # Only the matching fields are wildcard-checked. Prose fields are free text -- rejecting a
    # question mark in a rationale would be theatre, and would push authors towards shorter
    # justifications, which is the opposite of what this file is for.
    if scoped and any(marker in text for marker in EXCEPTION_WILDCARDS):
        raise ReleaseError(f"{where}: {field} must not contain a wildcard: {text!r}")
    return text


def _exception_date(entry: Mapping[str, Any], where: str) -> dt.date:
    value = entry.get("expires")
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ReleaseError(f"{where}: expires is required and must be a UTC date")
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ReleaseError(f"{where}: expires must be an ISO date (YYYY-MM-DD)") from exc


def parse_exception_entry(entry: Any, index: int) -> ScanException:
    where = f"exception {index}"
    if not isinstance(entry, Mapping):
        raise ReleaseError(f"{where}: each exception must be a mapping")
    has_id = "id" in entry
    has_class = "class" in entry
    if has_id == has_class:
        raise ReleaseError(f"{where}: name exactly one of id or class")
    allowed = EXCEPTION_COMMON_KEYS | {"id" if has_id else "class"}
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ReleaseError(f"{where}: unknown field(s) {', '.join(unknown)}")
    # A bare vulnerability id with no artifact scope is exactly what the exception file must not
    # accept: it would follow the id onto any future package or version.
    scope = {
        field: _exception_text(entry, field, where, scoped=True)
        for field in ("package", "version", "type")
    }
    identifier = _exception_text(entry, "id", where, scoped=True) if has_id else ""
    class_name = _exception_text(entry, "class", where, scoped=True) if has_class else ""
    # Required on both forms, not only the class form. An accepted finding with no tracking
    # issue has nowhere for its remediation to live, which is how a dated exception quietly
    # becomes a permanent one.
    tracking = _exception_text(entry, "tracking_issue", where)
    return ScanException(
        kind="class" if has_class else "exact",
        identifier=identifier,
        class_name=class_name,
        package=scope["package"],
        version=scope["version"],
        package_type=scope["type"],
        owner=_exception_text(entry, "owner", where),
        rationale=_exception_text(entry, "rationale", where),
        expires=_exception_date(entry, where),
        tracking_issue=tracking,
    )


def parse_exceptions(document: Any) -> tuple[ScanException, ...]:
    if not isinstance(document, Mapping):
        raise ReleaseError("the exception file must be a mapping")
    if document.get("version") != SCAN_POLICY_VERSION:
        raise ReleaseError(
            f"the exception file must declare version: {SCAN_POLICY_VERSION}"
        )
    entries = document.get("exceptions", [])
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise ReleaseError("the exception file's exceptions must be a list")
    return tuple(parse_exception_entry(entry, index) for index, entry in enumerate(entries, 1))


def load_exceptions(path: Path) -> tuple[tuple[ScanException, ...], bytes]:
    """Read and parse the reviewed exception file, returning its parsed form and exact bytes.

    The raw bytes are hashed into the evaluation so the durable record states which revision of
    the policy produced the verdict. An absent file is an empty policy, not an error: the gate
    is meaningful before anyone has written a first exception.

    PyYAML is imported here rather than at module scope on purpose. Both release jobs run a bare
    slim-Python image with no project dependencies installed, and they only ever read the JSON
    evaluation -- so nothing on the release path may import a third-party module.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return (), b""
    except OSError as exc:
        raise ReleaseError(f"could not read {path}: {exc}") from exc
    import yaml

    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ReleaseError(f"{path} is not readable YAML: {exc}") from exc
    return parse_exceptions(document), raw


def policy_fingerprint(exceptions_raw: bytes) -> dict[str, Any]:
    """Describe the policy that produced a verdict, hashed so it cannot be misreported."""
    exceptions_sha = hashlib.sha256(exceptions_raw).hexdigest()
    policy = {
        "version": SCAN_POLICY_VERSION,
        "blocking_severities": list(BLOCKING_SEVERITIES),
        "blocking_fix_states": list(BLOCKING_FIX_STATES),
        "max_db_built_age_hours": int(MAX_DB_BUILT_AGE.total_seconds() // 3600),
        "exceptions_sha256": exceptions_sha,
    }
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    policy["sha256"] = hashlib.sha256(canonical).hexdigest()
    return policy


def scan_report_target(report: Mapping[str, Any]) -> Mapping[str, Any]:
    source = report.get("source")
    if not isinstance(source, Mapping) or source.get("type") != "image":
        raise ReleaseError("scan report does not describe an image source")
    target = source.get("target")
    if not isinstance(target, Mapping):
        raise ReleaseError("scan report carries no readable image target")
    return target


def validate_scan_target(report: Mapping[str, Any], *, digest: str, repository: str) -> None:
    """Bind the report to the digest from inside the document.

    The package path binds nothing on its own -- the documented manual-recovery upload is
    unconstrained -- so any file could otherwise be staged under any digest. Both the reference
    the scanner was pointed at and the manifest it actually resolved are checked, which is also
    what rejects a report produced by scanning a mutable tag.
    """
    target = scan_report_target(report)
    user_input = target.get("userInput")
    if not isinstance(user_input, str) or f"@{digest}" not in user_input:
        raise ReleaseError(
            f"scan report was not taken against {digest}: scanned {user_input!r}. A tag "
            "reference is not acceptable evidence -- tags move, digests do not."
        )
    if target.get("manifestDigest") != digest:
        raise ReleaseError(
            f"scan report resolved {target.get('manifestDigest')!r}, not {digest}"
        )
    repo_digests = target.get("repoDigests")
    if isinstance(repo_digests, list) and repo_digests:
        expected = f"{repository}@{digest}"
        if expected not in repo_digests:
            raise ReleaseError(f"scan report does not carry the repo digest {expected}")


def validate_scan_scanner(report: Mapping[str, Any]) -> Mapping[str, Any]:
    descriptor = report.get("descriptor")
    if not isinstance(descriptor, Mapping):
        raise ReleaseError("scan report carries no scanner descriptor")
    pinned = TOOL_RELEASES["grype"]
    if descriptor.get("name") != "grype":
        raise ReleaseError(f"scan report was not produced by grype: {descriptor.get('name')!r}")
    # The threshold only means something relative to a fixed matcher. Go matching alone moved
    # this image's High/Critical count by more than any dependency bump between two Grype
    # releases, so a report from an unpinned scanner is not comparable evidence.
    if descriptor.get("version") != pinned["version"]:
        raise ReleaseError(
            f"scan report was produced by grype {descriptor.get('version')!r}, "
            f"not the pinned {pinned['version']}"
        )
    return descriptor


def describe_scan_database(descriptor: Mapping[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Read the database metadata the scan ran against, without enforcing its age.

    Read from the report being evaluated rather than from a separate `grype db status` run:
    that is the copy which cannot be swapped for another run's metadata. Structural problems
    still raise -- a report that cannot say which database produced it is unusable in either
    mode. Only the *age* verdict is left to the caller, and it is recorded either way.
    """
    database = descriptor.get("db")
    status = database.get("status") if isinstance(database, Mapping) else None
    if not isinstance(status, Mapping):
        raise ReleaseError("scan report carries no vulnerability database status")
    if status.get("valid") is not True:
        raise ReleaseError("scan ran against a vulnerability database that reported itself invalid")
    built_raw = status.get("built")
    if not isinstance(built_raw, str):
        raise ReleaseError("scan report carries no vulnerability database build time")
    try:
        built = dt.datetime.fromisoformat(built_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseError(f"unreadable vulnerability database build time: {built_raw}") from exc
    if built.tzinfo is None:
        built = built.replace(tzinfo=dt.timezone.utc)
    age = now - built
    return {
        "schema_version": status.get("schemaVersion"),
        "built": built_raw,
        "source": status.get("from"),
        "valid": True,
        "age_hours": round(age.total_seconds() / 3600, 2),
        "age_check": {
            "passed": age <= MAX_DB_BUILT_AGE,
            "max_hours": int(MAX_DB_BUILT_AGE.total_seconds() // 3600),
        },
    }


def validate_scan_database(descriptor: Mapping[str, Any], now: dt.datetime) -> dict[str, Any]:
    """Fail closed on a stale or invalid vulnerability database."""
    database = describe_scan_database(descriptor, now)
    if not database["age_check"]["passed"]:
        raise ReleaseError(
            f"vulnerability database is {database['age_hours']} hours old, older than the "
            f"permitted {int(MAX_DB_BUILT_AGE.total_seconds() // 3600)}. A scan against an old "
            "database is not a passing scan."
        )
    return database


def _match_field(source: Mapping[str, Any], field: str, where: str) -> str:
    """Read one required policy field, failing closed on anything unexpected.

    Coercing with `str()` would be the quiet disaster here: a match with a missing or null
    severity would become `""` and therefore not High or Critical, and a missing fix state
    would read as not-fixable. Malformed scanner output would then produce a *passing* verdict
    -- the one direction a gate must never fail in.
    """
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ReleaseError(f"scan report contains a match with no readable {where}")
    return value.strip()


def _fix_state(fix: Mapping[str, Any]) -> str:
    """Normalize the fix state, treating an empty one as explicitly unknown.

    Grype really does emit `"state": ""` for some matches -- two of them on the current image --
    so an empty string is scanner output rather than corruption, and it means the same thing as
    `unknown`. Recording it as `unknown` keeps it visible in the counts instead of hiding an
    empty key. It does not block either way: the day-one rule blocks on a fix that is known to
    exist, and unknown is not that. A non-string state is still a hard failure.
    """
    state = fix.get("state", "")
    if not isinstance(state, str):
        raise ReleaseError("scan report contains a match with an unreadable fix state")
    return state.strip() or "unknown"


def report_findings(report: Mapping[str, Any]) -> tuple[ScanFinding, ...]:
    matches = report.get("matches")
    if not isinstance(matches, list):
        raise ReleaseError("scan report carries no match list")
    findings: list[ScanFinding] = []
    for match in matches:
        if not isinstance(match, Mapping):
            raise ReleaseError("scan report contains an unreadable match")
        vulnerability = match.get("vulnerability")
        artifact = match.get("artifact")
        if not isinstance(vulnerability, Mapping) or not isinstance(artifact, Mapping):
            raise ReleaseError("scan report contains a match with no vulnerability or artifact")
        fix = vulnerability.get("fix")
        if not isinstance(fix, Mapping):
            raise ReleaseError("scan report contains a match with no fix state")
        findings.append(
            ScanFinding(
                identifier=_match_field(vulnerability, "id", "vulnerability id"),
                severity=_match_field(vulnerability, "severity", "severity"),
                fix_state=_fix_state(fix),
                package=_match_field(artifact, "name", "package name"),
                version=_match_field(artifact, "version", "package version"),
                package_type=_match_field(artifact, "type", "package type"),
            )
        )
    return tuple(findings)


def severity_counts(findings: Sequence[ScanFinding]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for finding in findings:
        by_state = counts.setdefault(finding.severity or "Unknown", {})
        state = finding.fix_state or "unknown"
        by_state[state] = by_state.get(state, 0) + 1
    return counts


def _unused_exception_reason(
    exception: ScanException,
    index: int,
    exceptions: Sequence[ScanException],
    findings: Sequence[ScanFinding],
    versions_by_package: Mapping[tuple[str, str], set[str]],
) -> str:
    installed = versions_by_package.get((exception.package, exception.package_type))
    if installed and exception.version not in installed:
        return (
            f"exception {exception.label} names a version that is no longer installed; "
            f"the scan reports {exception.package} at {', '.join(sorted(installed))}"
        )
    covered = [finding for finding in findings if exception.covers(finding)]
    if not covered:
        return f"exception {exception.label} matches no finding in this report"
    # Only blocking findings are ever absorbed, so only those can be shadowed. Testing the
    # shadow claim over all covered findings would report "already absorbs every finding it
    # covers" about an earlier entry that absorbed only the blocking subset.
    blocking_covered = [finding for finding in covered if finding.blocks]
    # An earlier entry absorbed everything this one covers. Reporting that as "matches only
    # non-blocking findings" would send the reader looking at severities instead of at the
    # duplicate, and the diagnostic is the entire reason this fails hard rather than warning.
    shadow = next(
        (
            other
            for position, other in enumerate(exceptions)
            if position < index and any(other.covers(finding) for finding in blocking_covered)
        ),
        None,
    )
    if shadow is not None:
        return (
            f"exception {exception.label} is shadowed by the earlier entry {shadow.label}, "
            "which already absorbs every finding it covers; remove one of them"
        )
    return (
        f"exception {exception.label} matches only findings that do not block, so it "
        "grants nothing; remove it rather than carrying an entry that suppresses nothing"
    )


def check_exception_hygiene(
    exceptions: Sequence[ScanException],
    absorbed: Mapping[int, list[str]],
    findings: Sequence[ScanFinding],
    today: dt.date,
) -> None:
    """Reject expired and unused entries so the file cannot silently rot.

    An entry that matches nothing is a hard failure rather than a warning: the common way an
    exception file becomes a rubber stamp is that entries outlive the findings that justified
    them, and nobody notices because nothing fails.
    """
    versions_by_package: dict[tuple[str, str], set[str]] = {}
    for finding in findings:
        versions_by_package.setdefault((finding.package, finding.package_type), set()).add(
            finding.version
        )
    for index, exception in enumerate(exceptions):
        if exception.expires < today:
            raise ReleaseError(
                f"exception {exception.label} expired on {exception.expires.isoformat()}"
            )
        if absorbed[index]:
            continue
        raise ReleaseError(
            _unused_exception_reason(exception, index, exceptions, findings, versions_by_package)
        )


def apply_exceptions(
    findings: Sequence[ScanFinding],
    exceptions: Sequence[ScanException],
) -> tuple[dict[int, list[str]], list[ScanFinding]]:
    absorbed: dict[int, list[str]] = {index: [] for index in range(len(exceptions))}
    blocking: list[ScanFinding] = []
    for finding in findings:
        if not finding.blocks:
            continue
        covering = next(
            (index for index, entry in enumerate(exceptions) if entry.covers(finding)),
            None,
        )
        if covering is None:
            blocking.append(finding)
        else:
            absorbed[covering].append(finding.identifier)
    return absorbed, blocking


def eol_distro_note(report: Mapping[str, Any]) -> dict[str, Any]:
    distro = report.get("distro")
    if not isinstance(distro, Mapping):
        return {"name": None, "version": None, "end_of_life": False, "note": None}
    name = str(distro.get("name", ""))
    version = str(distro.get("version", ""))
    note = EOL_DISTRO_RELEASES.get((name, version.split(".")[0]))
    return {
        "name": name or None,
        "version": version or None,
        "end_of_life": note is not None,
        "note": note,
    }


def evaluate_scan(
    report: Mapping[str, Any],
    *,
    digest: str,
    repository: str,
    commit: str,
    exceptions: Sequence[ScanException],
    exceptions_raw: bytes,
    now: dt.datetime,
    report_sha256: str,
    enforce_evidence: bool = True,
) -> dict[str, Any]:
    """Apply the release policy to a scan report and produce the durable evaluation record.

    Structural problems -- an unbound report, a stale database, an expired or unused exception --
    raise, because they mean the evidence itself cannot be trusted. Findings that merely violate
    the threshold do not raise: they are recorded and set the verdict, so the durable record
    explains a failure instead of only asserting one.

    `enforce_evidence` is false only on the offline `evaluate` path, which asks "does the policy
    still hold" about a report already in hand. Both checks it turns off are properties of the
    *evidence* rather than of the policy: the digest binding (a locally produced
    `docker-archive` report carries no registry reference) and the database age (the reports the
    documented workflow points at are default-branch artifacts kept for 30 days, so they are
    routinely older than the 48-hour release limit).

    Turning them off is recorded in the document, not just in the caller: `policy_only` is set
    and the database age check records `passed: false`. `validate_scan_evaluation()` rejects
    both, so a policy-only document that someone stages by hand can never be read as release
    evidence.
    """
    if enforce_evidence:
        validate_scan_target(report, digest=digest, repository=repository)
    descriptor = validate_scan_scanner(report)
    database = (
        validate_scan_database(descriptor, now)
        if enforce_evidence
        else describe_scan_database(descriptor, now)
    )
    findings = report_findings(report)
    absorbed, blocking = apply_exceptions(findings, exceptions)
    check_exception_hygiene(exceptions, absorbed, findings, now.date())
    unfixed = [
        finding.record()
        for finding in findings
        if finding.severity in BLOCKING_SEVERITIES and finding.fix_state not in BLOCKING_FIX_STATES
    ]
    return {
        "schema_version": SCAN_EVALUATION_SCHEMA,
        "verdict": "fail" if blocking else "pass",
        # A policy-only document answers a narrower question than a release needs. Recorded in
        # the document so the answer travels with it rather than living in whoever ran it.
        "policy_only": not enforce_evidence,
        "image_digest": digest,
        "image_repository": repository,
        "source_commit": commit,
        "evaluated_at": now.replace(microsecond=0).isoformat(),
        # Binds the verdict to the exact report it was computed from. The two documents are
        # staged and published separately, and the report is the one a reader opens -- without
        # this, a manually staged report that disagrees with its evaluation would be linked
        # from the release as the evidence, with nothing detecting the mismatch.
        "report_sha256": report_sha256,
        "scanner": {
            "name": "grype",
            "version": TOOL_RELEASES["grype"]["version"],
            "archive_sha256": TOOL_RELEASES["grype"]["sha256"],
        },
        "policy": policy_fingerprint(exceptions_raw),
        "database": database,
        "distro": eol_distro_note(report),
        "counts": {
            "total": len(findings),
            "by_severity_and_fix_state": severity_counts(findings),
            "blocking": len(blocking),
            "excepted": sum(len(ids) for ids in absorbed.values()),
            "unfixed_high_or_critical": len(unfixed),
        },
        "blocking_findings": [finding.record() for finding in blocking],
        # Recorded, never removed from the report: the evidence for tightening the rule in #60
        # has to survive the release that accepted it.
        "unfixed_high_or_critical": unfixed,
        "exceptions_applied": [
            exception.record(absorbed[index]) for index, exception in enumerate(exceptions)
        ],
    }


# How long before an exception's expiry date the summary starts warning. The rule itself stays
# a hard failure at midnight; this only removes the surprise, since the first notice would
# otherwise be a red pipeline on every branch and tag at once.
EXCEPTION_EXPIRY_WARNING = dt.timedelta(days=21)


def expiring_exceptions(evaluation: Mapping[str, Any], today: dt.date) -> list[str]:
    warnings: list[str] = []
    for record in evaluation.get("exceptions_applied", []):
        if not isinstance(record, Mapping):
            continue
        try:
            expires = dt.date.fromisoformat(str(record.get("expires", "")))
        except ValueError:
            continue
        remaining = expires - today
        if remaining <= EXCEPTION_EXPIRY_WARNING:
            label = record.get("id") or record.get("class")
            warnings.append(
                f"exception {label} on {record.get('package')}@{record.get('version')} expires "
                f"in {remaining.days} day(s) on {expires.isoformat()} "
                f"({record.get('tracking_issue')})"
            )
    return warnings


def summarize_evaluation(
    evaluation: Mapping[str, Any],
    *,
    today: dt.date | None = None,
) -> str:
    counts = evaluation.get("counts", {})
    database = evaluation.get("database", {})
    distro = evaluation.get("distro", {})
    lines = [
        f"[scan] verdict={evaluation.get('verdict')} digest={evaluation.get('image_digest')}",
        f"[scan] matches={counts.get('total')} blocking={counts.get('blocking')} "
        f"excepted={counts.get('excepted')} "
        f"unfixed_high_or_critical={counts.get('unfixed_high_or_critical')}",
        f"[scan] grype={TOOL_RELEASES['grype']['version']} "
        f"db_built={database.get('built')} db_age_hours={database.get('age_hours')}",
    ]
    age_check = database.get("age_check", {}) if isinstance(database, Mapping) else {}
    if isinstance(age_check, Mapping) and age_check.get("passed") is not True:
        # Only reachable in policy-only mode -- the release path raises instead. Printed rather
        # than left in the JSON so the reader knows which question this run did not answer.
        lines.append(
            f"[scan] WARNING: the vulnerability database is {database.get('age_hours')} hours "
            f"old, past the {age_check.get('max_hours')}-hour release limit. This result is a "
            "policy check, not release evidence."
        )
    if distro.get("end_of_life"):
        # Printed as well as recorded. Under-reporting is the one failure mode a CVE gate
        # cannot detect on its own, so it does not get to be a quiet field in a JSON file.
        lines.append(f"[scan] WARNING: {distro.get('note')}")
    for warning in expiring_exceptions(evaluation, today or dt.date.today()):
        lines.append(f"[scan] WARNING: {warning}")
    for finding in evaluation.get("blocking_findings", []):
        lines.append(
            f"[scan] blocking {finding.get('id')} {finding.get('severity')} "
            f"{finding.get('package')}@{finding.get('version')} ({finding.get('type')})"
        )
    return "\n".join(lines)


def grype_config(path: Path) -> Path:
    """Write the scanner configuration.

    A config file rather than environment variables for non-secret settings, because a mistyped
    `GRYPE_*` name is a silent no-op -- the failure mode that made an earlier ownership-filter
    experiment in #61 look like evidence when it had never bound. Credentials never appear here
    or in argv; they are passed through the environment and their resolved binding is checked
    separately before the scan.
    """
    max_age_hours = int(MAX_DB_BUILT_AGE.total_seconds() // 3600)
    path.write_text(
        "\n".join(
            (
                "db:",
                "  auto-update: true",
                "  validate-by-hash-on-start: true",
                "  validate-age: true",
                f"  max-allowed-built-age: {max_age_hours}h",
                "  require-update-check: true",
                "check-for-app-update: false",
                "",
            )
        ),
        encoding="utf-8",
    )
    return path


def registry_scan_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Registry credentials for the scanner, through the environment only.

    Grype's generated config descriptions name `SYFT_REGISTRY_AUTH_*`, but Grype 0.116.1 does not
    bind those variables. The resolved `registry.auth` list proves that the `GRYPE_*` prefix is
    the one this binary reads. Credentials are never passed as command-line arguments, which
    appear in job logs.
    """
    scan_env = dict(os.environ)
    scan_env.update(
        {
            "GRYPE_REGISTRY_AUTH_AUTHORITY": env["CI_REGISTRY"],
            "GRYPE_REGISTRY_AUTH_USERNAME": env["CI_REGISTRY_USER"],
            "GRYPE_REGISTRY_AUTH_PASSWORD": env["CI_REGISTRY_PASSWORD"],
        }
    )
    return scan_env


def _validate_resolved_grype_registry_auth(document: Any, authority: str) -> None:
    """Validate the version-specific resolved config shape without exposing its values."""
    if not isinstance(document, Mapping):
        raise ReleaseError("resolved Grype configuration is not a mapping")
    registry = document.get("registry")
    if not isinstance(registry, Mapping):
        raise ReleaseError("resolved Grype configuration has no registry mapping")
    auth = registry.get("auth")
    if not isinstance(auth, list):
        raise ReleaseError("resolved Grype registry.auth is not a list")
    for entry in auth:
        if not isinstance(entry, Mapping):
            raise ReleaseError("resolved Grype registry.auth contains a non-mapping entry")
    matches = [entry for entry in auth if entry.get("authority") == authority]
    if not matches:
        raise ReleaseError(f"resolved Grype registry.auth has no entry for {authority}")
    if not any(
        isinstance(entry.get("username"), str)
        and bool(entry["username"])
        and isinstance(entry.get("password"), str)
        and bool(entry["password"])
        for entry in matches
    ):
        raise ReleaseError(
            f"resolved Grype registry.auth entry for {authority} has empty credentials"
        )


def _validate_registry_authority(repository: str, authority: str) -> None:
    """The image the scan pulls must be under the authority the credentials are scoped to.

    Resolving the auth entry proves Grype bound the credentials; it does not prove they apply to
    the reference `run_grype()` pulls. GitLab keeps `CI_REGISTRY_IMAGE` prefixed by `CI_REGISTRY`
    by construction, so this closes the residual "credentials bound, still 401" shape rather than
    a reachable failure -- but it is the assertion that ties the guard to the pull target.
    """
    if repository != authority and not repository.startswith(f"{authority}/"):
        raise ReleaseError(
            f"the scan target {repository} is not under the registry authority {authority} "
            "the scan credentials are scoped to"
        )


def validate_grype_registry_auth(
    grype: Path,
    *,
    config_path: Path,
    environ: Mapping[str, str],
    authority: str,
) -> None:
    """Fail closed unless Grype resolves the registry credentials the scan will use.

    This deliberately bypasses `run()`: its error path may include captured stdout, while this
    command's stdout is the resolved configuration document and may contain credentials. The
    document shape is coupled to the checksum-pinned Grype version and must be re-verified when
    that pin changes.
    """
    import yaml

    try:
        result = subprocess.run(
            (str(grype), "config", "--load", "--config", str(config_path)),
            capture_output=True,
            text=True,
            env=dict(environ),
        )
    except OSError as exc:
        raise ReleaseError(f"could not resolve Grype registry authentication: {exc}") from exc
    if result.returncode != 0:
        raise ReleaseError(
            "grype config --load failed while resolving registry authentication "
            f"(exit {result.returncode}); resolved configuration was not logged"
        )
    # Raised after the handler returns, never from inside it. A PyYAML parse error quotes the
    # offending source with a context mark, so the resolved document -- credentials included --
    # travels with the exception object. `from None` would clear `__cause__` and leave that copy
    # in `__context__`; leaving the handler first is what makes both clean.
    document: Any = None
    readable = True
    try:
        document = yaml.safe_load(result.stdout)
    except yaml.YAMLError:
        readable = False
    if not readable:
        raise ReleaseError(
            "grype config --load returned unreadable YAML; resolved configuration was not logged"
        )
    _validate_resolved_grype_registry_auth(document, authority)


def run_grype(
    grype: Path,
    reference: str,
    *,
    report_path: Path,
    config_path: Path,
    environ: Mapping[str, str],
) -> dict[str, Any]:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    run(
        (
            str(grype),
            f"registry:{reference}",
            "--config",
            str(config_path),
            "--output",
            "json",
            "--file",
            str(report_path),
        ),
        environ=environ,
    )
    try:
        raw = report_path.read_bytes()
    except OSError as exc:
        raise ReleaseError(f"the scanner wrote no report to {report_path}: {exc}") from exc
    return json_object(raw, f"scan report {report_path}")


def _validate_evaluation_scanner(evaluation: Mapping[str, Any], description: str) -> None:
    scanner = evaluation.get("scanner")
    pinned = TOOL_RELEASES["grype"]
    if not isinstance(scanner, Mapping) or scanner.get("version") != pinned["version"]:
        raise ReleaseError(f"{description} was not produced by the pinned grype {pinned['version']}")
    # The archive checksum too, not only the version string: the version is self-reported by the
    # document, so on its own it says nothing about which binary produced it.
    if scanner.get("archive_sha256") != pinned["sha256"]:
        raise ReleaseError(f"{description} does not record the pinned grype archive checksum")


def _validate_evaluation_policy(
    evaluation: Mapping[str, Any],
    description: str,
    *,
    exceptions_raw: bytes,
) -> None:
    """Bind the verdict to the policy that produced it, recomputed from the tag's own files.

    Checking `policy.version` alone would let a document through that recorded different
    blocking severities, a different database age limit, or a foreign exception file -- all of
    which change what the verdict means. `exceptions_raw` is the checked-out file's **bytes**,
    hashed rather than parsed, which is what keeps this available on a release path that may not
    import a third-party module.
    """
    policy = evaluation.get("policy")
    if not isinstance(policy, Mapping) or policy.get("version") != SCAN_POLICY_VERSION:
        raise ReleaseError(f"{description} was produced under a different policy version")
    expected = policy_fingerprint(exceptions_raw)
    if policy.get("sha256") != expected["sha256"]:
        raise ReleaseError(
            f"{description} was produced under a different policy than this commit carries: "
            f"recorded {policy.get('sha256')!r}, expected {expected['sha256']!r}. The exception "
            "file or the blocking rule changed between the scan and this release."
        )


def _validate_evaluation_exception_expiry(
    evaluation: Mapping[str, Any],
    description: str,
    today: dt.date,
) -> None:
    """Re-check exception expiry at the moment the evidence is consumed.

    `check_exception_hygiene()` enforces expiry when an evaluation is *produced*, which is not
    the same moment. Evidence is keyed by digest and deliberately reusable, so a later release
    of the same digest would otherwise pass on an exception that has since expired -- and the
    expiry warning would helpfully print a negative day count while it did. The policy
    fingerprint already proves the tag carries the same exception file; this proves the dates in
    it have not run out. Stdlib only: the dates are read from the JSON evaluation, not the YAML.
    """
    applied = evaluation.get("exceptions_applied")
    if not isinstance(applied, list):
        raise ReleaseError(f"{description} carries no readable exception list")
    for record in applied:
        if not isinstance(record, Mapping):
            raise ReleaseError(f"{description} carries an unreadable exception record")
        label = record.get("id") or record.get("class") or "<unnamed>"
        try:
            expires = dt.date.fromisoformat(str(record.get("expires", "")))
        except ValueError as exc:
            raise ReleaseError(
                f"{description} carries exception {label} with no readable expiry date"
            ) from exc
        if expires < today:
            raise ReleaseError(
                f"{description} relies on exception {label}, which expired on "
                f"{expires.isoformat()}. Re-scan the digest against an updated exception file "
                "(see docs/RELEASING.md) rather than releasing on a lapsed acceptance."
            )


def validate_scan_evaluation(
    raw: bytes,
    description: str,
    *,
    digest: str,
    repository: str,
    exceptions_raw: bytes,
    report: bytes,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Verify a durable evaluation, and the report it points at, without re-running the scanner.

    This is the whole reason the release job never scans: the report embeds a scan timestamp and
    database build date, so it is not byte-stable, and a retried upload of a re-derived document
    would conflict with the durable-file content check after the aliases had already moved.

    Every consumer of staged evidence goes through here, so the retry path in `publish_scan()`
    gets the same checks as `release_publish` rather than a subset.
    """
    moment = now or dt.datetime.now(dt.timezone.utc)
    evaluation = json_object(raw, description)
    if evaluation.get("schema_version") != SCAN_EVALUATION_SCHEMA:
        raise ReleaseError(f"{description} has an unsupported schema version")
    if evaluation.get("policy_only") is True:
        raise ReleaseError(
            f"{description} was produced by an offline policy check, which does not verify the "
            "digest binding or the database age. Only `publish-scan` produces release evidence."
        )
    _validate_evaluation_policy(evaluation, description, exceptions_raw=exceptions_raw)
    if evaluation.get("image_digest") != digest:
        raise ReleaseError(
            f"{description} evaluates {evaluation.get('image_digest')!r}, not {digest}"
        )
    _validate_evaluation_scanner(evaluation, description)
    recorded = evaluation.get("report_sha256")
    actual = hashlib.sha256(report).hexdigest()
    if recorded != actual:
        raise ReleaseError(
            f"{description} was computed from a different report: recorded {recorded!r}, "
            f"staged document hashes to {actual}"
        )
    # The hash binds the evaluation to *a* report; this binds that report to the image being
    # promoted. Without it a hash-matched pair describing another digest is accepted -- which
    # `evaluate --report <wrong file> --digest <right digest>` will produce from one mistyped
    # path while following the recovery runbook.
    validate_scan_target(
        json_object(report, f"staged report for {digest}"),
        digest=digest,
        repository=repository,
    )
    database = evaluation.get("database")
    age_check = database.get("age_check") if isinstance(database, Mapping) else None
    if not isinstance(age_check, Mapping) or age_check.get("passed") is not True:
        raise ReleaseError(f"{description} does not record a passing database freshness check")
    _validate_evaluation_exception_expiry(evaluation, description, moment.date())
    if evaluation.get("verdict") != "pass":
        blocking = evaluation.get("counts", {})
        count = blocking.get("blocking") if isinstance(blocking, Mapping) else "?"
        raise ReleaseError(
            f"{description} did not pass the vulnerability policy ({count} blocking findings)"
        )
    return evaluation


def exceptions_document_bytes(path: Path) -> bytes:
    """The exception file's exact bytes, or empty when absent.

    Deliberately does not parse: `release_publish` needs the policy hash and must not import a
    YAML library.
    """
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise ReleaseError(f"could not read {path}: {exc}") from exc


def release_scan_for_digest(
    api: GitLabApi,
    digest: str,
    *,
    repository: str,
    exceptions_path: Path = EXCEPTIONS_PATH,
) -> tuple[bytes, bytes]:
    version = digest_package_version(digest)
    report = api.package_file(version, SCAN_REPORT_FILENAME, package_name=SCAN_PACKAGE_NAME)
    evaluation = api.package_file(
        version, SCAN_EVALUATION_FILENAME, package_name=SCAN_PACKAGE_NAME
    )
    if report is None or evaluation is None:
        raise ReleaseError(
            f"no durable vulnerability evidence is published for {digest}: expected "
            f"{SCAN_PACKAGE_NAME}/{version}/ to carry both {SCAN_REPORT_FILENAME} and "
            f"{SCAN_EVALUATION_FILENAME} (see docs/RELEASING.md)"
        )
    validate_scan_evaluation(
        evaluation,
        f"durable evaluation for {digest}",
        digest=digest,
        repository=repository,
        exceptions_raw=exceptions_document_bytes(exceptions_path),
        report=report,
    )
    return report, evaluation


def scan_digest(
    *,
    grype: Path,
    digest: str,
    artifacts_dir: Path,
    exceptions_path: Path,
    environ: Mapping[str, str],
    existing_report: bytes | None = None,
) -> tuple[dict[str, Any], bytes, bytes]:
    """Scan one immutable digest and evaluate it, or re-evaluate a report already staged.

    `existing_report` exists for the recovery case where a previous run staged the report but
    not the evaluation: re-scanning would produce different bytes and conflict with the durable
    content check, so the stored report is re-used verbatim.
    """
    repository = environ["CI_REGISTRY_IMAGE"]
    reference = f"{repository}@{digest}"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    report_path = artifacts_dir / SCAN_REPORT_FILENAME
    if existing_report is None:
        config_path = grype_config(artifacts_dir / "grype.yaml")
        scan_env = registry_scan_environment(environ)
        _validate_registry_authority(repository, environ["CI_REGISTRY"])
        validate_grype_registry_auth(
            grype,
            config_path=config_path,
            environ=scan_env,
            authority=environ["CI_REGISTRY"],
        )
        report = run_grype(
            grype,
            reference,
            report_path=report_path,
            config_path=config_path,
            environ=scan_env,
        )
        report_bytes = report_path.read_bytes()
    else:
        report_bytes = existing_report
        report = json_object(report_bytes, f"staged scan report for {digest}")
        report_path.write_bytes(report_bytes)
    exceptions, exceptions_raw = load_exceptions(exceptions_path)
    evaluation = evaluate_scan(
        report,
        digest=digest,
        repository=repository,
        commit=environ.get("CI_COMMIT_SHA", ""),
        exceptions=exceptions,
        exceptions_raw=exceptions_raw,
        now=dt.datetime.now(dt.timezone.utc),
        report_sha256=hashlib.sha256(report_bytes).hexdigest(),
    )
    evaluation_bytes = (json.dumps(evaluation, indent=2, sort_keys=True) + "\n").encode()
    (artifacts_dir / SCAN_EVALUATION_FILENAME).write_bytes(evaluation_bytes)
    return evaluation, report_bytes, evaluation_bytes


def evaluate_saved_report(
    *,
    report_path: Path,
    exceptions_path: Path,
    digest: str = "",
    repository: str = "",
) -> dict[str, Any]:
    """Re-run the policy against a report you already have, with no scanner and no network.

    This exists because the gate cannot run on a merge request -- no image is built, so there is
    no digest to scan -- while the exception file's hygiene rules are hard failures. Without an
    offline path, an MR that bumps `PYTHON_IMAGE` would pass every check, merge, and only then
    break `main` with ten "names a version that is no longer installed" errors. Point this at the
    report from the last `security_scan` artifact and it answers the question before merge.

    Neither the digest binding nor the database age is enforced here, so a report produced
    locally from a `docker-archive` works, and so does a default-branch artifact older than the
    48-hour release limit -- which most of them are, since they are kept for 30 days. Both are
    properties of the evidence, not of the policy. The document records `policy_only: true` and
    an unpassed age check, and `validate_scan_evaluation()` rejects both, so nothing produced
    here can be staged and read as release evidence.
    """
    raw = report_path.read_bytes()
    report = json_object(raw, f"scan report {report_path}")
    target = scan_report_target(report)
    resolved_digest = digest or str(target.get("manifestDigest", ""))
    user_input = str(target.get("userInput", ""))
    resolved_repository = repository or user_input.split("@")[0]
    exceptions, exceptions_raw = load_exceptions(exceptions_path)
    return evaluate_scan(
        report,
        digest=resolved_digest,
        repository=resolved_repository,
        commit="",
        exceptions=exceptions,
        exceptions_raw=exceptions_raw,
        now=dt.datetime.now(dt.timezone.utc),
        report_sha256=hashlib.sha256(raw).hexdigest(),
        enforce_evidence=False,
    )


def scan_published_image(
    *,
    grype: Path,
    artifacts_dir: Path,
    exceptions_path: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Fast-fail scan of the digest the default-branch pipeline just published."""
    source_env = os.environ if environ is None else environ
    env = require_env(
        (
            "IMAGE_PUBLISHED",
            "IMAGE_DIGEST",
            "CI_REGISTRY",
            "CI_REGISTRY_IMAGE",
            "CI_REGISTRY_USER",
            "CI_REGISTRY_PASSWORD",
        ),
        source_env,
    )
    if env["IMAGE_PUBLISHED"] != "true":
        raise ReleaseError("refusing to scan an image that was not published")
    evaluation, _, _ = scan_digest(
        grype=grype,
        digest=env["IMAGE_DIGEST"],
        artifacts_dir=artifacts_dir,
        exceptions_path=exceptions_path,
        environ=dict(source_env),
    )
    return evaluation


def publish_scan(
    *,
    grype: Path,
    crane: Path,
    artifacts_dir: Path,
    exceptions_path: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Re-scan the released digest with a current database and stage the evidence durably.

    Runs in its own job that `release_publish` needs, so an upload failure happens before any
    alias has moved. Re-scanning rather than re-asserting the default-branch result is
    deliberate: vulnerability knowledge is time-dependent, and a result that passed when the
    image was built is not a claim about the day it is released.
    """
    source_env = os.environ if environ is None else environ
    env = require_env(
        (
            "CI_COMMIT_TAG",
            "CI_COMMIT_SHA",
            "CI_COMMIT_REF_PROTECTED",
            "CI_REGISTRY",
            "CI_REGISTRY_IMAGE",
            "CI_REGISTRY_USER",
            "CI_REGISTRY_PASSWORD",
        ),
        source_env,
    )
    if env["CI_COMMIT_REF_PROTECTED"] != "true":
        raise ReleaseError("refusing to stage release scan evidence from an unprotected tag")
    Version.from_tag(env["CI_COMMIT_TAG"])
    api, _ = gitlab_api_from_env(source_env)
    crane_login(crane, env["CI_REGISTRY"], env["CI_REGISTRY_USER"], env["CI_REGISTRY_PASSWORD"])
    digest = crane_digest(crane, f"{env['CI_REGISTRY_IMAGE']}:{env['CI_COMMIT_SHA']}")
    package_version = digest_package_version(digest)

    # Read before scanning. A retry of a job that already staged its evidence must be a no-op:
    # the evaluation embeds a timestamp, so re-deriving it would conflict with the durable
    # content check rather than succeed idempotently.
    staged_report = api.package_file(
        package_version, SCAN_REPORT_FILENAME, package_name=SCAN_PACKAGE_NAME
    )
    staged_evaluation = api.package_file(
        package_version, SCAN_EVALUATION_FILENAME, package_name=SCAN_PACKAGE_NAME
    )
    if staged_report is not None and staged_evaluation is not None:
        evaluation = validate_scan_evaluation(
            staged_evaluation,
            f"staged evaluation for {digest}",
            digest=digest,
            repository=env["CI_REGISTRY_IMAGE"],
            exceptions_raw=exceptions_document_bytes(exceptions_path),
            report=staged_report,
        )
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / SCAN_REPORT_FILENAME).write_bytes(staged_report)
        (artifacts_dir / SCAN_EVALUATION_FILENAME).write_bytes(staged_evaluation)
        print(f"[scan] reusing the evidence already staged for {digest}")
        return evaluation
    if staged_evaluation is not None:
        raise ReleaseError(
            f"{SCAN_PACKAGE_NAME}/{package_version} carries an evaluation with no report; "
            "recover the pair by hand rather than re-scanning (see docs/RELEASING.md)"
        )

    evaluation, report_bytes, evaluation_bytes = scan_digest(
        grype=grype,
        digest=digest,
        artifacts_dir=artifacts_dir,
        exceptions_path=exceptions_path,
        environ=dict(env),
        existing_report=staged_report,
    )
    if evaluation["verdict"] != "pass":
        raise ReleaseError(
            f"release scan of {digest} did not pass: "
            f"{evaluation['counts']['blocking']} blocking findings"
        )
    api.upload_package_file(
        package_version, SCAN_REPORT_FILENAME, report_bytes, package_name=SCAN_PACKAGE_NAME
    )
    api.upload_package_file(
        package_version,
        SCAN_EVALUATION_FILENAME,
        evaluation_bytes,
        package_name=SCAN_PACKAGE_NAME,
    )
    return evaluation


def desired_release_links(
    api: GitLabApi,
    version: str,
    *,
    include_yank: bool = False,
    include_sbom: bool = False,
    include_scan: bool = False,
) -> list[dict[str, str]]:
    filenames: tuple[str, ...] = ("release-manifest.json", "changelog.md")
    if include_sbom:
        filenames += (SBOM_FILENAME,)
    # Gated for the same reason as the SBOM: this function is shared with the yank path, and
    # GitLab never checks that a link URL resolves. Added unconditionally, every release
    # published before this landed would gain a permanent 404 the next time its links were
    # reconciled.
    if include_scan:
        filenames += (SCAN_REPORT_FILENAME, SCAN_EVALUATION_FILENAME)
    if include_yank:
        filenames += ("yank-record.json",)
    return [
        {
            "name": filename,
            "url": api.asset_url(version, filename),
            "link_type": "other",
        }
        for filename in filenames
    ]


def existing_release_links(release: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Read the asset links of an existing release, strictly.

    Only an absent release is "no links". A release whose asset payload cannot be read as
    GitLab documents it is unexplained state: treating it as empty would mark links that may
    already exist as pending, and the duplicate would only be rejected after the aliases and
    package files were written. Anything unreadable raises instead.
    """
    if release is None:
        return []
    assets = release.get("assets")
    if not isinstance(assets, dict):
        raise ReleaseError("GitLab release record carries no readable assets object")
    links = assets.get("links")
    if not isinstance(links, list):
        raise ReleaseError("GitLab release assets carry no readable link list")
    recorded: list[dict[str, str]] = []
    for link in links:
        if (
            not isinstance(link, dict)
            or not isinstance(link.get("name"), str)
            or not isinstance(link.get("url"), str)
        ):
            raise ReleaseError(f"GitLab release carries an unreadable asset link: {link!r}")
        recorded.append({"name": link["name"], "url": link["url"]})
    return recorded


def pending_release_links(
    desired: Iterable[Mapping[str, str]],
    existing: Iterable[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Select the asset links still missing from a release.

    GitLab requires release link names and URLs to be unique, so re-sending a link that
    already exists fails the whole call. An exact match is accepted as already done; any
    other collision is unexplained state and fails closed.
    """
    by_name = {link["name"]: link["url"] for link in existing}
    by_url = {link["url"]: link["name"] for link in existing}
    pending: list[dict[str, str]] = []
    for link in desired:
        name, url = link["name"], link["url"]
        recorded = by_name.get(name)
        if recorded == url:
            continue
        if recorded is not None:
            raise ReleaseError(
                f"release link {name} already points at a different URL: {recorded}"
            )
        colliding = by_url.get(url)
        if colliding is not None:
            raise ReleaseError(f"release link URL for {name} is already used by {colliding}")
        pending.append(dict(link))
    return pending


@dataclass(frozen=True)
class ReleasePlan:
    """The GitLab Release work a publication or yank still has to perform."""

    tag: str
    exists: bool
    pending_links: tuple[dict[str, str], ...]


def plan_release(
    api: GitLabApi,
    version: str,
    *,
    tag: str,
    include_yank: bool = False,
    include_sbom: bool = False,
    include_scan: bool = False,
) -> ReleasePlan:
    release = api.release(tag)
    pending = pending_release_links(
        desired_release_links(
            api,
            version,
            include_yank=include_yank,
            include_sbom=include_sbom,
            include_scan=include_scan,
        ),
        existing_release_links(release),
    )
    return ReleasePlan(tag=tag, exists=release is not None, pending_links=tuple(pending))


def apply_release_plan(
    api: GitLabApi,
    plan: ReleasePlan,
    *,
    version: str,
    notes: str,
) -> None:
    """Create or update the GitLab Release through the API.

    The release record is written with the same job-token API client as the durable package
    files, so publication depends on no external CLI and, in particular, on no `git`
    executable for project discovery -- the release image ships neither.
    """
    name = f"Robot Dev Team v{version}"
    if not plan.exists:
        api.create_release(
            tag=plan.tag,
            name=name,
            description=notes,
            links=plan.pending_links,
        )
        return
    api.update_release(tag=plan.tag, name=name, description=notes)
    for link in plan.pending_links:
        api.add_release_link(tag=plan.tag, link=link)


def publish_release(
    *,
    context_path: Path,
    artifacts_dir: Path,
    crane: Path,
    exceptions_path: Path = EXCEPTIONS_PATH,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    source_env = os.environ if environ is None else environ
    env = require_env(
        (
            "CI_COMMIT_TAG",
            "CI_COMMIT_SHA",
            "CI_COMMIT_REF_PROTECTED",
            "CI_REGISTRY",
            "CI_REGISTRY_IMAGE",
            "CI_REGISTRY_USER",
            "CI_REGISTRY_PASSWORD",
            "CI_PIPELINE_URL",
            "CI_JOB_URL",
        ),
        source_env,
    )
    if env["CI_COMMIT_REF_PROTECTED"] != "true":
        raise ReleaseError("refusing to publish from an unprotected release tag")
    context = load_context(context_path)
    if context.get("operation") != "publish":
        raise ReleaseError("validated context does not authorize release publication")
    version = Version.from_tag(env["CI_COMMIT_TAG"])
    if context.get("release_version") != str(version):
        raise ReleaseError("validated context version does not match the release tag")
    if context.get("source_commit") != env["CI_COMMIT_SHA"]:
        raise ReleaseError("validated context commit does not match CI_COMMIT_SHA")

    api, _ = gitlab_api_from_env(source_env)
    if api.package_file(str(version), "yank-record.json") is not None:
        raise ReleaseError(f"release {version} is already yanked")
    # Resolved before any mutation so an unreadable or conflicting release record cannot
    # leave the aliases and package files published with no GitLab Release. The SBOM link is
    # planned here, ahead of the digest, because it points at the version-scoped copy this job
    # is about to write -- a digest-scoped link URL would make the plan digest-dependent and
    # force it after the registry login.
    plan = plan_release(
        api,
        str(version),
        tag=env["CI_COMMIT_TAG"],
        include_sbom=True,
        include_scan=True,
    )

    crane_login(
        crane,
        env["CI_REGISTRY"],
        env["CI_REGISTRY_USER"],
        env["CI_REGISTRY_PASSWORD"],
    )
    source_image = f"{env['CI_REGISTRY_IMAGE']}:{env['CI_COMMIT_SHA']}"
    source_digest = crane_digest(crane, source_image)
    # Fetched in the window between resolving the digest and the first alias write: nothing has
    # been mutated yet, so a missing or malformed SBOM aborts the release with the aliases
    # unmoved and no package file written. `source_image` is the same string the build side
    # scanned under, so the document must name the image this release is promoting.
    sbom_bytes = release_sbom_for_digest(api, source_digest, expected_name=source_image)
    # The gate sits in the same window and for the same reason: the evidence is only read and
    # verified here, never produced, so a missing, unbound, or failing evaluation aborts the
    # release with nothing mutated. It must never move into the shared alias code -- withdrawal
    # has to stay available during exactly the outage that would fail a scan.
    scan_report_bytes, scan_evaluation_bytes = release_scan_for_digest(
        api,
        source_digest,
        repository=env["CI_REGISTRY_IMAGE"],
        exceptions_path=exceptions_path,
    )
    moving_aliases = desired_moving_aliases(version, non_yanked_release_versions(api))

    aliases = [
        apply_alias(
            crane,
            env["CI_REGISTRY_IMAGE"],
            source_digest,
            str(version),
            immutable=True,
        )
    ]
    aliases.extend(
        apply_alias(
            crane,
            env["CI_REGISTRY_IMAGE"],
            source_digest,
            alias,
            immutable=False,
        )
        for alias in moving_aliases
    )

    existing_manifest = api.package_file(str(version), "release-manifest.json")
    released_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    if existing_manifest is not None:
        existing = json_object(existing_manifest, f"release manifest {version}")
        expected = {
            "release_version": str(version),
            "git_tag": env["CI_COMMIT_TAG"],
            "source_commit": env["CI_COMMIT_SHA"],
            "image_digest": source_digest,
        }
        for key, value in expected.items():
            if existing.get(key) != value:
                raise ReleaseError(
                    f"existing release manifest has conflicting {key}: {existing.get(key)!r}"
                )
        manifest = existing
        manifest_bytes = existing_manifest
    else:
        manifest = {
            "schema_version": 1,
            "release_version": str(version),
            "git_tag": env["CI_COMMIT_TAG"],
            "source_commit": env["CI_COMMIT_SHA"],
            "source_image": source_image,
            "image_digest": source_digest,
            "image_reference": f"{env['CI_REGISTRY_IMAGE']}@{source_digest}",
            "aliases": aliases,
            "pipeline_url": env["CI_PIPELINE_URL"],
            "job_url": env["CI_JOB_URL"],
            "released_at": released_at,
        }
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    notes = str(context["changelog"])
    changelog_bytes = notes.encode()
    api.upload_package_file(str(version), "release-manifest.json", manifest_bytes)
    api.upload_package_file(str(version), "changelog.md", changelog_bytes)
    # The exact bytes staged under the digest, so the version-scoped copy the release links is
    # the SBOM of the smoke-tested image and not a re-derived document.
    api.upload_package_file(str(version), SBOM_FILENAME, sbom_bytes)
    # Copied, not re-derived, for the same reason as the SBOM: a Grype report embeds a scan
    # timestamp and database build date, so regenerating it would break the durable-file content
    # check on an ordinary job retry -- with the aliases already moved.
    api.upload_package_file(str(version), SCAN_REPORT_FILENAME, scan_report_bytes)
    api.upload_package_file(str(version), SCAN_EVALUATION_FILENAME, scan_evaluation_bytes)

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = artifacts_dir / "release-manifest.json"
    notes_path = artifacts_dir / "release-changelog.md"
    manifest_path.write_bytes(manifest_bytes)
    notes_path.write_bytes(changelog_bytes)
    (artifacts_dir / SBOM_FILENAME).write_bytes(sbom_bytes)
    (artifacts_dir / SCAN_REPORT_FILENAME).write_bytes(scan_report_bytes)
    (artifacts_dir / SCAN_EVALUATION_FILENAME).write_bytes(scan_evaluation_bytes)
    apply_release_plan(api, plan, version=str(version), notes=notes)
    return manifest


def manifest_for_version(api: GitLabApi, version: Version) -> dict[str, Any]:
    raw = api.package_file(str(version), "release-manifest.json")
    if raw is None:
        raise ReleaseError(f"release {version} has no durable release manifest")
    manifest = json_object(raw, f"release manifest {version}")
    validate_release_manifest(manifest, version)
    return manifest


def validate_yank_context(
    context: Mapping[str, Any],
    env: Mapping[str, str],
) -> tuple[Version, str]:
    if env["CI_COMMIT_REF_PROTECTED"] != "true":
        raise ReleaseError("release reconciliation requires a protected tag")
    if context.get("operation") != "yank":
        raise ReleaseError("validated context does not authorize release reconciliation")
    if context.get("git_tag") != env["CI_COMMIT_TAG"]:
        raise ReleaseError("validated context tag does not match CI_COMMIT_TAG")
    if context.get("source_commit") != env["CI_COMMIT_SHA"]:
        raise ReleaseError("validated context commit does not match CI_COMMIT_SHA")
    yanked = Version.from_yank_tag(env["CI_COMMIT_TAG"])
    if context.get("yanked_version") != str(yanked):
        raise ReleaseError("validated context version does not match the yank tag")
    reason = context.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ReleaseError("validated yank reason must not be blank")
    return yanked, reason.strip()


def validated_manifest_digest(manifest: Mapping[str, Any], version: Version) -> str:
    digest = str(manifest.get("image_digest", ""))
    if IMAGE_DIGEST.fullmatch(digest) is None:
        raise ReleaseError(f"release {version} has an invalid image digest")
    return digest


def target_version_for_alias(
    alias: str,
    released: Iterable[Version],
) -> Version | None:
    versions = tuple(released)
    if alias == "latest":
        eligible = versions
    elif re.fullmatch(r"(0|[1-9][0-9]*)", alias):
        major = int(alias)
        eligible = tuple(version for version in versions if version.major == major)
    elif re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", alias):
        major, minor = (int(part) for part in alias.split("."))
        eligible = tuple(
            version
            for version in versions
            if (version.major, version.minor) == (major, minor)
        )
    else:
        raise ReleaseError(f"release manifest contains invalid moving alias: {alias}")
    return max(eligible, default=None)


def moving_alias_names(manifest: Mapping[str, Any], version: Version) -> tuple[str, ...]:
    alias_records = manifest.get("aliases", [])
    if not isinstance(alias_records, list):
        raise ReleaseError("release manifest aliases must be an array")
    names: list[str] = []
    for alias_record in alias_records:
        if not isinstance(alias_record, dict) or alias_record.get("kind") != "moving":
            continue
        alias = alias_record.get("name")
        if not isinstance(alias, str) or alias not in version.aliases:
            raise ReleaseError(f"release manifest contains invalid moving alias: {alias!r}")
        if alias not in names:
            names.append(alias)
    return tuple(names)


def reconcile_moving_aliases(
    *,
    crane: Path,
    repository: str,
    aliases: Iterable[str],
    bad_digest: str,
    targets: Mapping[str, tuple[Version, str]],
) -> tuple[dict[str, str], list[str]]:
    observed: dict[str, str] = {}
    for alias in aliases:
        observed[alias] = crane_digest(crane, f"{repository}:{alias}")

    unsafe = sorted(
        alias for alias, current in observed.items() if current == bad_digest and alias not in targets
    )
    if unsafe:
        joined = ", ".join(unsafe)
        raise ReleaseError(
            f"cannot yank while aliases lack a compatible non-yanked release: {joined}"
        )

    repointed: dict[str, str] = {}
    skipped: list[str] = []
    for alias, current in observed.items():
        reference = f"{repository}:{alias}"
        target = targets.get(alias)
        if target is None:
            skipped.append(alias)
            continue
        target_version, target_digest = target
        if current == bad_digest:
            run((str(crane), "tag", f"{repository}@{target_digest}", alias))
            if crane_digest(crane, reference) != target_digest:
                raise ReleaseError(f"rollback verification failed for {reference}")
            repointed[alias] = str(target_version)
        elif current == target_digest:
            repointed[alias] = str(target_version)
        else:
            skipped.append(alias)
    return repointed, skipped


def existing_yank_record(
    api: GitLabApi,
    *,
    yanked: Version,
    reason: str,
) -> dict[str, Any] | None:
    raw = api.package_file(str(yanked), "yank-record.json")
    if raw is None:
        return None
    record = json_object(raw, f"yank record {yanked}")
    if record.get("yanked_version") != str(yanked) or record.get("reason") != reason:
        raise ReleaseError(f"release {yanked} already has a different yank record")
    return record


def yank_release(
    *,
    context_path: Path,
    artifacts_dir: Path,
    crane: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    source_env = os.environ if environ is None else environ
    env = require_env(
        (
            "CI_COMMIT_TAG",
            "CI_COMMIT_SHA",
            "CI_COMMIT_REF_PROTECTED",
            "CI_REGISTRY",
            "CI_REGISTRY_IMAGE",
            "CI_REGISTRY_USER",
            "CI_REGISTRY_PASSWORD",
            "CI_PIPELINE_URL",
            "CI_JOB_URL",
        ),
        source_env,
    )
    context = load_context(context_path)
    yanked, reason = validate_yank_context(context, env)

    api, _ = gitlab_api_from_env(source_env)
    bad_manifest = manifest_for_version(api, yanked)
    record = existing_yank_record(api, yanked=yanked, reason=reason)
    bad_digest = validated_manifest_digest(bad_manifest, yanked)
    released = successful_releases(api)
    released.pop(yanked, None)
    aliases = moving_alias_names(bad_manifest, yanked)
    targets: dict[str, tuple[Version, str]] = {}
    for alias in aliases:
        target_version = target_version_for_alias(alias, released)
        if target_version is None:
            continue
        target_manifest = released[target_version]
        targets[alias] = (
            target_version,
            validated_manifest_digest(target_manifest, target_version),
        )
    operator = api.current_job_user()
    # Resolved before the aliases move so a conflicting release record fails the yank
    # while it is still reversible. The SBOM link is gated on the file actually existing:
    # GitLab does not check that a link URL resolves, so desiring it unconditionally would
    # give a release published before the SBOM work a permanent link returning 404.
    has_sbom = api.package_file(str(yanked), SBOM_FILENAME) is not None
    # Same gating for the scan evidence, and note what this path deliberately does not do: it
    # reads whether the files exist, never whether they pass. Withdrawal must work when the
    # scanner cannot run at all -- including when the CVE motivating the yank is the one that
    # would fail the scan.
    has_scan = api.package_file(str(yanked), SCAN_EVALUATION_FILENAME) is not None
    plan = plan_release(
        api,
        str(yanked),
        tag=f"v{yanked}",
        include_yank=True,
        include_sbom=has_sbom,
        include_scan=has_scan,
    )

    crane_login(
        crane,
        env["CI_REGISTRY"],
        env["CI_REGISTRY_USER"],
        env["CI_REGISTRY_PASSWORD"],
    )
    repointed, skipped = reconcile_moving_aliases(
        crane=crane,
        repository=env["CI_REGISTRY_IMAGE"],
        aliases=aliases,
        bad_digest=bad_digest,
        targets=targets,
    )

    if record is None:
        record = {
            "schema_version": 1,
            "yanked_version": str(yanked),
            "reason": reason,
            "operator": {
                "id": operator["id"],
                "username": operator["username"],
            },
            "yanked_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "alias_targets": dict(sorted(repointed.items())),
            "aliases_skipped": sorted(skipped),
            "pipeline_url": env["CI_PIPELINE_URL"],
            "job_url": env["CI_JOB_URL"],
        }
    record_bytes = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    api.upload_package_file(str(yanked), "yank-record.json", record_bytes)

    changelog = api.package_file(str(yanked), "changelog.md")
    if changelog is None:
        raise ReleaseError(f"release {yanked} has no durable changelog")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    record_path = artifacts_dir / "yank-record.json"
    notes_path = artifacts_dir / "yanked-release-notes.md"
    record_path.write_bytes(record_bytes)
    target_lines = "\n".join(
        f"- `{alias}` now tracks v{target_version}"
        for alias, target_version in sorted(repointed.items())
    )
    if not target_lines:
        target_lines = "- No moving alias still referenced this release."
    notes = (
        "> **YANKED:** This release was withdrawn.\n\n"
        f"Reason: {reason}\n\n"
        f"{target_lines}\n\n"
    ) + changelog.decode("utf-8", errors="replace")
    notes_path.write_text(notes, encoding="utf-8")
    apply_release_plan(api, plan, version=str(yanked), notes=notes)
    return record


def download_tool(name: str, destination_dir: Path) -> Path:
    metadata = TOOL_RELEASES[name]
    request = urllib.request.Request(
        str(metadata["url"]),
        headers={"User-Agent": "robot-dev-team-release-ci"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            archive = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise ReleaseError(f"could not download {name} {metadata['version']}: {exc}") from exc
    digest = hashlib.sha256(archive).hexdigest()
    if digest != metadata["sha256"]:
        raise ReleaseError(f"{name} {metadata['version']} checksum mismatch: {digest}")

    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            members = [
                member
                for member in bundle.getmembers()
                if member.isfile() and Path(member.name).name == name
            ]
            if len(members) != 1:
                raise ReleaseError(f"{name} archive did not contain exactly one {name} binary")
            stream = bundle.extractfile(members[0])
            if stream is None:
                raise ReleaseError(f"could not read {name} from its release archive")
            binary = stream.read()
    except tarfile.TarError as exc:
        raise ReleaseError(f"{name} release archive is invalid: {exc}") from exc

    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / name
    destination.write_bytes(binary)
    destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return destination


def install_tools(directory: Path, names: Sequence[str] | None = None) -> None:
    selected = tuple(TOOL_RELEASES) if names is None else tuple(names)
    for name in selected:
        if name not in TOOL_RELEASES:
            raise ReleaseError(f"no pinned release is configured for {name}")
    for name in selected:
        destination = download_tool(name, directory)
        print(f"[release] installed {name} {TOOL_RELEASES[name]['version']} at {destination}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a protected release tag")
    validate.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    validate.add_argument("--changelog", type=Path, default=Path("docs/CHANGELOG.md"))
    validate.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/release-context.json"),
    )

    install = subparsers.add_parser("install-tools", help="install pinned release CLIs")
    install.add_argument("--directory", type=Path, default=Path(".release-bin"))
    install.add_argument(
        "--tools",
        default="",
        help=(
            "comma-separated subset of the pinned CLIs to install; the yank path passes "
            f"'{','.join(YANK_TOOLS)}' so withdrawal never depends on the scanner"
        ),
    )

    scan = subparsers.add_parser("scan", help="scan the published image digest and evaluate it")
    scan.add_argument("--grype", type=Path, default=Path(".release-bin/grype"))
    scan.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    scan.add_argument("--exceptions", type=Path, default=EXCEPTIONS_PATH)

    offline = subparsers.add_parser(
        "evaluate",
        help="re-run the policy offline against a report you already have",
    )
    offline.add_argument("--report", type=Path, required=True)
    offline.add_argument("--exceptions", type=Path, default=EXCEPTIONS_PATH)
    offline.add_argument("--digest", default="")
    offline.add_argument("--repository", default="")

    publish_scan_parser = subparsers.add_parser(
        "publish-scan",
        help="re-scan the released digest and stage the durable evidence",
    )
    publish_scan_parser.add_argument("--grype", type=Path, default=Path(".release-bin/grype"))
    publish_scan_parser.add_argument("--crane", type=Path, default=Path(".release-bin/crane"))
    publish_scan_parser.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    publish_scan_parser.add_argument("--exceptions", type=Path, default=EXCEPTIONS_PATH)

    sbom = subparsers.add_parser(
        "publish-sbom",
        help="stage the built SBOM under its published image digest",
    )
    sbom.add_argument("--sbom", type=Path, default=Path("artifacts/sbom.spdx.json"))

    probe = subparsers.add_parser(
        "probe-dockerhub",
        help="copy a protected main image to a disposable Docker Hub tag and verify it",
    )
    probe.add_argument("--crane", type=Path, default=Path(".release-bin/crane"))
    probe.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))

    publish = subparsers.add_parser("publish", help="publish a validated stable release")
    publish.add_argument(
        "--context",
        type=Path,
        default=Path("artifacts/release-context.json"),
    )
    publish.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    publish.add_argument("--crane", type=Path, default=Path(".release-bin/crane"))

    yank = subparsers.add_parser("yank", help="yank a release and reconcile moving aliases")
    yank.add_argument(
        "--context",
        type=Path,
        default=Path("artifacts/release-context.json"),
    )
    yank.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    yank.add_argument("--crane", type=Path, default=Path(".release-bin/crane"))
    return parser


def run_scan_command(args: argparse.Namespace) -> None:
    """Dispatch the three scan entry points, which differ only in where the report comes from."""
    if args.command == "scan":
        evaluation = scan_published_image(
            grype=args.grype,
            artifacts_dir=args.artifacts_dir,
            exceptions_path=args.exceptions,
        )
    elif args.command == "evaluate":
        evaluation = evaluate_saved_report(
            report_path=args.report,
            exceptions_path=args.exceptions,
            digest=args.digest,
            repository=args.repository,
        )
    else:
        evaluation = publish_scan(
            grype=args.grype,
            crane=args.crane,
            artifacts_dir=args.artifacts_dir,
            exceptions_path=args.exceptions,
        )
    if args.command == "evaluate":
        print("[scan] offline policy check: the digest binding is not verified in this mode")
    print(summarize_evaluation(evaluation))
    # Flushed before raising: the error goes to stderr, and an unflushed stdout would put the
    # summary explaining the failure *after* it in a piped job log.
    sys.stdout.flush()
    # `publish-scan` has already raised on a failing verdict, before staging anything durable.
    if evaluation["verdict"] != "pass":
        raise ReleaseError(
            f"vulnerability policy failed: {evaluation['counts']['blocking']} blocking findings"
        )


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "validate":
            validate_release(
                pyproject_path=args.pyproject,
                changelog_path=args.changelog,
                output_path=args.output,
            )
            print(f"[release] validated {os.environ.get('CI_COMMIT_TAG', '')}")
        elif args.command == "install-tools":
            selected = [name for name in args.tools.split(",") if name] or None
            install_tools(args.directory, selected)
        elif args.command in ("scan", "evaluate", "publish-scan"):
            run_scan_command(args)
        elif args.command == "publish-sbom":
            staged = publish_sbom(sbom_path=args.sbom)
            print(f"[release] staged SBOM at {SBOM_PACKAGE_NAME}/{staged}/{SBOM_FILENAME}")
        elif args.command == "probe-dockerhub":
            evidence = probe_dockerhub_credentials(
                crane=args.crane,
                artifacts_dir=args.artifacts_dir,
            )
            print(
                "[release] verified Docker Hub credential probe at "
                f"{evidence['destination_reference']} ({evidence['destination_digest']})"
            )
        elif args.command == "publish":
            manifest = publish_release(
                context_path=args.context,
                artifacts_dir=args.artifacts_dir,
                crane=args.crane,
            )
            print(
                f"[release] published v{manifest['release_version']} at {manifest['image_digest']}"
            )
        elif args.command == "yank":
            record = yank_release(
                context_path=args.context,
                artifacts_dir=args.artifacts_dir,
                crane=args.crane,
            )
            print(f"[release] yanked v{record['yanked_version']}")
        else:  # pragma: no cover - argparse enforces the command
            raise ReleaseError(f"unknown command: {args.command}")
    except ReleaseError as exc:
        print(f"[release] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
