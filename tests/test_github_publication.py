"""Robot Dev Team Project
File: tests/test_github_publication.py
Description: Regression tests for the public GitHub release projection, withdrawal, and audit.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from scripts import github_publication as gp
from scripts import release_tools

VERSION = "0.3.0"
TAG = f"v{VERSION}"
DIGEST = f"sha256:{'a' * 64}"
SERVER_HOST = "gitlab.internal.test"
REGISTRY_HOST = "registry.internal.test"
TOKEN = "github-token-value"
# Authorized at 2026-09-23T12:00:00Z. Every public object is dated from this, never from "now".
TAGGER_EPOCH = 1790164800
NOTES = f"## [{TAG}] - 2026-09-23\n\n- Public projection.\n"


def git(repo: Path, *arguments: str, stdin: bytes | None = None, epoch: int = 1780000000) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Canonical Dev",
        "GIT_AUTHOR_EMAIL": f"dev@{SERVER_HOST}",
        "GIT_COMMITTER_NAME": "Canonical Dev",
        "GIT_COMMITTER_EMAIL": f"dev@{SERVER_HOST}",
        "GIT_AUTHOR_DATE": f"{epoch} +0200",
        "GIT_COMMITTER_DATE": f"{epoch} +0200",
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        input=stdin,
        capture_output=True,
        env=env,
        check=True,
    )
    return result.stdout.decode().strip()


def commit_files(repo: Path, files: dict[str, str], message: str, epoch: int = 1780000000) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "--no-gpg-sign", "-m", message, epoch=epoch)
    return git(repo, "rev-parse", "HEAD")


def make_canonical(tmp_path: Path, files: dict[str, str] | None = None) -> tuple[Path, str]:
    repo = tmp_path / "canonical"
    repo.mkdir()
    git(repo, "init", "--quiet", "--initial-branch=main")
    commit_files(repo, {"README.md": "internal history\n"}, "internal churn")
    commit = commit_files(
        repo,
        files or {"README.md": "Robot Dev Team\n", "docs/CHANGELOG.md": NOTES},
        "release",
    )
    # The canonical tagger carries the private host; nothing public may inherit it.
    git(repo, "tag", "-a", "--no-sign", "-m", "Release", TAG, epoch=TAGGER_EPOCH)
    return repo, commit


@pytest.fixture
def public(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A public remote at a legacy prefix, with `rc` created at the same commit.

    The module's legacy prefix is pointed at this remote's, because publication anchors the first
    release's first parent to it exactly as the audit does.
    """
    seed = tmp_path / "legacy"
    seed.mkdir()
    git(seed, "init", "--quiet", "--initial-branch=main")
    commit_files(seed, {"README.md": "legacy\n", "npm-cache/.gitkeep": ""}, "legacy one")
    legacy = commit_files(seed, {"README.md": "legacy two\n"}, "legacy two")
    remote = tmp_path / "public.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    git(seed, "push", "--quiet", str(remote), "main:refs/heads/main", "main:refs/heads/rc")
    monkeypatch.setattr(gp, "LEGACY_PREFIX_COMMIT", legacy)
    return {"seed": seed, "remote": remote, "legacy": legacy}


def remote_ref(remote: Path, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
    )
    return result.stdout.decode().strip() or None


class FakeGitLab:
    def __init__(self, files: dict[tuple[str, str], bytes]) -> None:
        self.files = files
        self.uploads: list[tuple[str, str]] = []

    def package_file(self, version: str, filename: str, **_: Any) -> bytes | None:
        return self.files.get((version, filename))

    def upload_package_file(self, version: str, filename: str, content: bytes, **_: Any) -> None:
        existing = self.files.get((version, filename))
        if existing is not None and existing != content:
            raise release_tools.ReleaseError("durable release file already exists with different content")
        self.files[(version, filename)] = content
        self.uploads.append((version, filename))

    def package_versions(self) -> list[str]:
        return sorted({version for version, _ in self.files})


def evidence_files(
    commit: str, version: str = VERSION, notes: str = NOTES
) -> dict[tuple[str, str], bytes]:
    manifest = {
        "schema_version": 1,
        "release_version": version,
        "git_tag": f"v{version}",
        "source_commit": commit,
        "source_image": f"{REGISTRY_HOST}:5050/team/robot-dev-team:{commit}",
        "image_digest": DIGEST,
    }
    # The deferred documents carry the registry host by construction. They are hashed into the
    # receipt and never published, so they must not trip the gate.
    return {
        (version, "changelog.md"): notes.encode(),
        (version, "release-manifest.json"): json.dumps(manifest).encode(),
        (version, "sbom.spdx.json"): f'{{"name": "{REGISTRY_HOST}"}}'.encode(),
        (version, "vulnerability-report.json"): f'{{"userInput": "{REGISTRY_HOST}"}}'.encode(),
        (version, "vulnerability-evaluation.json"): b'{"verdict": "pass"}',
    }


