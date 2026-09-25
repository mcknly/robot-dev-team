"""Robot Dev Team Project
File: tests/test_ci_scripts.py
Description: Regression tests for CI image build and publication scripts.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_INSPECT_PUSHED_DIGEST = REPO_ROOT / "scripts" / "ci-inspect-pushed-digest.sh"
CI_BUILD_IMAGE = REPO_ROOT / "scripts" / "ci-build-image.sh"
PROTECTED_DEFAULT_BRANCH_RULE = (
    '$CI_PIPELINE_SOURCE == "push" && $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH '
    '&& $CI_COMMIT_REF_PROTECTED == "true"'
)
DOCKERHUB_ENVIRONMENT = "dockerhub-publication"
GITHUB_ENVIRONMENT = "github-publication"
# Every environment any job declares, and the only jobs allowed to declare it -- and therefore to
# receive its scoped credentials. The #8 publish and #53 yank jobs must be added deliberately.
SCOPED_ENVIRONMENT_JOBS = {
    DOCKERHUB_ENVIRONMENT: {"dockerhub_credential_probe"},
    GITHUB_ENVIRONMENT: {"github_release_publish", "github_release_withdraw"},
}
# The jobs that install packages from the live Debian mirrors at job time. Snapshot
# enforcement is scoped to the shipped image and does not cover these (#65), so a job
# added here is a deliberate extension of that boundary rather than an oversight.
APT_CONSUMER_JOBS = {"validate", "compat_python_floor", "release_contract", "github_release_publish"}
APT_INSTALL_STEP = "apt-get install --yes --no-install-recommends git"
# Detection has to be spelling-independent, or the guard above is opt-in: `apt-get -y install`
# and `apt install` are the same act and neither contains the literal install step.
APT_INSTALL_PATTERN = re.compile(r"\bapt(?:-get)?\b.*\binstall\b")
IMAGE_REF = "registry.example.test/team/robot-dev-team:0123456789abcdef"
# Mocked tests prove that the helper hashes the exact bytes returned by
# `--raw`; registry identity is covered by the pinned-client live check.
RAW_MANIFEST = """{
  "schemaVersion": 2,
  "mediaType": "application/vnd.docker.distribution.manifest.v2+json"
}"""
MANIFEST_DIGEST = f"sha256:{hashlib.sha256(RAW_MANIFEST.encode()).hexdigest()}"


def _run_inspection(
    tmp_path: Path,
    registry_output: str,
    *,
    inspect_exit_code: int = 0,
    force_shasum: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "docker-call.log"
    hash_call_log = tmp_path / "hash-call.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_DOCKER_CALL_LOG"
if [ "$FAKE_INSPECT_EXIT_CODE" -ne 0 ]; then
  exit "$FAKE_INSPECT_EXIT_CODE"
fi
printf '%s' "$FAKE_REGISTRY_OUTPUT"
"""
    )
    fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    env = os.environ.copy()
    if force_shasum:
        for tool_name in ("grep", "mktemp", "rm"):
            tool_path = shutil.which(tool_name)
            assert tool_path is not None
            (bin_dir / tool_name).symlink_to(tool_path)
        fake_shasum = bin_dir / "shasum"
        fake_shasum.write_text(
            """#!/bin/sh
printf 'shasum %s %s\\n' "$1" "$2" > "$FAKE_HASH_CALL_LOG"
printf '%s  %s\\n' "$FAKE_MANIFEST_HASH" "$3"
"""
        )
        fake_shasum.chmod(fake_shasum.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    env.update(
        {
            "PATH": str(bin_dir) if force_shasum else f"{bin_dir}:{env['PATH']}",
            "FAKE_DOCKER_CALL_LOG": str(call_log),
            "FAKE_HASH_CALL_LOG": str(hash_call_log),
            "FAKE_INSPECT_EXIT_CODE": str(inspect_exit_code),
            "FAKE_MANIFEST_HASH": MANIFEST_DIGEST.removeprefix("sha256:"),
            "FAKE_REGISTRY_OUTPUT": registry_output,
        }
    )
    shell_path = shutil.which("sh")
    assert shell_path is not None
    result = subprocess.run(
        [shell_path, str(CI_INSPECT_PUSHED_DIGEST), IMAGE_REF],
        env=env,
        capture_output=True,
        text=True,
    )
    hash_call = hash_call_log.read_text() if hash_call_log.exists() else ""
    return result, call_log.read_text(), hash_call


def test_inspect_pushed_digest_hashes_raw_multiline_manifest(tmp_path: Path) -> None:
    result, docker_call, _ = _run_inspection(tmp_path, RAW_MANIFEST)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{MANIFEST_DIGEST}\n"
    assert docker_call.splitlines() == [
        "buildx",
        "imagetools",
        "inspect",
        "--raw",
        IMAGE_REF,
    ]


def test_inspect_pushed_digest_falls_back_to_shasum(tmp_path: Path) -> None:
    result, _, hash_call = _run_inspection(tmp_path, RAW_MANIFEST, force_shasum=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{MANIFEST_DIGEST}\n"
    assert hash_call == "shasum -a 256\n"


def test_inspect_pushed_digest_rejects_empty_registry_output(tmp_path: Path) -> None:
    result, _, _ = _run_inspection(tmp_path, "")

    assert result.returncode == 1
    assert "pushed manifest digest was not found" in result.stderr
    assert result.stdout == ""


def test_inspect_pushed_digest_reports_registry_inspection_failure(tmp_path: Path) -> None:
    result, _, _ = _run_inspection(tmp_path, "", inspect_exit_code=1)

    assert result.returncode == 1
    assert "pushed manifest could not be inspected" in result.stderr
    assert result.stdout == ""


def ci_config() -> dict[str, Any]:
    loaded = yaml.safe_load((REPO_ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_sbom_publish_stages_the_built_artifact_from_the_publishing_pipeline() -> None:
    """The SBOM upload runs where Python exists and where the digest is actually known."""
    config = ci_config()
    build = config["build_smoke_publish"]
    job = config["sbom_publish"]

    # The digest is only emitted by the dotenv report, and the SBOM only exists as an artifact
    # of the same job, so both handoffs must stay wired.
    assert "artifacts/sbom.spdx.json" in build["artifacts"]["paths"]
    assert build["artifacts"]["reports"]["dotenv"] == "artifacts/image.env"
    assert job["needs"] == [{"job": "build_smoke_publish", "artifacts": True}]

    # A separate job so a failed upload is retryable on its own: re-running the build job
    # would trip its own immutable-SHA-tag guard against the image it already pushed.
    assert job["script"] == ["python scripts/release_tools.py publish-sbom"]
    assert job["image"] == config["validate"]["image"]
    # The protected-ref runner: this job writes durable supply-chain state, and rdt-validate
    # also runs untrusted merge-request containers. No resource_group -- build_smoke_publish
    # has already finished and released image-publication by the time this job starts.
    assert job["tags"] == ["rdt"]
    assert job["tags"] == build["tags"]
    assert job["interruptible"] is False
    assert "resource_group" not in job
    assert job["rules"] == [{"if": PROTECTED_DEFAULT_BRANCH_RULE}, {"when": "never"}]
    assert job["rules"][0] == build["rules"][0]


def test_release_publish_retains_the_released_sbom() -> None:
    assert "artifacts/sbom.spdx.json" in ci_config()["release_publish"]["artifacts"]["paths"]


def test_dockerhub_probe_is_manual_and_protected() -> None:
    config = ci_config()
    job = config["dockerhub_credential_probe"]

    assert job["tags"] == ["rdt"]
    assert job["interruptible"] is False
    assert job["resource_group"] == "release-publication"
    assert job["needs"] == [
        {"job": "build_smoke_publish", "artifacts": True},
        {"job": "security_scan", "artifacts": False},
    ]
    assert job["environment"] == {"name": DOCKERHUB_ENVIRONMENT, "action": "prepare"}
    assert job["rules"] == [
        {"if": PROTECTED_DEFAULT_BRANCH_RULE, "when": "manual", "allow_failure": True},
        {"when": "never"},
    ]
    assert job["script"] == [
        "python scripts/release_tools.py install-tools --tools crane",
        "python scripts/release_tools.py probe-dockerhub",
    ]
    assert job["artifacts"]["paths"] == ["artifacts/dockerhub-probe.json"]


def test_only_approved_jobs_receive_scoped_credentials() -> None:
    """The environment scope is what keeps each publication token out of other protected jobs.

    GitLab CE has no protected environments, so declaring the environment is sufficient to receive
    the credentials. Templates and `default:` are checked too, since `extends` would inherit them.
    The map is closed: a new environment name is an unreviewed grant, so it fails here until it is
    added deliberately -- pinning only the names already known would leave the next one unguarded.
    """
    config = ci_config()
    declaring: dict[str, set[str]] = {}
    for name, entry in config.items():
        if not isinstance(entry, dict) or "environment" not in entry:
            continue
        environment = entry["environment"]
        env_name = environment.get("name") if isinstance(environment, dict) else environment
        assert isinstance(env_name, str)
        # A variable-expanded name could resolve to the scoped environment at run time.
        assert "$" not in env_name, f"{name} has a dynamic environment name"
        declaring.setdefault(env_name, set()).add(name)

    assert declaring == SCOPED_ENVIRONMENT_JOBS
    for jobs in declaring.values():
        for name in jobs:
            assert config[name]["tags"] == ["rdt"]


def _job(name: str) -> dict[str, Any]:
    job = ci_config()[name]
    assert isinstance(job, dict)
    return job


def test_stages_end_with_public_projection() -> None:
    # A stage after `release`, not a same-stage `needs`: nothing public before the canonical
    # release has completed.
    assert ci_config()["stages"] == ["validate", "image", "security", "release", "publish"]


def test_github_publication_is_manual_serialized_and_after_the_release() -> None:
    job = _job("github_release_publish")

    assert job["stage"] == "publish"
    assert job["tags"] == ["rdt"]
    assert job["interruptible"] is False
    # One resource group with the release and the yank, so a projection never interleaves them.
    assert job["resource_group"] == _job("release_publish")["resource_group"]
    # No artifact is consumed: a manual job may be played after 30-day artifacts expire, so every
    # input is read from durable state and the need is for ordering alone.
    assert job["needs"] == [{"job": "release_publish", "artifacts": False}]
    assert job["environment"]["name"] == GITHUB_ENVIRONMENT
    assert "action" not in job["environment"], "publication should record a deployment"
    # Same admission as release_publish, plus the manual final call. Not allow_failure: the tag
    # pipeline stays visibly unfinished until the projection has run.
    assert job["rules"] == [
        {"if": _job("release_publish")["rules"][0]["if"], "when": "manual"},
        {"when": "never"},
    ]
    assert job["script"] == ["python -m scripts.github_publication publish"]
    assert job["artifacts"]["paths"] == ["artifacts/public-release-receipt.json"]
    assert 'git fetch origin "+refs/tags/${CI_COMMIT_TAG}:refs/tags/${CI_COMMIT_TAG}"' in job[
        "before_script"
    ]


def test_every_release_writer_shares_one_resource_group() -> None:
    """GitLab runs one job per resource group at a time, across pipelines.

    `github_release_publish` reads whether its version is yanked once, before it pushes. That is
    only safe because `release_yank` cannot run in the gap: the two share this group. Splitting it
    would open a window in which a yank lands mid-publication and the release goes out as current.
    """
    groups = {
        name: _job(name)["resource_group"]
        for name in (
            "release_publish",
            "release_yank",
            "security_scan_release",
            "github_release_publish",
            "github_release_withdraw",
        )
    }
    assert set(groups.values()) == {"release-publication"}, groups


def test_github_withdrawal_follows_the_yank_without_a_scanner() -> None:
    job = _job("github_release_withdraw")

    assert job["stage"] == "publish"
    assert job["tags"] == ["rdt"]
    assert job["interruptible"] is False
    assert job["resource_group"] == _job("release_yank")["resource_group"]
    assert job["needs"] == [{"job": "release_yank", "artifacts": False}]
    assert job["environment"] == {"name": GITHUB_ENVIRONMENT, "action": "prepare"}
    assert job["rules"] == [{"if": _job("release_yank")["rules"][0]["if"]}, {"when": "never"}]
    # Withdrawal must work during the outage that would fail a scan: nothing is installed.
    assert job["script"] == ["python -m scripts.github_publication withdraw"]
    assert "before_script" not in job


def test_public_projection_jobs_consume_no_artifacts() -> None:
    """Both jobs must work however long after their tag they run, so no input may expire."""
    for name in ("github_release_publish", "github_release_withdraw"):
        job = _job(name)
        assert all(need.get("artifacts") is False for need in job["needs"]), name
        assert not any("--context" in line for line in job["script"]), name


def test_drift_audit_runs_alone_on_the_protected_schedule() -> None:
    config = ci_config()
    job = _job("github_publication_audit")
    schedule = (
        '$CI_PIPELINE_SOURCE == "schedule" && $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH'
    )

    assert {"if": schedule} in config["workflow"]["rules"]
    assert job["rules"] == [
        {"if": f'{schedule} && $CI_COMMIT_REF_PROTECTED == "true"'},
        {"when": "never"},
    ]
    assert job["needs"] == []
    assert job["tags"] == ["rdt"]
    # Tokenless: it must never be able to write to the repository it audits.
    assert "environment" not in job
    assert job["script"] == ["python -m scripts.github_publication audit"]

    # A failed scheduled pipeline is the drift alert, so nothing else may run in it.
    assert config["validate"]["rules"] == [
        {"if": '$CI_PIPELINE_SOURCE == "schedule"', "when": "never"},
        {"when": "on_success"},
    ]
    for name, entry in config.items():
        if not isinstance(entry, dict) or "script" not in entry or name == "github_publication_audit":
            continue
        rules = entry.get("rules")
        assert rules is not None, f"{name} has no rules and would run in the audit pipeline"
        admitting = [rule for rule in rules if rule.get("when") != "never"]
        assert all("schedule" not in str(rule.get("if", "")) for rule in admitting), name
        # Only `validate` may fall through to an unconditional rule, and it excludes schedules.
        if any("if" not in rule for rule in admitting):
            assert name == "validate", f"{name} runs in every pipeline the workflow admits"


def _script_lines(entry: dict[str, Any], key: str) -> list[str]:
    """Return a job's script block as individual commands.

    GitLab accepts a block scalar (`script: |`) as well as a sequence. Unpacking the scalar as
    a sequence iterates *characters*, so a substring test over it is always false and the job
    disappears from any audit built on one.
    """
    raw = entry.get(key) or []
    items = [raw] if isinstance(raw, str) else raw
    return [line.strip() for item in items for line in str(item).splitlines() if line.strip()]


def test_apt_consumers_are_scoped_and_record_the_git_build() -> None:
    """CI installs `git` unpinned by decision, so the job log must say which build ran.

    Pinning the set is the point. The shipped image is the only artifact carrying a digest, an
    SBOM, and digest-keyed scan evidence for a reproducibility claim to bind to, so a new APT
    consumer either contributes nothing to that image -- in which case it belongs in this set
    and in the `docs/CI.md` scope statement -- or it contributes a package or a layer, in which
    case it needs the `Dockerfile`'s enforced snapshot rather than a live mirror. The test is
    about package contents, not about publication: `release_contract` is in this set and does
    declare a release artifact, because what `git` gives it is validation and an annotated tag
    message rather than anything that ends up installed in the image.
    """
    config = ci_config()
    consumers: set[str] = set()
    for name, entry in config.items():
        if not isinstance(entry, dict):
            continue
        before = _script_lines(entry, "before_script")
        steps = [*before, *_script_lines(entry, "script")]
        if not any(APT_INSTALL_PATTERN.search(step) for step in steps):
            continue
        consumers.add(name)
        assert APT_INSTALL_STEP in before, (
            f"{name} installs from a live mirror with an unreviewed step; "
            f"expected {APT_INSTALL_STEP!r} in before_script"
        )
        # `tests/test_wrappers.py` exercises version-sensitive git behaviour, so an unrecorded
        # drift would leave a flipped assertion with nothing to attribute it to. The record has
        # to be in `before_script`: a later failure there -- `release_contract` runs `git fetch`
        # in it -- skips `script:` entirely, losing the build for the run most worth explaining.
        assert "git --version" in before, (
            f"{name} does not record its git build in before_script"
        )
        assert before.index("git --version") > before.index(APT_INSTALL_STEP), (
            f"{name} records its git build before installing git"
        )
        # No second home for DEBIAN_SNAPSHOT: that duplication is what #65 declined to pay.
        assert not any("snapshot.debian.org" in step for step in steps)

    assert consumers == APT_CONSUMER_JOBS


def test_apt_consumer_scope_statement_names_every_consumer() -> None:
    """Bind the prose to the set, since only the set has teeth.

    Adding a fourth consumer turns the pipeline red until `APT_CONSUMER_JOBS` is updated, but
    nothing otherwise forces the scope statements that enumerate the jobs by name to follow --
    which is the likelier decay path now.
    """
    for doc in (REPO_ROOT / "SECURITY.md", REPO_ROOT / "docs" / "CI.md"):
        text = doc.read_text(encoding="utf-8")
        for name in sorted(APT_CONSUMER_JOBS):
            assert f"`{name}`" in text, f"{doc.name} scope statement omits {name}"


def test_build_image_emits_no_digest_when_publication_is_skipped() -> None:
    """`sbom_publish` validates IMAGE_PUBLISHED because the unpublished path emits no digest."""
    script = CI_BUILD_IMAGE.read_text(encoding="utf-8")
    skipped, published = script.split("if [ \"${CI_COMMIT_BRANCH:-}\" != \"$CI_DEFAULT_BRANCH\" ]")

    assert "printf 'IMAGE_PUBLISHED=false\\n' > artifacts/image.env" in skipped
    assert "IMAGE_DIGEST" not in skipped
    assert "printf 'IMAGE_DIGEST=%s\\n' \"$digest\"" in published


def test_inspect_pushed_digest_requires_one_nonempty_reference() -> None:
    for arguments in ([], [""]):
        result = subprocess.run(
            ["sh", str(CI_INSPECT_PUSHED_DIGEST), *arguments],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 2
        assert "usage: ci-inspect-pushed-digest.sh IMAGE_REFERENCE" in result.stderr
        assert result.stdout == ""