class FakeGitHub:
    """In-memory releases, with Git data served from the real bare remote."""

    def __init__(self, remote: Path) -> None:
        self.remote = remote
        self.releases: list[dict[str, Any]] = []
        self.contents: dict[int, bytes] = {}
        self.latest_id: int | None = None
        self.next_id = 100
        self.calls: list[tuple[str, Any]] = []
        self.fail: set[str] = set()
        self.corrupt_uploads = False

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def _release(self, release_id: int) -> dict[str, Any]:
        return next(r for r in self.releases if r["id"] == release_id)

    def _check(self, operation: str) -> None:
        if operation in self.fail:
            self.fail.discard(operation)
            raise release_tools.ReleaseError(f"GitHub API {operation} failed (502)")

    def list_releases(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.releases)

    def create_release(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._check("create")
        self.calls.append(("create", payload))
        record = {**payload, "id": self._id(), "assets": []}
        self.releases.append(record)
        return copy.deepcopy(record)

    def update_release(self, release_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self._check("update")
        self.calls.append(("update", {"id": release_id, **payload}))
        record = self._release(release_id)
        record.update({k: v for k, v in payload.items() if k != "make_latest"})
        # GitHub documents `make_latest` as defaulting to "true" on update, so an unqualified
        # PATCH of a published release makes it latest. Modelled here so code that relies on
        # the safe reading of the API fails in tests rather than on the public repository.
        make_latest = payload.get("make_latest", "true")
        if make_latest == "true" and not record.get("draft"):
            self.latest_id = release_id
        return copy.deepcopy(record)

    def latest_release(self) -> dict[str, Any] | None:
        if self.latest_id is None:
            return None
        return copy.deepcopy(self._release(self.latest_id))

    def upload_asset(self, release_id: int, name: str, content: bytes, content_type: str) -> dict[str, Any]:
        self._check("upload")
        self.calls.append(("upload", name))
        asset = {"id": self._id(), "name": name, "state": "uploaded", "content_type": content_type}
        self.contents[asset["id"]] = content + (b"x" if self.corrupt_uploads else b"")
        self._release(release_id)["assets"].append(asset)
        return dict(asset)

    def delete_asset(self, asset_id: int) -> None:
        self.calls.append(("delete", asset_id))
        for record in self.releases:
            record["assets"] = [a for a in record["assets"] if a["id"] != asset_id]

    def download_asset(self, asset: dict[str, Any]) -> bytes:
        return self.contents[asset["id"]]

    def seed_release(self, tag: str, *, draft: bool, name: str | None = None, body: str = NOTES,
                     assets: dict[str, bytes] | None = None, state: str = "uploaded") -> dict[str, Any]:
        record = {
            "id": self._id(),
            "tag_name": tag,
            "name": name or f"Robot Dev Team {tag}",
            "body": body,
            "draft": draft,
            "assets": [],
        }
        for asset_name, content in (assets or {}).items():
            asset = {"id": self._id(), "name": asset_name, "state": state}
            self.contents[asset["id"]] = content
            record["assets"].append(asset)
        self.releases.append(record)
        return record

    # Git data for the audit, answered from the bare remote.
    def _git(self, *arguments: str) -> str:
        return git(self.remote, *arguments)

    def ref(self, name: str) -> str | None:
        return remote_ref(self.remote, f"refs/{name}")

    def matching_refs(self, prefix: str) -> dict[str, str]:
        raw = self._git("for-each-ref", "--format=%(refname) %(objectname)", f"refs/{prefix}")
        return dict(line.split(" ", 1) for line in raw.splitlines() if line)

    def tag_target(self, tag_object: str) -> str:
        return self._git("rev-parse", f"{tag_object}^{{commit}}")

    def commit(self, sha: str) -> tuple[str, list[str]]:
        return self._git("rev-parse", f"{sha}^{{tree}}"), self._git(
            "rev-list", "--parents", "-n", "1", sha
        ).split()[1:]

    def contains(self, ancestor: str, descendant: str) -> bool:
        return gp.is_ancestor(self.remote, ancestor, descendant)

    def branches(self) -> list[str]:
        raw = self._git("for-each-ref", "--format=%(refname:short)", "refs/heads")
        return raw.splitlines()


def ci_env(commit: str, **overrides: str) -> dict[str, str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "CI_COMMIT_TAG": TAG,
        "CI_COMMIT_SHA": commit,
        "CI_COMMIT_REF_PROTECTED": "true",
        "CI_SERVER_HOST": SERVER_HOST,
        "CI_REGISTRY": f"{REGISTRY_HOST}:5050",
        gp.TOKEN_VARIABLE: TOKEN,
    }
    env.update(overrides)
    return env


@pytest.fixture
def world(tmp_path: Path, public: dict[str, Any]) -> dict[str, Any]:
    canonical, commit = make_canonical(tmp_path)
    return {
        **public,
        "tmp": tmp_path,
        "canonical": canonical,
        "commit": commit,
        "github": FakeGitHub(public["remote"]),
        "gitlab": FakeGitLab(evidence_files(commit)),
    }


def publish(world: dict[str, Any], **env: str) -> dict[str, Any]:
    return gp.publish_to_github(
        artifacts_dir=world["tmp"] / "artifacts",
        source_repo=world["canonical"],
        remote=str(world["remote"]),
        github=world["github"],
        gitlab=world["gitlab"],
        environ=ci_env(world["commit"], **env),
    )


NEXT_VERSION = "0.4.0"
NEXT_NOTES = f"## [v{NEXT_VERSION}] - 2026-09-30\n\n- Next release.\n"


def add_next_release(world: dict[str, Any]) -> str:
    """Tag a second canonical release, v0.4.0, and stage its durable evidence."""
    commit = commit_files(
        world["canonical"], {"docs/CHANGELOG.md": f"{NEXT_NOTES}\n{NOTES}"}, "next release"
    )
    git(
        world["canonical"], "tag", "-a", "--no-sign", "-m", "Release", f"v{NEXT_VERSION}",
        epoch=TAGGER_EPOCH + 86400,
    )
    world["gitlab"].files.update(evidence_files(commit, NEXT_VERSION, NEXT_NOTES))
    return commit


def publish_next(world: dict[str, Any], commit: str) -> dict[str, Any]:
    return publish(world, CI_COMMIT_TAG=f"v{NEXT_VERSION}", CI_COMMIT_SHA=commit)


def push_hand_made_commit(world: dict[str, Any], onto: str = "refs/heads/main") -> str:
    """Append a commit to a public branch from outside the job, as a hand-made write would."""
    seed = world["seed"]
    git(seed, "fetch", "--quiet", str(world["remote"]), onto)
    git(seed, "switch", "--quiet", "--detach", "FETCH_HEAD")
    extra = commit_files(seed, {"HAND.md": "x\n"}, "hand-made")
    git(seed, "push", "--quiet", str(world["remote"]), f"{extra}:{onto}")
    return extra


def public_refs(world: dict[str, Any]) -> dict[str, str | None]:
    return {
        ref: remote_ref(world["remote"], ref)
        for ref in ("refs/heads/main", "refs/heads/rc", f"refs/tags/{TAG}")
    }


# --- Fresh publication ------------------------------------------------------------------------


def test_first_projection_fast_forwards_main_and_rc_onto_the_canonical_tree(world: dict[str, Any]) -> None:
    receipt = publish(world)

    remote = world["remote"]
    commit = remote_ref(remote, "refs/heads/main")
    assert commit == receipt["public"]["commit"]
    assert remote_ref(remote, "refs/heads/rc") == commit
    # A fast-forward onto the untouched legacy prefix, not a rewrite of it.
    assert git(remote, "rev-list", "--parents", "-n", "1", commit).split()[1:] == [world["legacy"]]
    canonical_tree = git(world["canonical"], "rev-parse", f"{TAG}^{{tree}}")
    assert git(remote, "rev-parse", f"{commit}^{{tree}}") == canonical_tree
    assert receipt["public"]["tree"] == receipt["canonical"]["tree"] == canonical_tree
    assert receipt["canonical"]["commit"] == world["commit"]
    # The tag is annotated and points at the release commit.
    assert git(remote, "cat-file", "-t", f"refs/tags/{TAG}") == "tag"
    assert git(remote, "rev-parse", f"refs/tags/{TAG}^{{commit}}") == commit
    # No canonical commit crossed over: the internal history is not reachable from public main.
    assert world["commit"] not in git(remote, "rev-list", "--all").split()


def test_public_identities_are_the_noreply_identity_and_dated_by_the_canonical_tag(
    world: dict[str, Any],
) -> None:
    receipt = publish(world)
    remote = world["remote"]
    commit_object = git(remote, "cat-file", "commit", receipt["public"]["commit"])
    tag_object = git(remote, "cat-file", "tag", receipt["public"]["tag_object"])

    identity = f"{gp.PUBLIC_IDENTITY_NAME} <{gp.PUBLIC_IDENTITY_EMAIL}> {TAGGER_EPOCH} +0000"
    assert f"\nauthor {identity}\n" in commit_object
    assert f"\ncommitter {identity}\n" in commit_object
    assert f"\ntagger {identity}\n" in tag_object
    assert f"{gp.CANONICAL_COMMIT_TRAILER}: {world['commit']}" in commit_object
    for obj in (commit_object, tag_object):
        assert SERVER_HOST not in obj.lower()
        assert "Canonical Dev" not in obj


def test_projection_is_reproducible_from_the_same_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same canonical tag and public parents produce the same public objects, byte for byte."""
    commits = []
    for attempt in ("one", "two"):
        root = tmp_path / attempt
        root.mkdir()
        seed = root / "legacy"
        seed.mkdir()
        git(seed, "init", "--quiet", "--initial-branch=main")
        monkeypatch.setattr(
            gp, "LEGACY_PREFIX_COMMIT", commit_files(seed, {"README.md": "legacy\n"}, "legacy")
        )
        remote = root / "public.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
        git(seed, "push", "--quiet", str(remote), "main:refs/heads/main", "main:refs/heads/rc")
        canonical, commit = make_canonical(root)
        seed_world = {
            "tmp": root,
            "remote": remote,
            "canonical": canonical,
            "commit": commit,
            "github": FakeGitHub(remote),
            "gitlab": FakeGitLab(evidence_files(commit)),
        }
        commits.append(publish(seed_world)["public"])
    assert commits[0] == commits[1]


def test_receipt_binds_both_sides_and_pins_the_deferred_evidence(world: dict[str, Any]) -> None:
    receipt = publish(world)
    files = world["gitlab"].files

    assert receipt["schema_version"] == gp.RECEIPT_SCHEMA
    assert receipt["image"] == {"qualified_digest": DIGEST, "public_references": []}
    for filename in gp.DEFERRED_EVIDENCE:
        entry = receipt["evidence"][filename]
        assert entry["published"] is False
        assert entry["sha256"] == hashlib.sha256(files[(VERSION, filename)]).hexdigest()
    assert receipt["evidence"]["changelog.md"]["published"] is True
    assert receipt["public"]["repository"] == "github.com/mcknly/robot-dev-team"

    # Durable, as an artifact, and as the public asset: the same bytes in all three places.
    raw = files[(VERSION, gp.RECEIPT_FILENAME)]
    assert (world["tmp"] / "artifacts" / gp.RECEIPT_FILENAME).read_bytes() == raw
    assert json.loads(raw) == receipt
    [release] = world["github"].releases
    published = {a["name"]: world["github"].contents[a["id"]] for a in release["assets"]}
    assert published == {gp.RECEIPT_FILENAME: raw, "changelog.md": NOTES.encode()}
    # The receipt names neither host, although the deferred documents it hashes do.
    assert REGISTRY_HOST not in raw.decode() and SERVER_HOST not in raw.decode()


def test_release_is_built_as_a_draft_and_published_only_after_assets_verify(world: dict[str, Any]) -> None:
    publish(world)
    calls = world["github"].calls

    assert calls[0][0] == "create" and calls[0][1]["draft"] is True
    assert [name for op, name in calls if op == "upload"] == ["changelog.md", gp.RECEIPT_FILENAME]
    assert calls[-1] == ("update", {"id": calls[-1][1]["id"], "draft": False, "make_latest": "true"})
    [release] = world["github"].releases
    assert release["draft"] is False
    assert release["name"] == f"Robot Dev Team {TAG}"
    assert release["body"] == NOTES


def test_rc_carrying_accepted_work_becomes_the_second_parent(world: dict[str, Any]) -> None:
    seed = world["seed"]
    git(seed, "switch", "--quiet", "-c", "contributor")
    contributed = commit_files(seed, {"CONTRIBUTED.md": "thanks\n"}, "Contributor change")
    git(seed, "switch", "--quiet", "main")
    git(seed, "merge", "--quiet", "--no-ff", "--no-gpg-sign", "-m", "Merge PR", "contributor")
    merged = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "--quiet", str(world["remote"]), f"{merged}:refs/heads/rc")

    receipt = publish(world)

    assert receipt["public"]["parents"] == [world["legacy"], merged]
    remote = world["remote"]
    assert gp.is_ancestor(remote, contributed, receipt["public"]["commit"])
    assert remote_ref(remote, "refs/heads/rc") == receipt["public"]["commit"]
    # The tree is still the canonical tree: the contributor's file is not in it.
    assert "CONTRIBUTED.md" not in git(remote, "ls-tree", "--name-only", receipt["public"]["commit"])


# --- Fail-closed cases before any public write ------------------------------------------------


def assert_nothing_public(world: dict[str, Any], before: dict[str, str | None]) -> None:
    assert public_refs(world) == before
    assert world["github"].calls == []
    assert (VERSION, gp.RECEIPT_FILENAME) not in world["gitlab"].files


@pytest.mark.parametrize(
    ("content", "role"),
    [
        (f"see https://{SERVER_HOST}/team/project\n", "server"),
        # Title case resolves like lowercase, so the gate must not be case-sensitive.
        ("see Gitlab.Internal.Test for details\n", "server"),
        # A registry on its own host is checked too, not only the server host.
        (f"docker pull {REGISTRY_HOST}:5050/team/image\n", "registry"),
    ],
)
def test_host_gate_rejects_a_tree_naming_a_canonical_host(
    tmp_path: Path, public: dict[str, Any], content: str, role: str
) -> None:
    canonical, commit = make_canonical(
        tmp_path, {"README.md": "Robot Dev Team\n", "docs/RUNBOOK.md": content}
    )
    world = {
        **public,
        "tmp": tmp_path,
        "canonical": canonical,
        "commit": commit,
        "github": FakeGitHub(public["remote"]),
        "gitlab": FakeGitLab(evidence_files(commit)),
    }
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError, match=rf"docs/RUNBOOK.md \({role} host\)"):
        publish(world)
    assert_nothing_public(world, before)


def test_host_gate_rejects_release_notes_naming_a_canonical_host(
    tmp_path: Path, public: dict[str, Any]
) -> None:
    """The release body is the tagged changelog section, so the tree pass is what catches it."""
    notes = f"{NOTES}\nSee https://{SERVER_HOST}/issues/1\n"
    canonical, commit = make_canonical(
        tmp_path, {"README.md": "Robot Dev Team\n", "docs/CHANGELOG.md": notes}
    )
    files = evidence_files(commit)
    files[(VERSION, "changelog.md")] = notes.encode()
    world = {
        **public,
        "tmp": tmp_path,
        "canonical": canonical,
        "commit": commit,
        "github": FakeGitHub(public["remote"]),
        "gitlab": FakeGitLab(files),
    }
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError, match=r"docs/CHANGELOG.md \(server host\)"):
        publish(world)
    assert_nothing_public(world, before)


def test_host_gate_refuses_to_run_without_both_hosts() -> None:
    with pytest.raises(release_tools.ReleaseError, match="registry host is unknown"):
        gp.HostGate.from_env({"CI_SERVER_HOST": SERVER_HOST, "CI_REGISTRY": ""})
    with pytest.raises(release_tools.ReleaseError, match="server host is unknown"):
        gp.HostGate.from_env({"CI_SERVER_HOST": " ", "CI_REGISTRY": REGISTRY_HOST})


@pytest.mark.parametrize(
    ("registry", "host"),
    [
        ("registry.example:5050", "registry.example"),
        ("Registry.Example", "registry.example"),
        ("[::1]:5050", "::1"),
    ],
)
def test_registry_host_strips_the_port(registry: str, host: str) -> None:
    assert gp.registry_host(registry) == host


def test_a_yanked_release_is_never_published(world: dict[str, Any]) -> None:
    world["gitlab"].files[(VERSION, "yank-record.json")] = b"{}"
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError, match="yanked"):
        publish(world)
    assert_nothing_public(world, before)


def test_publication_requires_the_durable_release_evidence(world: dict[str, Any]) -> None:
    del world["gitlab"].files[(VERSION, "vulnerability-evaluation.json")]
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError, match="release_publish must succeed first"):
        publish(world)
    assert_nothing_public(world, before)


def test_unprotected_tags_and_mismatched_durable_state_are_refused(world: dict[str, Any]) -> None:
    before = public_refs(world)
    with pytest.raises(release_tools.ReleaseError, match="unprotected"):
        publish(world, CI_COMMIT_REF_PROTECTED="false")
    # The durable manifest is what proves the release contract passed, for this commit only.
    with pytest.raises(release_tools.ReleaseError, match="different source commit"):
        publish(world, CI_COMMIT_SHA="f" * 40)
    # The notes that become the release body must be the tagged tree's own changelog section.
    world["gitlab"].files[(VERSION, "changelog.md")] = b"## [v0.3.0] - 2026-09-23\n\n- Other.\n"
    with pytest.raises(release_tools.ReleaseError, match="tagged tree's changelog section"):
        publish(world)
    assert_nothing_public(world, before)


def test_publication_needs_no_job_artifact(world: dict[str, Any]) -> None:
    """Played or retried after 30-day artifacts expire, the job still has every input it needs."""
    assert not (world["tmp"] / "artifacts").exists()

    receipt = publish(world)

    assert receipt["release_version"] == VERSION


def test_the_token_is_required(world: dict[str, Any]) -> None:
    with pytest.raises(release_tools.ReleaseError, match=gp.TOKEN_VARIABLE):
        publish(world, **{gp.TOKEN_VARIABLE: ""})


def test_diverged_rc_fails_closed(world: dict[str, Any]) -> None:
    seed = world["seed"]
    git(seed, "switch", "--quiet", "--orphan", "stray")
    stray = commit_files(seed, {"STRAY.md": "x\n"}, "unrelated history")
    git(seed, "push", "--quiet", "--force", str(world["remote"]), f"{stray}:refs/heads/rc")
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError, match="diverged"):
        publish(world)
    assert_nothing_public(world, before)


def test_a_conflicting_public_tag_fails_closed(world: dict[str, Any]) -> None:
    git(world["seed"], "tag", "-a", "--no-sign", "-m", "hand made", TAG)
    git(world["seed"], "push", "--quiet", str(world["remote"]), f"refs/tags/{TAG}")
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError, match="not the tagged canonical tree"):
        publish(world)
    assert_nothing_public(world, before)


def test_a_newer_public_release_blocks_moving_main_backwards(world: dict[str, Any]) -> None:
    git(world["seed"], "tag", "-a", "--no-sign", "-m", "newer", "v0.4.0")
    git(world["seed"], "push", "--quiet", str(world["remote"]), "refs/tags/v0.4.0")
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError, match="backwards"):
        publish(world)
    assert_nothing_public(world, before)


def test_a_moved_rc_rejects_the_whole_atomic_push(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pull request merged between fetch and push must not be reset over, and main must not move.

    The push is atomic, so the rejected `rc` update takes the `main` and tag updates with it.
    """
    real_plan = gp.plan_public_refs

    def plan_then_merge(work: Path, release: gp.CanonicalRelease, anchor: str) -> gp.PublicRefs:
        planned = real_plan(work, release, anchor)
        seed = world["seed"]
        git(seed, "switch", "--quiet", "main")
        late = commit_files(seed, {"LATE.md": "merged in the window\n"}, "late merge")
        git(seed, "push", "--quiet", str(world["remote"]), f"{late}:refs/heads/rc")
        return planned

    monkeypatch.setattr(gp, "plan_public_refs", plan_then_merge)
    legacy = world["legacy"]

    with pytest.raises(release_tools.ReleaseError, match="git push failed"):
        publish(world)
    assert remote_ref(world["remote"], "refs/heads/main") == legacy
    assert remote_ref(world["remote"], f"refs/tags/{TAG}") is None
    assert world["github"].calls == []


def test_rc_moving_on_right_after_the_push_is_not_a_failure(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intake that lands between the push and its verification is intake, not a failed push."""
    real_remote_refs = gp.remote_refs
    merged: list[str] = []

    def merge_then_read(work: Path, remote: str, environ: Any) -> dict[str, str]:
        if not merged:
            seed = world["seed"]
            git(seed, "fetch", "--quiet", str(world["remote"]), "rc")
            git(seed, "switch", "--quiet", "--detach", "FETCH_HEAD")
            merged.append(commit_files(seed, {"AFTER.md": "merged after\n"}, "after merge"))
            git(seed, "push", "--quiet", str(world["remote"]), f"{merged[0]}:refs/heads/rc")
        return real_remote_refs(work, remote, environ)

    monkeypatch.setattr(gp, "remote_refs", merge_then_read)

    receipt = publish(world)

    assert remote_ref(world["remote"], "refs/heads/rc") == merged[0]
    assert gp.is_ancestor(world["remote"], receipt["public"]["commit"], merged[0])
    assert audit(world) == []


def plant_public_release(
    world: dict[str, Any],
    *,
    author: str = gp.PUBLIC_IDENTITY_NAME,
    tag_note: str = "",
    parent: str | None = None,
) -> None:
    """Write a same-tree release commit and tag onto the public remote, outside the job."""
    seed = world["seed"]
    parent = parent or world["legacy"]
    git(seed, "fetch", "--quiet", str(world["canonical"]), f"refs/tags/{TAG}:refs/tags/canonical")
    release = gp.canonical_release(world["canonical"], TAG, world["commit"])
    stamp = f"{release.authorized_at} +0000"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": gp.PUBLIC_IDENTITY_EMAIL,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_NAME": gp.PUBLIC_IDENTITY_NAME,
        "GIT_COMMITTER_EMAIL": gp.PUBLIC_IDENTITY_EMAIL,
        "GIT_COMMITTER_DATE": stamp,
    }
    commit = subprocess.run(
        ["git", "-C", str(seed), "commit-tree", release.tree, "-p", parent],
        input=gp.commit_message(release).encode(),
        capture_output=True,
        env=env,
        check=True,
    ).stdout.decode().strip()
    tag = git(
        seed,
        "mktag",
        stdin=(
            f"object {commit}\ntype commit\ntag {TAG}\n"
            f"tagger {gp.identity_line(release.authorized_at)}\n\n"
            f"{gp.tag_message(release)}{tag_note}"
        ).encode(),
    )
    git(
        seed, "push", "--quiet", str(world["remote"]),
        f"{commit}:refs/heads/main", f"{commit}:refs/heads/rc", f"{tag}:refs/tags/{TAG}",
    )


def test_a_same_tree_tag_this_job_did_not_create_is_not_adopted(world: dict[str, Any]) -> None:
    """Right tree and trailer, different author: adopting it would receipt a foreign commit."""
    plant_public_release(world, author="Someone Else")
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError, match="not the release commit this job creates"):
        publish(world)
    assert_nothing_public(world, before)


def test_a_foreign_tag_object_on_the_right_commit_is_not_adopted(world: dict[str, Any]) -> None:
    plant_public_release(world, tag_note="\nhand-edited\n")
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError, match="not the tag object this job creates"):
        publish(world)
    assert_nothing_public(world, before)


def test_a_hand_made_commit_on_main_is_never_built_upon_before_the_first_release(
    world: dict[str, Any],
) -> None:
    """Building on it would make it the release's first parent: permanent, and receipted."""
    push_hand_made_commit(world)
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError, match="outside publication"):
        publish(world)
    assert_nothing_public(world, before)


def test_a_hand_made_commit_on_main_is_never_built_upon_between_releases(
    world: dict[str, Any],
) -> None:
    publish(world)
    commit = add_next_release(world)
    push_hand_made_commit(world)
    before = public_refs(world)
    calls = len(world["github"].calls)

    with pytest.raises(release_tools.ReleaseError, match="outside publication"):
        publish_next(world, commit)
    assert public_refs(world) == before
    assert len(world["github"].calls) == calls
    assert (NEXT_VERSION, gp.RECEIPT_FILENAME) not in world["gitlab"].files


def test_a_later_release_over_an_unfinished_one_names_the_retry(world: dict[str, Any]) -> None:
    """Same refusal as a hand-made write, but the cure is a retry, and the error has to say so."""
    world["github"].fail.add("create")
    with pytest.raises(release_tools.ReleaseError, match="502"):
        publish(world)
    commit = add_next_release(world)
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError) as caught:
        publish_next(world, commit)

    message = str(caught.value)
    assert f"it is the {TAG} release commit" in message
    assert f"Retry github_release_publish in the {TAG} pipeline first" in message
    assert "outside publication" not in message
    assert public_refs(world) == before

    # Following the advice unblocks it.
    publish(world)
    assert publish_next(world, commit)["public"]["parents"][0] == before["refs/heads/main"]


def test_consecutive_releases_chain_through_their_first_parents(world: dict[str, Any]) -> None:
    first = publish(world)
    second = publish_next(world, add_next_release(world))

    assert second["public"]["parents"] == [first["public"]["commit"]]
    assert remote_ref(world["remote"], "refs/heads/main") == second["public"]["commit"]
    assert audit(world) == []


def test_an_existing_tag_off_the_release_chain_is_not_adopted(world: dict[str, Any]) -> None:
    """Byte-identical objects are not enough when the first parent is not the previous release."""
    hand = push_hand_made_commit(world)
    plant_public_release(world, parent=hand)
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError, match="does not follow"):
        publish(world)
    assert_nothing_public(world, before)


def test_a_release_made_exactly_as_the_job_would_is_adopted(world: dict[str, Any]) -> None:
    """The byte-identity check is exact, not merely strict: the job's own objects still converge."""
    plant_public_release(world)

    receipt = publish(world)

    assert receipt["public"]["commit"] == remote_ref(world["remote"], "refs/heads/main")


# --- Retries converge -------------------------------------------------------------------------


def test_a_completed_publication_reruns_as_a_no_op(world: dict[str, Any]) -> None:
    first = publish(world)
    refs = public_refs(world)
    calls = len(world["github"].calls)

    second = publish(world)

    assert second == first
    assert public_refs(world) == refs
    # Nothing created, uploaded, or updated: a published release is only ever checked.
    assert len(world["github"].calls) == calls


def test_a_failure_after_the_push_converges_on_retry(world: dict[str, Any]) -> None:
    world["github"].fail.add("create")
    with pytest.raises(release_tools.ReleaseError, match="502"):
        publish(world)
    refs = public_refs(world)
    assert refs[f"refs/tags/{TAG}"] is not None
    assert (VERSION, gp.RECEIPT_FILENAME) not in world["gitlab"].files

    receipt = publish(world)

    # The retry reused the public tag rather than minting a second release commit.
    assert public_refs(world) == refs
    assert receipt["public"]["commit"] == refs["refs/heads/main"]
    [release] = world["github"].releases
    assert release["draft"] is False


def test_an_interrupted_upload_on_a_draft_is_repaired(world: dict[str, Any]) -> None:
    seeded = world["github"].seed_release(
        TAG, draft=True, assets={gp.RECEIPT_FILENAME: b"partial"}, state="starter"
    )
    placeholder = seeded["assets"][0]["id"]

    publish(world)

    [release] = world["github"].releases
    assert release["draft"] is False
    assert sorted(a["name"] for a in release["assets"]) == ["changelog.md", gp.RECEIPT_FILENAME]
    assert ("delete", placeholder) in world["github"].calls
    assert placeholder not in {a["id"] for a in release["assets"]}


def test_a_draft_with_stale_notes_is_brought_up_to_date(world: dict[str, Any]) -> None:
    world["github"].seed_release(TAG, draft=True, body="stale")

    publish(world)

    [release] = world["github"].releases
    assert release["body"] == NOTES


def test_a_published_release_with_different_bytes_fails_closed(world: dict[str, Any]) -> None:
    receipt = publish(world)
    [release] = world["github"].releases
    asset = next(a for a in release["assets"] if a["name"] == gp.RECEIPT_FILENAME)
    world["github"].contents[asset["id"]] = b"{}"

    with pytest.raises(release_tools.ReleaseError, match="differs from the bytes"):
        publish(world)
    assert receipt["public"]["commit"] == remote_ref(world["remote"], "refs/heads/main")


def test_a_published_release_missing_an_asset_is_not_repaired(world: dict[str, Any]) -> None:
    """It was public without its evidence; re-uploading would hide that, so it fails closed."""
    publish(world)
    [release] = world["github"].releases
    release["assets"] = [a for a in release["assets"] if a["name"] != "changelog.md"]
    calls = len(world["github"].calls)

    with pytest.raises(release_tools.ReleaseError, match="changelog.md for v0.3.0 is missing"):
        publish(world)
    assert len(world["github"].calls) == calls


def test_a_published_release_with_edited_notes_fails_closed(world: dict[str, Any]) -> None:
    publish(world)
    world["github"].releases[0]["body"] = "edited by hand"

    with pytest.raises(release_tools.ReleaseError, match="differs from the release notes"):
        publish(world)


def test_unexpected_release_assets_fail_closed(world: dict[str, Any]) -> None:
    world["github"].seed_release(TAG, draft=True, assets={"sbom.spdx.json": b"{}"})

    with pytest.raises(release_tools.ReleaseError, match="unexpected assets: sbom.spdx.json"):
        publish(world)


def test_an_upload_that_reads_back_differently_is_never_published(world: dict[str, Any]) -> None:
    world["github"].corrupt_uploads = True

    with pytest.raises(release_tools.ReleaseError, match="differs from the bytes"):
        publish(world)
    [release] = world["github"].releases
    assert release["draft"] is True


def test_duplicate_releases_for_the_tag_fail_closed(world: dict[str, Any]) -> None:
    world["github"].seed_release(TAG, draft=True)
    world["github"].seed_release(TAG, draft=True)

    with pytest.raises(release_tools.ReleaseError, match="2 releases"):
        publish(world)


def test_an_older_release_is_not_marked_latest(world: dict[str, Any]) -> None:
    world["github"].seed_release("v9.0.0", draft=False)

    receipt = publish(world)

    assert receipt["release_version"] == VERSION
    assert world["github"].calls[-1][1]["make_latest"] == "false"


# --- Credential hygiene -----------------------------------------------------------------------


def test_askpass_holds_no_secret_and_the_git_environment_is_isolated(tmp_path: Path) -> None:
    askpass = gp.write_askpass(tmp_path)
    env = gp.isolated_git_environment(
        {"PATH": "/usr/bin", "GIT_ASKPASS": "/elsewhere", "GIT_DIR": "/x"},
        askpass=askpass,
        token=TOKEN,
    )

    assert TOKEN not in askpass.read_text(encoding="utf-8")
    assert oct(askpass.stat().st_mode & 0o777) == "0o700"
    assert env["GIT_ASKPASS"] == str(askpass)
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_DIR" not in env
    answered = subprocess.run(
        [str(askpass), "Password for 'https://x-access-token@github.com': "],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert answered.stdout == f"{TOKEN}\n"
    username = subprocess.run(
        [str(askpass), "Username for 'https://github.com': "],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert username.stdout == "x-access-token\n"


def test_git_commands_carry_no_token_in_arguments(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(arguments: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append([str(item) for item in arguments])
        return real_run(arguments, *args, **kwargs)

    monkeypatch.setattr(gp.subprocess, "run", recording_run)
    publish(world)

    assert seen
    assert not any(TOKEN in item for command in seen for item in command)


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: Any) -> bool:
        return False


def serve(
    monkeypatch: pytest.MonkeyPatch, responses: list[bytes | int]
) -> list[urllib.request.Request]:
    """Answer GitHub requests in order; an int answers with that HTTP status."""
    seen: list[urllib.request.Request] = []
    queue = list(responses)

    def urlopen(request: urllib.request.Request, timeout: int = 0) -> FakeResponse:
        seen.append(request)
        outcome = queue.pop(0)
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(
                request.full_url, outcome, "error", {}, io.BytesIO(b'{"message":"nope"}')  # type: ignore[arg-type]
            )
        return FakeResponse(outcome)

    monkeypatch.setattr(gp.urllib.request, "urlopen", urlopen)
    return seen


def test_release_listing_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    full_page = json.dumps([{"id": index, "tag_name": f"v0.0.{index}"} for index in range(100)])
    seen = serve(monkeypatch, [full_page.encode(), b'[{"id": 100, "tag_name": "v0.1.0"}]'])

    releases = gp.GitHubApi(TOKEN).list_releases()

    assert len(releases) == 101
    assert [urllib.parse.urlsplit(r.full_url).query for r in seen] == [
        "per_page=100&page=1",
        "per_page=100&page=2",
    ]
    assert seen[0].full_url.startswith("https://api.github.com/repos/mcknly/robot-dev-team/releases")


def test_asset_upload_uses_the_upload_host(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(monkeypatch, [b'{"id": 7, "name": "changelog.md", "state": "uploaded"}'])

    gp.GitHubApi(TOKEN).upload_asset(42, "changelog.md", b"notes", "text/markdown")

    [request] = seen
    assert request.full_url == (
        "https://uploads.github.com/repos/mcknly/robot-dev-team/releases/42/assets?name=changelog.md"
    )
    assert request.get_method() == "POST"
    assert request.data == b"notes"
    assert request.headers["Content-type"] == "text/markdown"


def test_asset_download_depends_on_whether_a_token_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = serve(monkeypatch, [b"authenticated", b"anonymous"])
    asset = {
        "name": "changelog.md",
        "url": "https://api.github.com/repos/mcknly/robot-dev-team/releases/assets/7",
        "browser_download_url": "https://github.com/mcknly/robot-dev-team/releases/download/v0.3.0/changelog.md",
    }

    assert gp.GitHubApi(TOKEN).download_asset(asset) == b"authenticated"
    assert gp.GitHubApi(None).download_asset(asset) == b"anonymous"

    # Drafts are only readable through the API; the public URL costs no quota for the audit.
    assert seen[0].full_url == asset["url"]
    assert seen[0].headers["Accept"] == "application/octet-stream"
    assert seen[1].full_url == asset["browser_download_url"]
    assert "Authorization" not in seen[1].unredirected_hdrs


def test_api_errors_name_the_path_and_never_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, [403])

    with pytest.raises(release_tools.ReleaseError) as caught:
        gp.GitHubApi(TOKEN).create_release({"tag_name": TAG})

    message = str(caught.value)
    assert "/repos/mcknly/robot-dev-team/releases failed (403)" in message
    assert TOKEN not in message
    assert "api.github.com" not in message


def test_missing_refs_and_latest_release_read_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, [404, 404])
    api = gp.GitHubApi(None)

    assert api.ref("heads/main") is None
    assert api.latest_release() is None


def test_comparison_reads_ancestry_from_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(
        monkeypatch,
        [b'{"status": "ahead"}', b'{"status": "identical"}', b'{"status": "diverged"}'],
    )
    api = gp.GitHubApi(None)

    assert api.contains("a", "b") is True
    assert api.contains("a", "a") is True
    assert api.contains("a", "c") is False


def test_a_gitlink_in_the_projected_tree_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    commit_files(repo, {"README.md": "x\n"}, "base")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/lib")
    tree = git(repo, "write-tree")
    gate = gp.HostGate((("server", SERVER_HOST), ("registry", REGISTRY_HOST)))

    with pytest.raises(release_tools.ReleaseError, match="commit entry at vendor/lib"):
        gate.check(gp.tree_surfaces(repo, tree))


def test_the_audit_command_fails_on_drift(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda environ=None: (object(), {}))
    monkeypatch.setattr(gp, "audit_github_publication", lambda **_: ["public main moved"])
    monkeypatch.setattr(gp.sys, "argv", ["github_publication.py", "audit"])
    monkeypatch.setenv("CI_SERVER_HOST", SERVER_HOST)
    monkeypatch.setenv("CI_REGISTRY", REGISTRY_HOST)

    assert gp.main() == 1
    error = capsys.readouterr().err
    assert "[github] DRIFT: public main moved" in error
    assert "public repository drift: 1 finding(s)" in error


def test_the_audit_command_needs_both_hosts_to_rebuild_withdrawal_notices(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(release_tools, "gitlab_api_from_env", lambda environ=None: (object(), {}))
    monkeypatch.setattr(gp.sys, "argv", ["github_publication.py", "audit"])
    monkeypatch.delenv("CI_SERVER_HOST", raising=False)
    monkeypatch.delenv("CI_REGISTRY", raising=False)

    assert gp.main() == 1
    assert "canonical server host is unknown" in capsys.readouterr().err


def test_authorization_is_never_forwarded_across_a_redirect() -> None:
    api = gp.GitHubApi(TOKEN)
    request = api.build_request("GET", "https://api.github.com/repos/x/y/releases/assets/1")

    assert request.unredirected_hdrs.get("Authorization") == f"Bearer {TOKEN}"
    assert "Authorization" not in request.headers
    anonymous = gp.GitHubApi(None).build_request("GET", "https://api.github.com/repos/x/y")
    assert "Authorization" not in anonymous.unredirected_hdrs


# --- Withdrawal -------------------------------------------------------------------------------


def yank_record(reason: str = "Broken startup") -> bytes:
    """The immutable record `release_yank` writes; withdrawal reads its reason from here."""
    return json.dumps({"schema_version": 1, "yanked_version": VERSION, "reason": reason}).encode()


def withdraw_now(world: dict[str, Any], **env: str) -> dict[str, Any] | None:
    return gp.withdraw_from_github(
        github=world["github"],
        gitlab=world["gitlab"],
        environ=ci_env(world["commit"], **{"CI_COMMIT_TAG": f"{TAG}-yank", **env}),
    )


def withdraw(world: dict[str, Any], reason: str = "Broken startup") -> dict[str, Any] | None:
    world["gitlab"].files[(VERSION, "yank-record.json")] = yank_record(reason)
    return withdraw_now(world)


def test_withdrawal_marks_the_release_and_moves_latest(world: dict[str, Any]) -> None:
    older = world["github"].seed_release("v0.2.0", draft=False)
    publish(world)
    assert world["github"].latest_id != older["id"]

    record = withdraw(world)

    assert record is not None
    assert record["name"] == f"{gp.WITHDRAWN_PREFIX}Robot Dev Team {TAG}"
    assert record["body"].startswith("> **WITHDRAWN:**")
    assert "Reason: Broken startup" in record["body"]
    assert record["body"].endswith(NOTES)
    assert world["github"].latest_id == older["id"]
    # The tag and the assets stay: they are the audit record of what was published.
    assert remote_ref(world["remote"], f"refs/tags/{TAG}") is not None
    assert len(record["assets"]) == 2


def test_withdrawal_is_idempotent(world: dict[str, Any]) -> None:
    publish(world)
    withdraw(world)
    calls = len(world["github"].calls)

    withdraw(world)

    assert len(world["github"].calls) == calls


def test_withdrawal_without_a_public_release_is_a_no_op(world: dict[str, Any]) -> None:
    assert withdraw(world) is None
    assert world["github"].calls == []


def test_withdrawal_requires_the_canonical_yank_record(world: dict[str, Any]) -> None:
    publish(world)
    with pytest.raises(release_tools.ReleaseError, match="release_yank must succeed first"):
        withdraw_now(world)
    world["gitlab"].files[(VERSION, "yank-record.json")] = b'{"yanked_version": "0.3.0"}'
    with pytest.raises(release_tools.ReleaseError, match="yank record for 0.3.0 is unreadable"):
        withdraw_now(world)


def test_withdrawal_requires_a_protected_yank_tag(world: dict[str, Any]) -> None:
    world["gitlab"].files[(VERSION, "yank-record.json")] = yank_record()
    with pytest.raises(release_tools.ReleaseError, match="protected yank tag"):
        withdraw_now(world, CI_COMMIT_REF_PROTECTED="false")
    with pytest.raises(release_tools.ReleaseError, match="vX.Y.Z-yank"):
        withdraw_now(world, CI_COMMIT_TAG=TAG)


def test_withdrawing_an_older_release_leaves_latest_where_it_is(world: dict[str, Any]) -> None:
    """GitHub defaults `make_latest` to true on update; the rename must not take the badge."""
    newer = world["github"].seed_release("v0.4.0", draft=False)
    world["github"].latest_id = newer["id"]
    publish(world)
    assert world["github"].latest_id == newer["id"]

    withdraw(world)

    assert world["github"].latest_id == newer["id"]
    rename = next(call for op, call in world["github"].calls if op == "update" and "name" in call)
    assert rename["make_latest"] == "false"


def test_a_reason_naming_a_canonical_host_is_withheld_not_blocking(world: dict[str, Any]) -> None:
    """The yank tag cannot be re-cut, so failing here would make public withdrawal impossible."""
    publish(world)

    record = withdraw(world, reason=f"see https://{SERVER_HOST.upper()}/issues/9")

    assert record is not None
    assert record["name"].startswith(gp.WITHDRAWN_PREFIX)
    assert f"Reason: {gp.WITHHELD_REASON}" in record["body"]
    assert SERVER_HOST not in record["body"].lower()
    assert audit(world) == []


# --- Audit ------------------------------------------------------------------------------------


def audit(world: dict[str, Any]) -> list[str]:
    return gp.audit_github_publication(
        github=world["github"],
        gitlab=world["gitlab"],
        gate=gp.HostGate.from_env(ci_env(world["commit"])),
    )


def test_audit_accepts_the_untouched_legacy_prefix(world: dict[str, Any]) -> None:
    assert audit(world) == []


def test_audit_flags_any_write_to_main_before_the_first_release(world: dict[str, Any]) -> None:
    push_hand_made_commit(world)

    assert any("not the legacy prefix" in finding for finding in audit(world))


def test_a_hand_made_main_stays_drift_after_a_refused_publication(world: dict[str, Any]) -> None:
    """The finding reviewers reproduced: publishing used to absorb the drift and clear it."""
    push_hand_made_commit(world)
    with pytest.raises(release_tools.ReleaseError):
        publish(world)

    assert any("not the legacy prefix" in finding for finding in audit(world))


def test_audit_anchors_the_first_parent_chain(world: dict[str, Any]) -> None:
    publish(world)
    publish_next(world, add_next_release(world))
    raw = world["gitlab"].files[(NEXT_VERSION, gp.RECEIPT_FILENAME)]
    receipt = json.loads(raw)
    receipt["public"]["parents"] = [world["legacy"]]
    world["gitlab"].files[(NEXT_VERSION, gp.RECEIPT_FILENAME)] = json.dumps(receipt).encode()

    findings = audit(world)

    assert any(
        f"v{NEXT_VERSION} has first parent {world['legacy']}, not the previous release commit" in f
        for f in findings
    )


def test_audit_accepts_a_published_release(world: dict[str, Any]) -> None:
    publish(world)
    assert audit(world) == []


def test_audit_accepts_a_withdrawn_release(world: dict[str, Any]) -> None:
    publish(world)
    withdraw(world)
    assert audit(world) == []


def test_audit_flags_a_yanked_release_still_presented_as_current(world: dict[str, Any]) -> None:
    publish(world)
    world["gitlab"].files[(VERSION, "yank-record.json")] = yank_record()

    findings = audit(world)
    assert any("WITHDRAWN" in finding for finding in findings)
    assert f"public release notes for {TAG} differ from what was published" in findings


def test_audit_flags_main_moved_off_the_release_line(world: dict[str, Any]) -> None:
    receipt = publish(world)
    extra = push_hand_made_commit(world)

    findings = audit(world)
    assert any(
        f"public main ({extra}) is not the latest release commit ({receipt['public']['commit']})"
        in f
        for f in findings
    )
    assert any("does not descend from public main" in f for f in findings)


def test_audit_flags_unreceipted_tags_branches_and_releases(world: dict[str, Any]) -> None:
    publish(world)
    seed = world["seed"]
    git(seed, "tag", "stray-tag")
    git(seed, "tag", "v0.9.9")
    git(seed, "push", "--quiet", str(world["remote"]), "refs/tags/stray-tag", "refs/tags/v0.9.9")
    git(seed, "push", "--quiet", str(world["remote"]), "main:refs/heads/feature")
    world["github"].seed_release("v0.9.9", draft=False)

    findings = audit(world)

    assert "unexpected public tag stray-tag" in findings
    assert "public tag v0.9.9 has no durable receipt" in findings
    assert "unexpected public branches: feature" in findings
    assert "public release v0.9.9 has no durable receipt" in findings


def test_audit_flags_altered_release_assets(world: dict[str, Any]) -> None:
    publish(world)
    [release] = world["github"].releases
    for asset in release["assets"]:
        world["github"].contents[asset["id"]] += b" "

    findings = audit(world)

    assert f"public receipt for {TAG} differs from the durable receipt" in findings
    assert f"public changelog for {TAG} differs from its receipt" in findings


def test_audit_flags_edited_release_notes(world: dict[str, Any]) -> None:
    """The body is the most visible public surface; an edited download link would live there."""
    publish(world)
    world["github"].releases[0]["body"] = NOTES + "\nDownload it from https://example.invalid/\n"

    assert audit(world) == [f"public release notes for {TAG} differ from what was published"]


def test_audit_flags_a_mangled_withdrawal_notice(world: dict[str, Any]) -> None:
    publish(world)
    withdraw(world)
    world["github"].releases[0]["body"] = "> **WITHDRAWN:** gone\n"

    assert audit(world) == [f"public release notes for {TAG} differ from what was published"]


def test_audit_flags_an_edited_withdrawal_reason(world: dict[str, Any]) -> None:
    """Header and changelog intact, reason swapped for a link: the whole notice is compared."""
    publish(world)
    withdraw(world)
    record = world["github"].releases[0]
    record["body"] = record["body"].replace(
        "Reason: Broken startup", "Reason: download the fix at https://evil.example/x"
    )

    assert audit(world) == [f"public release notes for {TAG} differ from what was published"]


# --- A publication that stopped partway, then a yank -------------------------------------------


def test_a_yank_after_a_stopped_publication_fails_loudly_then_recovers(world: dict[str, Any]) -> None:
    """The tag is public and cannot be deleted; succeeding would leave it looking current forever.

    The recovery is the publication job itself: for a yanked version whose refs already landed, it
    finishes the release as withdrawn -- never as latest -- and writes the receipt, so the audit
    and every later release have their anchor.
    """
    world["github"].fail.add("create")
    with pytest.raises(release_tools.ReleaseError, match="502"):
        publish(world)
    world["gitlab"].files[(VERSION, "yank-record.json")] = yank_record()

    with pytest.raises(release_tools.ReleaseError, match="Retry github_release_publish"):
        withdraw_now(world)
    assert "public tag v0.3.0 has no durable receipt" in audit(world)

    receipt = publish(world)

    [release] = world["github"].releases
    assert release["draft"] is False
    assert release["name"] == f"{gp.WITHDRAWN_PREFIX}Robot Dev Team {TAG}"
    assert "Reason: Broken startup" in release["body"]
    assert world["github"].latest_id is None
    assert world["gitlab"].files[(VERSION, gp.RECEIPT_FILENAME)] == gp.receipt_bytes(receipt)
    calls = len(world["github"].calls)
    withdraw_now(world)
    assert len(world["github"].calls) == calls
    assert audit(world) == []


def test_a_yank_after_a_stopped_upload_finishes_the_draft_as_withdrawn(
    world: dict[str, Any],
) -> None:
    world["github"].fail.add("upload")
    with pytest.raises(release_tools.ReleaseError, match="502"):
        publish(world)
    world["gitlab"].files[(VERSION, "yank-record.json")] = yank_record()

    with pytest.raises(release_tools.ReleaseError, match="never published"):
        withdraw_now(world)

    publish(world)

    [release] = world["github"].releases
    assert release["draft"] is False
    assert release["name"].startswith(gp.WITHDRAWN_PREFIX)
    assert audit(world) == []


def test_a_yanked_version_with_nothing_public_is_still_never_published(
    world: dict[str, Any],
) -> None:
    world["gitlab"].files[(VERSION, "yank-record.json")] = yank_record()
    before = public_refs(world)

    with pytest.raises(release_tools.ReleaseError, match="is yanked; it must not be published"):
        publish(world)
    assert_nothing_public(world, before)
    assert withdraw_now(world) is None


def test_audit_flags_the_latest_marker_on_the_wrong_release(world: dict[str, Any]) -> None:
    publish(world)
    stray = world["github"].seed_release("v0.1.0", draft=False)
    world["github"].releases.remove(stray)
    world["github"].releases.append({**stray})
    world["github"].latest_id = stray["id"]

    assert f"GitHub marks v0.1.0 as latest, not {TAG}" in audit(world)


def test_audit_accepts_any_latest_once_everything_is_withdrawn(world: dict[str, Any]) -> None:
    """GitHub cannot unset "latest", so with nothing left to point at there is nothing to flag."""
    publish(world)
    withdraw(world)

    assert world["github"].latest_id is not None
    assert audit(world) == []


def test_audit_rejects_a_malformed_durable_receipt(world: dict[str, Any]) -> None:
    publish(world)
    receipt = json.loads(world["gitlab"].files[(VERSION, gp.RECEIPT_FILENAME)])
    receipt["public"]["tree"] = "0" * 40
    world["gitlab"].files[(VERSION, gp.RECEIPT_FILENAME)] = json.dumps(receipt).encode()

    with pytest.raises(release_tools.ReleaseError, match="differs from the canonical"):
        audit(world)
