#!/usr/bin/env python3
"""Robot Dev Team Project
File: scripts/github_publication.py
Description: Project a released tree onto the public GitHub repository, withdraw it, and audit it.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from scripts import release_tools
from scripts.release_tools import ReleaseError, Version

GITHUB_REPOSITORY = "mcknly/robot-dev-team"
GITHUB_GIT_URL = f"https://github.com/{GITHUB_REPOSITORY}.git"
GITHUB_API_URL = "https://api.github.com"
# Release asset uploads go to a separate host. It is named here so the egress requirement in
# docs/CI.md has one source of truth to agree with.
GITHUB_UPLOADS_URL = "https://uploads.github.com"
GITHUB_API_VERSION = "2022-11-28"
# The environment-scoped credential. Only jobs declaring `github-publication` receive it, and
# tests/test_ci_scripts.py pins which jobs those are.
TOKEN_VARIABLE = "GITHUB_MIRROR_TOKEN"
# The end of the legacy prefix: public `main` before the first projection. Until a receipt exists,
# the audit expects public `main` to still be exactly this commit.
LEGACY_PREFIX_COMMIT = "447f674ce3c7d59a9dc7a15a4b86112561b1440d"
# Author, committer, and tagger of every public release object. Public `main` is append-only, so an
# identity carrying a canonical host would be permanent; this one is GitHub's noreply form for the
# owning account, which links the commits to that profile and publishes no mailbox.
PUBLIC_IDENTITY_NAME = "MCKNLY LLC"
PUBLIC_IDENTITY_EMAIL = "30472644+mcknly@users.noreply.github.com"
RECEIPT_FILENAME = "public-release-receipt.json"
RECEIPT_SCHEMA = 1
CHANGELOG_FILENAME = "changelog.md"
MANIFEST_FILENAME = "release-manifest.json"
YANK_RECORD_FILENAME = "yank-record.json"
# Pinned by hash in the receipt and deliberately not uploaded: each carries a canonical host by
# construction (docs/MIRRORING.md section 2). A sanitized public variant is #81.
DEFERRED_EVIDENCE = (
    MANIFEST_FILENAME,
    release_tools.SBOM_FILENAME,
    release_tools.SCAN_REPORT_FILENAME,
    release_tools.SCAN_EVALUATION_FILENAME,
)
PUBLISHED_ASSETS = (CHANGELOG_FILENAME, RECEIPT_FILENAME)
ASSET_CONTENT_TYPES = {
    CHANGELOG_FILENAME: "text/markdown",
    RECEIPT_FILENAME: "application/json",
}
WITHDRAWN_PREFIX = "[WITHDRAWN] "
CANONICAL_COMMIT_TRAILER = "Canonical-Commit"
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def release_name(version: Version) -> str:
    return f"Robot Dev Team v{version}"


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# --- The outbound host gate -------------------------------------------------------------------


def registry_host(registry: str) -> str:
    """Return the host component of `CI_REGISTRY`, which may carry a port."""
    value = registry.strip().lower()
    if value.startswith("["):
        value = value[1:].split("]", 1)[0]
    elif ":" in value:
        host, _, port = value.rpartition(":")
        if port.isdigit():
            value = host
    return value


@dataclass(frozen=True)
class HostGate:
    """Refuse any outbound byte that names a canonical host.

    Two hosts, not one: GitLab permits the container registry on its own host or subdomain, and
    the canonical evidence documents carry the *registry* host. Matching is case-insensitive,
    because a title-cased host resolves exactly like the lowercase one.
    """

    hosts: tuple[tuple[str, str], ...]

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> HostGate:
        pairs = (
            ("server", env.get("CI_SERVER_HOST", "").strip().lower()),
            ("registry", registry_host(env.get("CI_REGISTRY", ""))),
        )
        for role, host in pairs:
            # A gate with an empty needle matches nothing and so passes everything.
            if not host:
                raise ReleaseError(f"outbound host gate: the canonical {role} host is unknown")
        return cls(hosts=pairs)

    def roles_in(self, content: bytes) -> list[str]:
        folded = content.lower()
        return [role for role, host in self.hosts if host.encode() in folded]

    def check(self, surfaces: Iterable[tuple[str, bytes]]) -> None:
        offenders: list[str] = []
        for label, content in surfaces:
            roles = self.roles_in(content)
            if roles:
                offenders.append(f"{label} ({'/'.join(roles)} host)")
        if offenders:
            # The host itself is not echoed: the label says where, and the job log is not the
            # place to restate the literal the gate exists to keep out of public view.
            raise ReleaseError(
                "outbound host gate: a canonical host occurs in the outbound surface, so "
                "nothing was published: " + ", ".join(offenders)
            )


# --- Git plumbing -----------------------------------------------------------------------------


def git(
    repo: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    environ: Mapping[str, str] | None = None,
) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        input=input_bytes,
        capture_output=True,
        env=None if environ is None else dict(environ),
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseError(f"git {arguments[0]} failed: {detail or f'exit {result.returncode}'}")
    return result.stdout


def git_text(repo: Path, *arguments: str, environ: Mapping[str, str] | None = None) -> str:
    return git(repo, *arguments, environ=environ).decode("utf-8").strip()


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
    )
    if result.returncode not in (0, 1):
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseError(f"git merge-base failed: {detail}")
    return result.returncode == 0


def resolve(repo: Path, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8").strip()


def tree_surfaces(repo: Path, tree: str) -> Iterator[tuple[str, bytes]]:
    """Yield every path and blob of `tree` for the host gate.

    A gitlink has no content here to check, so it fails closed rather than passing unread.
    """
    raw = git(repo, "ls-tree", "-r", "-z", "--full-tree", tree)
    blobs: list[tuple[str, str]] = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, _, path_bytes = entry.partition(b"\t")
        _mode, kind, sha = meta.decode("ascii").split()
        path = path_bytes.decode("utf-8", errors="replace")
        if kind != "blob":
            raise ReleaseError(
                f"the projected tree carries a {kind} entry at {path}, whose content cannot be "
                "checked by the outbound host gate"
            )
        blobs.append((sha, path))
    yield ("projected tree paths", "\n".join(path for _, path in blobs).encode())
    if not blobs:
        return
    stream = git(repo, "cat-file", "--batch", input_bytes="".join(f"{s}\n" for s, _ in blobs).encode())
    offset = 0
    for sha, path in blobs:
        newline = stream.index(b"\n", offset)
        header = stream[offset:newline].decode("ascii").split()
        if len(header) != 3 or header[0] != sha or header[1] != "blob":
            raise ReleaseError(f"could not read {path} from the projected tree")
        size = int(header[2])
        start = newline + 1
        yield (f"projected tree: {path}", stream[start : start + size])
        offset = start + size + 1


def isolated_git_environment(
    base: Mapping[str, str],
    *,
    askpass: Path | None = None,
    token: str | None = None,
) -> dict[str, str]:
    """An environment in which only the askpass helper can answer a credential prompt.

    No global or system config is read, so no credential helper can supply or store anything,
    and the token never appears in an argument or a remote URL -- both of which survive in
    `git remote -v`, in error text, and in any traced command.
    """
    env = {key: value for key, value in base.items() if not key.startswith("GIT_")}
    env.pop(TOKEN_VARIABLE, None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if askpass is not None:
        if not token:
            raise ReleaseError(f"{TOKEN_VARIABLE} is required")
        env["GIT_ASKPASS"] = str(askpass)
        env[TOKEN_VARIABLE] = token
    return env


def write_askpass(directory: Path) -> Path:
    # The script holds no secret: it reads the token from its environment when git asks.
    path = directory / "askpass.sh"
    path.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  Username*) printf '%s\\n' x-access-token ;;\n"
        f'  *) printf \'%s\\n\' "${TOKEN_VARIABLE}" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def prepare_work_repository(work: Path, source_repo: Path, tree: str) -> None:
    """Create a scratch repository holding only the tagged tree's objects.

    Only the tree is copied, never a canonical commit: nothing the push could reach from the
    public release commit exists here except the tree and what is fetched from the public side.
    That also keeps the CI checkout's shallow boundary out of every walk this job performs.
    """
    subprocess.run(["git", "init", "--bare", "--quiet", str(work)], check=True, capture_output=True)
    listing = git(source_repo, "rev-list", "--objects", tree)
    pack = git(source_repo, "pack-objects", "--stdout", "--quiet", input_bytes=listing)
    git(work, "unpack-objects", "-q", input_bytes=pack)


def fetch_public(work: Path, remote: str, environ: Mapping[str, str]) -> None:
    git(
        work,
        "fetch",
        "--quiet",
        "--no-tags",
        remote,
        "+refs/heads/*:refs/public/heads/*",
        "+refs/tags/*:refs/public/tags/*",
        environ=environ,
    )


def remote_refs(work: Path, remote: str, environ: Mapping[str, str]) -> dict[str, str]:
    raw = git_text(work, "ls-remote", remote, environ=environ)
    refs: dict[str, str] = {}
    for line in raw.splitlines():
        sha, _, name = line.partition("\t")
        refs[name] = sha
    return refs


# --- Canonical inputs -------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalRelease:
    version: Version
    tag: str
    commit: str
    tree: str
    # The canonical tag's tagger time, in epoch seconds. It dates every public object so a retry
    # reproduces them; the canonical tagger's identity and zone are never read.
    authorized_at: int


def canonical_release(source_repo: Path, tag: str, commit: str) -> CanonicalRelease:
    ref = f"refs/tags/{tag}"
    if git_text(source_repo, "cat-file", "-t", ref) != "tag":
        raise ReleaseError(f"{tag} must be an annotated Git tag")
    tagged = git_text(source_repo, "rev-parse", f"{ref}^{{commit}}")
    if tagged != commit:
        raise ReleaseError(f"{tag} points at {tagged}, not the pipeline commit {commit}")
    tree = git_text(source_repo, "rev-parse", f"{ref}^{{tree}}")
    stamp = git_text(source_repo, "for-each-ref", "--format=%(taggerdate:unix)", ref)
    if not stamp.isdigit():
        raise ReleaseError(f"{tag} carries no readable tagger date")
    return CanonicalRelease(Version.from_tag(tag), tag, commit, tree, int(stamp))


def release_evidence(
    api: release_tools.GitLabApi,
    version: Version,
    commit: str,
) -> dict[str, bytes]:
    """Read the durable files `release_publish` wrote, and nothing it did not.

    Read from the version-scoped package rather than job artifacts, which expire: a retry weeks
    later has to publish the same bytes the release authorized. Whether the version is yanked is
    the caller's decision, because it depends on what is already public.
    """
    evidence: dict[str, bytes] = {}
    for filename in (CHANGELOG_FILENAME, *DEFERRED_EVIDENCE):
        content = api.package_file(str(version), filename)
        if content is None:
            raise ReleaseError(
                f"release {version} has no durable {filename}; release_publish must succeed first"
            )
        evidence[filename] = content
    manifest = release_tools.json_object(evidence[MANIFEST_FILENAME], f"release manifest {version}")
    release_tools.validate_release_manifest(manifest, version)
    if manifest.get("source_commit") != commit:
        raise ReleaseError(f"release manifest {version} names a different source commit")
    return evidence


# --- Public objects ---------------------------------------------------------------------------


def identity_line(stamp: int) -> str:
    return f"{PUBLIC_IDENTITY_NAME} <{PUBLIC_IDENTITY_EMAIL}> {stamp} +0000"


def commit_message(release: CanonicalRelease) -> str:
    return (
        f"{release_name(release.version)}\n\n"
        "Public release commit. Its tree is byte-identical to the canonical tagged tree for\n"
        f"{release.tag}. See docs/MIRRORING.md, and the publication receipt attached to the\n"
        "GitHub Release, for what that binds and how to check it.\n\n"
        f"{CANONICAL_COMMIT_TRAILER}: {release.commit}\n"
    )


def tag_message(release: CanonicalRelease) -> str:
    return f"{release_name(release.version)}\n\n{CANONICAL_COMMIT_TRAILER}: {release.commit}\n"


def public_parents(work: Path, main: str, rc: str) -> list[str]:
    """First parent is the previous public release; `rc` joins only when it carries new work.

    `rc` is fast-forwarded onto every release commit, so it descends from `main` unless something
    wrote to it outside this policy. That is drift, and it fails closed.
    """
    if rc == main or is_ancestor(work, rc, main):
        return [main]
    if is_ancestor(work, main, rc):
        return [main, rc]
    raise ReleaseError(
        f"public rc ({rc}) has diverged from public main ({main}); reconcile it before publishing"
    )


def create_release_commit(work: Path, release: CanonicalRelease, parents: Sequence[str]) -> str:
    identity = {
        "GIT_AUTHOR_NAME": PUBLIC_IDENTITY_NAME,
        "GIT_AUTHOR_EMAIL": PUBLIC_IDENTITY_EMAIL,
        "GIT_AUTHOR_DATE": f"{release.authorized_at} +0000",
        "GIT_COMMITTER_NAME": PUBLIC_IDENTITY_NAME,
        "GIT_COMMITTER_EMAIL": PUBLIC_IDENTITY_EMAIL,
        "GIT_COMMITTER_DATE": f"{release.authorized_at} +0000",
    }
    env = {**isolated_git_environment(os.environ), **identity}
    arguments = ["commit-tree", release.tree]
    for parent in parents:
        arguments += ["-p", parent]
    return git(
        work, *arguments, input_bytes=commit_message(release).encode(), environ=env
    ).decode().strip()


def create_release_tag(work: Path, release: CanonicalRelease, commit: str) -> str:
    body = (
        f"object {commit}\n"
        "type commit\n"
        f"tag {release.tag}\n"
        f"tagger {identity_line(release.authorized_at)}\n\n"
        f"{tag_message(release)}"
    )
    return git(work, "mktag", input_bytes=body.encode()).decode().strip()


def public_tag_names(work: Path) -> list[str]:
    raw = git_text(work, "for-each-ref", "--format=%(refname)", "refs/public/tags/")
    return [line.removeprefix("refs/public/tags/") for line in raw.splitlines() if line]


def unfinished_publication_hint(work: Path, main: str) -> str | None:
    """Name the stable public tag on `main`, when an earlier publication stopped after its push.

    The anchor guard is right to refuse either way, but the two causes need opposite responses: a
    hand-made write is drift to investigate, while a tagged release commit with no receipt is an
    earlier run that only needs retrying. A stable tag on main with a receipt would itself be the
    anchor, so any tag found here is one whose publication never finished.
    """
    for tag in sorted(public_tag_names(work)):
        if release_tools.STABLE_TAG.fullmatch(tag) is None:
            continue
        if git_text(work, "rev-parse", f"refs/public/tags/{tag}^{{commit}}") == main:
            return (
                f"it is the {tag} release commit, whose publication stopped before writing its "
                f"receipt. Retry github_release_publish in the {tag} pipeline first"
            )
    return None


def commit_parents(work: Path, commit: str) -> list[str]:
    return git_text(work, "rev-list", "--parents", "-n", "1", commit).split()[1:]


def verify_existing_public_tag(
    work: Path,
    release: CanonicalRelease,
    tag_object: str,
) -> str:
    """Accept a public tag only if it is exactly the one this release would have created.

    Byte identity, not resemblance: the public objects are deterministic, so the commit is rebuilt
    from its recorded parents and the tag from that commit, and both object ids must match. A tag
    with the right tree and trailer but any other author, date, message, or tagger is foreign, and
    adopting it would issue a receipt for objects this job never made.
    """
    if git_text(work, "cat-file", "-t", tag_object) != "tag":
        raise ReleaseError(f"public tag {release.tag} is not an annotated tag")
    commit = git_text(work, "rev-parse", f"{tag_object}^{{commit}}")
    if git_text(work, "rev-parse", f"{commit}^{{tree}}") != release.tree:
        raise ReleaseError(
            f"public tag {release.tag} points at {commit}, whose tree is not the tagged "
            "canonical tree"
        )
    if create_release_commit(work, release, commit_parents(work, commit)) != commit:
        raise ReleaseError(
            f"public tag {release.tag} points at {commit}, which is not the release commit this "
            "job creates; establish what wrote it before publishing"
        )
    if create_release_tag(work, release, commit) != tag_object:
        raise ReleaseError(
            f"public tag {release.tag} is not the tag object this job creates; establish what "
            "wrote it before publishing"
        )
    return commit


@dataclass(frozen=True)
class PublicRefs:
    commit: str
    tag_object: str
    parents: tuple[str, ...]
    # Remote ref updates still to push, as (object, destination ref). Empty on a converged retry.
    updates: tuple[tuple[str, str], ...]


def release_anchor(receipts: Mapping[Version, tuple[dict[str, Any], bytes]], version: Version) -> str:
    """The commit a release's first parent must be: the previous receipted release, or the prefix.

    Withdrawn releases count -- they stay on `main`. Anchoring to the durable receipts rather than
    to whatever public `main` happens to be is what stops a hand-made commit on `main` from being
    adopted as a parent and so made permanent, and receipted, public ancestry.
    """
    earlier = [candidate for candidate in receipts if candidate < version]
    if not earlier:
        return LEGACY_PREFIX_COMMIT
    return str(receipts[max(earlier)][0]["public"]["commit"])


def plan_public_refs(work: Path, release: CanonicalRelease, anchor: str) -> PublicRefs:
    main = resolve(work, "refs/public/heads/main")
    rc = resolve(work, "refs/public/heads/rc")
    if main is None:
        raise ReleaseError("public main does not exist; the legacy prefix must be in place")
    if rc is None:
        raise ReleaseError("public rc does not exist; create it at the end of the legacy prefix")
    existing = resolve(work, f"refs/public/tags/{release.tag}")

    if existing is not None:
        commit = verify_existing_public_tag(work, release, existing)
        if commit_parents(work, commit)[0] != anchor:
            raise ReleaseError(
                f"public release commit for {release.tag} does not follow {anchor}, the previous "
                "release commit, as its first parent; establish what wrote it before publishing"
            )
        if not is_ancestor(work, commit, main):
            raise ReleaseError(f"public tag {release.tag} is not contained in public main")
        if not is_ancestor(work, commit, rc):
            raise ReleaseError(f"public rc does not descend from the {release.tag} release commit")
        return PublicRefs(commit, existing, tuple(commit_parents(work, commit)), ())

    # Public main is the latest published release. A lower version landing on top of a higher one
    # would regress it, and main is append-only, so the regression would be permanent.
    newer = sorted(
        Version.from_tag(tag)
        for tag in public_tag_names(work)
        if release_tools.STABLE_TAG.fullmatch(tag) and Version.from_tag(tag) > release.version
    )
    if newer:
        raise ReleaseError(
            f"public v{newer[-1]} is newer than {release.tag}; publishing it would move public "
            "main backwards"
        )
    # No tag, so nothing of this release is public, and public main must be exactly the previous
    # release. Anything else was written outside the job, and building on it would make it the
    # new release's first parent: permanent, receipted, and no longer visible to the audit.
    if main != anchor:
        raise ReleaseError(f"public main is {main}, not {anchor}, the previous release commit; " + (
            unfinished_publication_hint(work, main)
            or "something wrote to public main outside publication. Establish what before publishing"
        ))
    parents = public_parents(work, main, rc)
    commit = create_release_commit(work, release, parents)
    tag_object = create_release_tag(work, release, commit)
    return PublicRefs(
        commit,
        tag_object,
        tuple(parents),
        (
            (commit, "refs/heads/main"),
            (commit, "refs/heads/rc"),
            (tag_object, f"refs/tags/{release.tag}"),
        ),
    )


def push_public_refs(work: Path, remote: str, refs: PublicRefs, environ: Mapping[str, str]) -> None:
    """Push every ref in one atomic update, never forced.

    Atomic, so a retry never finds main moved without its tag or rc. Unforced, so a ref that moved
    since the fetch -- an `rc` merge in the window, above all -- rejects the whole push rather
    than being reset over.
    """
    if not refs.updates:
        return
    specs = [f"{source}:{destination}" for source, destination in refs.updates]
    git(work, "push", "--atomic", "--quiet", remote, *specs, environ=environ)
    observed = remote_refs(work, remote, environ)
    for source, destination in refs.updates:
        landed = observed.get(destination)
        if landed == source:
            continue
        # A pull request merged into `rc` straight after the push moves it on. That is intake
        # doing its job, not a failed push, so `rc` only has to descend from the release commit.
        if destination == "refs/heads/rc" and landed is not None:
            git(work, "fetch", "--quiet", remote, "+refs/heads/rc:refs/public/heads/rc", environ=environ)
            if is_ancestor(work, source, landed):
                continue
        raise ReleaseError(f"public {destination} did not land at {source} after the push")


# --- Receipt ----------------------------------------------------------------------------------


def build_receipt(
    release: CanonicalRelease,
    refs: PublicRefs,
    evidence: Mapping[str, bytes],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """The public binding between the canonical release and its projection.

    Deterministic by construction -- no timestamp of its own -- so a retry produces the same bytes
    and a same-name asset with different bytes can be treated as the conflict it is.
    """
    return {
        "schema_version": RECEIPT_SCHEMA,
        "release_version": str(release.version),
        "canonical": {
            "tag": release.tag,
            "commit": release.commit,
            "tree": release.tree,
        },
        "public": {
            "repository": f"github.com/{GITHUB_REPOSITORY}",
            "tag": release.tag,
            "tag_object": refs.tag_object,
            "commit": refs.commit,
            "tree": release.tree,
            "parents": list(refs.parents),
        },
        "image": {
            "qualified_digest": release_tools.validated_manifest_digest(manifest, release.version),
            # Public registry references arrive with Docker Hub promotion (#8). Until then the list
            # is empty rather than absent, so the schema does not change when it fills.
            "public_references": [],
        },
        "evidence": {
            filename: {
                "sha256": sha256_hex(evidence[filename]),
                "published": filename in PUBLISHED_ASSETS,
            }
            for filename in sorted(evidence)
        },
    }


def receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    return (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()


def validate_receipt(receipt: Mapping[str, Any], version: Version) -> None:
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise ReleaseError(f"receipt {version} has an unsupported schema")
    if receipt.get("release_version") != str(version):
        raise ReleaseError(f"receipt {version} names a different version")
    public = receipt.get("public")
    canonical = receipt.get("canonical")
    if not isinstance(public, dict) or not isinstance(canonical, dict):
        raise ReleaseError(f"receipt {version} is missing its canonical or public binding")
    for key in ("tag_object", "commit", "tree"):
        if SHA1.fullmatch(str(public.get(key, ""))) is None:
            raise ReleaseError(f"receipt {version} has an invalid public {key}")
    if public.get("tree") != canonical.get("tree"):
        raise ReleaseError(f"receipt {version} binds a public tree that differs from the canonical")
    parents = public.get("parents")
    if not isinstance(parents, list) or not parents:
        raise ReleaseError(f"receipt {version} records no public parents")
    evidence = receipt.get("evidence")
    if not isinstance(evidence, dict) or CHANGELOG_FILENAME not in evidence:
        raise ReleaseError(f"receipt {version} records no evidence hashes")


# --- GitHub API -------------------------------------------------------------------------------


class GitHubApi:
    """A minimal REST client. The token, when present, is never sent across a redirect.

    Asset downloads redirect to object storage that rejects a second authorization mechanism, and
    a bearer token has no business reaching a host other than the one it was issued for.
    """

    def __init__(
        self,
        token: str | None,
        *,
        repository: str = GITHUB_REPOSITORY,
        api_url: str = GITHUB_API_URL,
        uploads_url: str = GITHUB_UPLOADS_URL,
    ) -> None:
        self.token = token
        self.repository = repository
        self.api_url = api_url.rstrip("/")
        self.uploads_url = uploads_url.rstrip("/")

    def build_request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        accept: str = "application/vnd.github+json",
    ) -> urllib.request.Request:
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Accept", accept)
        request.add_header("X-GitHub-Api-Version", GITHUB_API_VERSION)
        request.add_header("User-Agent", "robot-dev-team-release-ci")
        if content_type is not None:
            request.add_header("Content-Type", content_type)
        if self.token:
            request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        return request

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        accept: str = "application/vnd.github+json",
        allow_missing: bool = False,
    ) -> bytes | None:
        request = self.build_request(
            method, url, body=body, content_type=content_type, accept=accept
        )
        described = urllib.parse.urlsplit(url).path
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw: bytes = response.read()
                return raw
        except urllib.error.HTTPError as exc:
            if allow_missing and exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", errors="replace")
            raise ReleaseError(f"GitHub API {method} {described} failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise ReleaseError(f"GitHub API {method} {described} failed: {exc}") from exc

    def repo_url(self, path: str) -> str:
        return f"{self.api_url}/repos/{self.repository}{path}"

    def call(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode()
        raw = self.request(
            method,
            self.repo_url(path),
            body=body,
            content_type=None if payload is None else "application/json",
        )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"GitHub API {method} {path} returned invalid JSON") from exc

    def paginated(self, path: str) -> list[Any]:
        items: list[Any] = []
        page = 1
        separator = "&" if "?" in path else "?"
        while True:
            batch = self.call("GET", f"{path}{separator}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ReleaseError(f"GitHub API GET {path} did not return an array")
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    # Releases. Listing is the only lookup that also returns drafts, which a retry must find.
    def list_releases(self) -> list[dict[str, Any]]:
        return [item for item in self.paginated("/releases") if isinstance(item, dict)]

    def create_release(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._object(self.call("POST", "/releases", payload), "created release")

    def update_release(self, release_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._object(
            self.call("PATCH", f"/releases/{release_id}", payload), "updated release"
        )

    def latest_release(self) -> dict[str, Any] | None:
        raw = self.request("GET", self.repo_url("/releases/latest"), allow_missing=True)
        if raw is None:
            return None
        return release_tools.json_object(raw, "GitHub latest release")

    def upload_asset(
        self, release_id: int, name: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({"name": name})
        url = f"{self.uploads_url}/repos/{self.repository}/releases/{release_id}/assets?{query}"
        raw = self.request("POST", url, body=content, content_type=content_type)
        return release_tools.json_object(raw or b"", f"uploaded asset {name}")

    def delete_asset(self, asset_id: int) -> None:
        self.request("DELETE", self.repo_url(f"/releases/assets/{asset_id}"))

    def download_asset(self, asset: Mapping[str, Any]) -> bytes:
        if self.token:
            url = str(asset.get("url", ""))
            accept = "application/octet-stream"
        else:
            # Anonymous readers use the public download URL, which costs no API quota.
            url = str(asset.get("browser_download_url", ""))
            accept = "*/*"
        if not url:
            raise ReleaseError(f"release asset {asset.get('name')!r} has no download URL")
        raw = self.request("GET", url, accept=accept)
        return raw or b""

    # Read-only Git data, used by the audit so it needs neither git nor a token.
    def ref(self, name: str) -> str | None:
        raw = self.request("GET", self.repo_url(f"/git/ref/{name}"), allow_missing=True)
        if raw is None:
            return None
        record = release_tools.json_object(raw, f"GitHub ref {name}")
        target = record.get("object")
        if not isinstance(target, dict) or not isinstance(target.get("sha"), str):
            raise ReleaseError(f"GitHub ref {name} has no readable target")
        return str(target["sha"])

    def matching_refs(self, prefix: str) -> dict[str, str]:
        records = self.call("GET", f"/git/matching-refs/{prefix}")
        if not isinstance(records, list):
            raise ReleaseError(f"GitHub refs under {prefix} are unreadable")
        refs: dict[str, str] = {}
        for record in records:
            target = record.get("object") if isinstance(record, dict) else None
            if not isinstance(target, dict) or not isinstance(record.get("ref"), str):
                raise ReleaseError(f"GitHub ref record is unreadable: {record!r}")
            refs[record["ref"]] = str(target.get("sha"))
        return refs

    def tag_target(self, tag_object: str) -> str:
        record = self._object(self.call("GET", f"/git/tags/{tag_object}"), "tag object")
        target = record.get("object")
        if not isinstance(target, dict) or target.get("type") != "commit":
            raise ReleaseError(f"public tag object {tag_object} does not point at a commit")
        return str(target.get("sha"))

    def commit(self, sha: str) -> tuple[str, list[str]]:
        record = self._object(self.call("GET", f"/git/commits/{sha}"), "commit")
        tree = record.get("tree")
        parents = record.get("parents")
        if not isinstance(tree, dict) or not isinstance(parents, list):
            raise ReleaseError(f"public commit {sha} is unreadable")
        return str(tree.get("sha")), [str(parent.get("sha")) for parent in parents]

    def contains(self, ancestor: str, descendant: str) -> bool:
        record = self._object(
            self.call("GET", f"/compare/{ancestor}...{descendant}"), "comparison"
        )
        return record.get("status") in ("identical", "ahead")

    def branches(self) -> list[str]:
        return [str(item.get("name")) for item in self.paginated("/branches")]

    @staticmethod
    def _object(value: Any, description: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ReleaseError(f"GitHub {description} is not a JSON object")
        return value


# --- GitHub Release reconciliation ------------------------------------------------------------


def normalized_body(body: Any) -> str:
    return str(body or "").replace("\r\n", "\n").strip()


def releases_for_tag(releases: Iterable[Mapping[str, Any]], tag: str) -> list[dict[str, Any]]:
    return [dict(release) for release in releases if release.get("tag_name") == tag]


def published_stable_versions(
    releases: Iterable[Mapping[str, Any]],
    *,
    exclude: str | None = None,
) -> dict[Version, dict[str, Any]]:
    versions: dict[Version, dict[str, Any]] = {}
    for release in releases:
        tag = str(release.get("tag_name", ""))
        if release.get("draft") or tag == exclude or release_tools.STABLE_TAG.fullmatch(tag) is None:
            continue
        if str(release.get("name", "")).startswith(WITHDRAWN_PREFIX):
            continue
        versions[Version.from_tag(tag)] = dict(release)
    return versions


def reconcile_release(
    github: GitHubApi,
    *,
    release: CanonicalRelease,
    name: str,
    body: str,
    assets: Mapping[str, bytes],
    withdrawn: bool = False,
) -> dict[str, Any]:
    """Create or converge the GitHub Release as draft, attach assets, verify them, then publish.

    Assets go onto a draft first so a release is never visible without them, and so a repository
    with immutable releases enabled can still converge on a retry. A published release is only
    ever checked, never rewritten: a mismatch there is unexplained state and fails closed. A
    withdrawn release is completed under its withdrawn name and notes and is never made latest.
    """
    all_releases = github.list_releases()
    matches = releases_for_tag(all_releases, release.tag)
    if len(matches) > 1:
        raise ReleaseError(f"GitHub carries {len(matches)} releases for {release.tag}")
    record = converge_release_record(
        github,
        matches[0] if matches else None,
        tag=release.tag,
        name=name,
        body=body,
    )
    converge_release_assets(github, record, tag=release.tag, assets=assets)
    if record.get("draft"):
        others = published_stable_versions(all_releases, exclude=release.tag)
        newest = all(release.version > other for other in others)
        make_latest = "true" if newest and not withdrawn else "false"
        record = github.update_release(
            int(record["id"]), {"draft": False, "make_latest": make_latest}
        )
    return record


def converge_release_record(
    github: GitHubApi,
    record: dict[str, Any] | None,
    *,
    tag: str,
    name: str,
    body: str,
) -> dict[str, Any]:
    if record is None:
        return github.create_release(
            {"tag_name": tag, "name": name, "body": body, "draft": True, "prerelease": False}
        )
    matches = record.get("name") == name and normalized_body(record.get("body")) == body.strip()
    if matches:
        return record
    if not record.get("draft"):
        raise ReleaseError(f"the published GitHub Release for {tag} differs from the release notes")
    return github.update_release(int(record["id"]), {"name": name, "body": body})


def converge_release_assets(
    github: GitHubApi,
    record: Mapping[str, Any],
    *,
    tag: str,
    assets: Mapping[str, bytes],
) -> None:
    """Attach every asset exactly once and read each one back before the release is published."""
    draft = bool(record.get("draft"))
    existing = {str(asset.get("name")): dict(asset) for asset in record.get("assets") or []}
    unexpected = sorted(set(existing) - set(assets))
    if unexpected:
        raise ReleaseError(
            f"the GitHub Release for {tag} carries unexpected assets: {', '.join(unexpected)}"
        )
    for filename, content in assets.items():
        asset = existing.get(filename)
        # A published release is only ever checked. It became public with every asset attached, so
        # a missing or incomplete one means something removed it; repairing that silently would
        # hide a release that was visible without its evidence. Only a draft is repaired.
        if not draft and (asset is None or asset.get("state") != "uploaded"):
            state = "missing" if asset is None else "incomplete"
            raise ReleaseError(f"published asset {filename} for {tag} is {state}")
        if asset is not None and asset.get("state") != "uploaded":
            # An interrupted upload leaves a placeholder.
            github.delete_asset(int(asset["id"]))
            asset = None
        if asset is None:
            asset = github.upload_asset(
                int(record["id"]), filename, content, ASSET_CONTENT_TYPES[filename]
            )
        if sha256_hex(github.download_asset(asset)) != sha256_hex(content):
            raise ReleaseError(
                f"GitHub Release asset {filename} for {tag} differs from the bytes this release "
                "publishes"
            )


# --- Entry points -----------------------------------------------------------------------------


def tagged_changelog(source_repo: Path, release: CanonicalRelease) -> str:
    """The release's changelog section, read from the tagged tree rather than the worktree."""
    content = git(source_repo, "show", f"{release.tree}:docs/CHANGELOG.md")
    with tempfile.TemporaryDirectory(prefix="github-publication-changelog-") as scratch:
        path = Path(scratch) / "CHANGELOG.md"
        path.write_bytes(content)
        excerpt, _ = release_tools.changelog_excerpt(path, release.version)
    return excerpt


def publish_to_github(
    *,
    artifacts_dir: Path,
    source_repo: Path = Path("."),
    remote: str = GITHUB_GIT_URL,
    github: GitHubApi | None = None,
    gitlab: release_tools.GitLabApi | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Publish the release the pipeline's protected tag names.

    Every input is durable: the protected tag in the checkout, and the files `release_publish`
    wrote to the version-scoped package. Nothing is read from a job artifact, so the job can be
    played or retried however long after the tag -- artifacts expire, and a manual job leaves its
    pipeline blocked, so they are not kept as the latest successful ones either.
    """
    source_env = os.environ if environ is None else environ
    env = release_tools.require_env(
        (
            "CI_COMMIT_TAG",
            "CI_COMMIT_SHA",
            "CI_COMMIT_REF_PROTECTED",
            "CI_SERVER_HOST",
            "CI_REGISTRY",
            TOKEN_VARIABLE,
        ),
        source_env,
    )
    if env["CI_COMMIT_REF_PROTECTED"] != "true":
        raise ReleaseError("refusing to publish from an unprotected release tag")
    version = Version.from_tag(env["CI_COMMIT_TAG"])
    gate = HostGate.from_env(env)

    if gitlab is None:
        gitlab, _ = release_tools.gitlab_api_from_env(source_env)
    if github is None:
        github = GitHubApi(env[TOKEN_VARIABLE])
    # The durable manifest is what proves the release contract passed: release_publish writes it
    # only after validating the tag, and it binds the version, tag, and commit checked here.
    evidence = release_evidence(gitlab, version, env["CI_COMMIT_SHA"])
    manifest = release_tools.json_object(evidence[MANIFEST_FILENAME], f"release manifest {version}")
    notes = evidence[CHANGELOG_FILENAME]
    # Read once, before planning. A yank cannot land between this read and the push: release_yank
    # shares this job's `release-publication` resource group, so GitLab runs the two strictly one
    # after the other, and the withdrawal job only follows the yank. tests/test_ci_scripts.py pins
    # the shared group; if it is ever split, this read has to be repeated after the push.
    receipts, yanked_versions = load_receipts(gitlab)
    yanked = version in yanked_versions
    release = canonical_release(source_repo.resolve(), env["CI_COMMIT_TAG"], env["CI_COMMIT_SHA"])
    # The tree is checked before anything is fetched or written: it is the largest surface, and
    # a finding here needs a canonical fix and a new tag, never a retry.
    gate.check(tree_surfaces(source_repo.resolve(), release.tree))
    if notes.decode("utf-8") != tagged_changelog(source_repo.resolve(), release):
        raise ReleaseError(
            f"durable changelog for {version} differs from the tagged tree's changelog section"
        )

    with tempfile.TemporaryDirectory(prefix="github-publication-") as scratch:
        scratch_dir = Path(scratch)
        work = scratch_dir / "work.git"
        prepare_work_repository(work, source_repo.resolve(), release.tree)
        remote_env = isolated_git_environment(
            source_env,
            askpass=write_askpass(scratch_dir),
            token=env[TOKEN_VARIABLE],
        )
        fetch_public(work, remote, remote_env)
        refs = plan_public_refs(work, release, release_anchor(receipts, version))
        # A yanked version is never made public. The one exception is completing a publication
        # that already pushed its refs before the yank: that tag cannot be deleted, and leaving it
        # without a release or receipt would block every later release. Nothing new is pushed.
        if yanked and refs.updates:
            raise ReleaseError(f"release {version} is yanked; it must not be published")

        receipt = build_receipt(release, refs, evidence, manifest)
        receipt_content = receipt_bytes(receipt)
        name, body = release_presentation(gitlab, gate, version, notes.decode("utf-8"), yanked)
        assets = {CHANGELOG_FILENAME: notes, RECEIPT_FILENAME: receipt_content}
        # The whole outbound surface, checked before the first public write. The identities are
        # inside the commit and tag objects, which is the only form in which they are published.
        gate.check(
            [
                ("public release commit object", git(work, "cat-file", "commit", refs.commit)),
                ("public tag object", git(work, "cat-file", "tag", refs.tag_object)),
                ("release name", name.encode()),
                ("release body", body.encode()),
                *((f"release asset {asset}", content) for asset, content in assets.items()),
            ]
        )
        push_public_refs(work, remote, refs, remote_env)

    reconcile_release(
        github, release=release, name=name, body=body, assets=assets, withdrawn=yanked
    )
    # Recorded durably only once public, so the audit's set of receipts means "published".
    gitlab.upload_package_file(str(version), RECEIPT_FILENAME, receipt_content)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / RECEIPT_FILENAME).write_bytes(receipt_content)
    return receipt


WITHHELD_REASON = "withheld from the public record; it names the canonical instance"


def withdrawal_body(reason: str, notes: str) -> str:
    return f"> **WITHDRAWN:** This release was withdrawn.\n\nReason: {reason}\n\n{notes}"


def release_presentation(
    gitlab: release_tools.GitLabApi,
    gate: HostGate,
    version: Version,
    notes: str,
    withdrawn: bool,
) -> tuple[str, str]:
    """The name and notes a public release must carry: the changelog, or the withdrawal notice.

    One function for the publisher, the withdrawal job, and the audit, so the three cannot drift
    apart on what a withdrawn release looks like.
    """
    if not withdrawn:
        return release_name(version), notes
    reason = public_withdrawal_reason(gate, durable_yank_reason(gitlab, version))
    return WITHDRAWN_PREFIX + release_name(version), withdrawal_body(reason, notes)


def durable_yank_reason(gitlab: release_tools.GitLabApi, yanked: Version) -> str:
    """The withdrawal reason as `release_yank` recorded it, from its immutable yank record."""
    raw = gitlab.package_file(str(yanked), YANK_RECORD_FILENAME)
    if raw is None:
        raise ReleaseError(f"release {yanked} has no yank record; release_yank must succeed first")
    record = release_tools.json_object(raw, f"yank record {yanked}")
    reason = record.get("reason")
    if record.get("yanked_version") != str(yanked) or not isinstance(reason, str) or not reason.strip():
        raise ReleaseError(f"yank record for {yanked} is unreadable")
    return reason.strip()


def withdraw_from_github(
    *,
    github: GitHubApi | None = None,
    gitlab: release_tools.GitLabApi | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Mark the public release withdrawn. The tag, commit, and assets stay as the audit record.

    Deliberately carries no qualification gate: withdrawal has to work during the outage that
    would fail a scan, and when the finding motivating it is the one that would fail it. Every
    input is durable -- the protected yank tag and the yank record `release_yank` wrote -- so a
    retry long after the tag still works.
    """
    source_env = os.environ if environ is None else environ
    env = release_tools.require_env(
        (
            "CI_COMMIT_TAG",
            "CI_COMMIT_REF_PROTECTED",
            "CI_SERVER_HOST",
            "CI_REGISTRY",
            TOKEN_VARIABLE,
        ),
        source_env,
    )
    if env["CI_COMMIT_REF_PROTECTED"] != "true":
        raise ReleaseError("public withdrawal requires a protected yank tag")
    yanked = Version.from_yank_tag(env["CI_COMMIT_TAG"])
    gate = HostGate.from_env(env)
    if gitlab is None:
        gitlab, _ = release_tools.gitlab_api_from_env(source_env)
    if github is None:
        github = GitHubApi(env[TOKEN_VARIABLE])
    notes = gitlab.package_file(str(yanked), CHANGELOG_FILENAME)
    if notes is None:
        raise ReleaseError(f"release {yanked} has no durable changelog")
    name, body = release_presentation(gitlab, gate, yanked, notes.decode("utf-8"), True)
    if f"Reason: {WITHHELD_REASON}" in body:
        print("[github] WARNING: the yank reason names a canonical host; withholding it publicly")

    tag = f"v{yanked}"
    all_releases = github.list_releases()
    record = published_release_to_withdraw(github, all_releases, tag)
    if record is None:
        print(f"[github] {tag} was never published; nothing to withdraw")
        return None
    gate.check([("withdrawn release name", name.encode()), ("withdrawn release body", body.encode())])

    if record.get("name") != name or normalized_body(record.get("body")) != body.strip():
        # `make_latest` is explicit because GitHub documents it as defaulting to true on update:
        # an unqualified rename could hand the withdrawn release the "Latest" badge.
        record = github.update_release(
            int(record["id"]), {"name": name, "body": body, "make_latest": "false"}
        )
    # Read after the rename, never before it, so whatever the update did to "latest" is seen.
    latest = github.latest_release()
    if latest is not None and latest.get("id") == record.get("id"):
        move_latest(github, all_releases, exclude=tag)
    return record


def published_release_to_withdraw(
    github: GitHubApi, releases: Sequence[Mapping[str, Any]], tag: str
) -> dict[str, Any] | None:
    """The published release for `tag`, or None when nothing of the version is public.

    A public tag with no published release is a publication that stopped partway. Returning None
    there would let the job succeed while the tag stays public with nothing marking it withdrawn --
    the "looks current forever" outcome -- so it fails loudly and names the one job that can finish
    it.
    """
    matches = releases_for_tag(releases, tag)
    if len(matches) > 1:
        raise ReleaseError(f"GitHub carries {len(matches)} releases for {tag}")
    if matches and not matches[0].get("draft"):
        return matches[0]
    if not matches and github.ref(f"tags/{tag}") is None:
        return None
    raise ReleaseError(
        f"public tag {tag} exists but its GitHub Release was never published: the publication "
        f"stopped partway. Retry github_release_publish in the {tag} pipeline -- it completes "
        "the release as withdrawn -- then retry this job"
    )


def public_withdrawal_reason(gate: HostGate, reason: str) -> str:
    """The reason as published, or a placeholder when it names a canonical host.

    The reason is the one free-text input on this path, and the yank tag that carries it cannot be
    re-cut. Failing on it would make the public withdrawal impossible, which is the one thing this
    path must never be; so a reason naming a canonical host is withheld, not published.

    The audit rebuilds the expected notice with this same rule against the hosts it runs with. If
    the canonical registry host is ever renamed, a reason withheld under the old name may no
    longer match, and the audit reports it as drift. That false positive is accepted: the check
    it enables -- the whole notice, not just its first line -- is the one that catches an edited
    reason.
    """
    if gate.roles_in(reason.encode()):
        return WITHHELD_REASON
    return reason


def move_latest(
    github: GitHubApi, releases: Iterable[Mapping[str, Any]], *, exclude: str
) -> None:
    # GitHub cannot unset "latest"; it can only move it. Move it to the highest release left.
    remaining = published_stable_versions(releases, exclude=exclude)
    if remaining:
        github.update_release(int(remaining[max(remaining)]["id"]), {"make_latest": "true"})


def load_receipts(
    gitlab: release_tools.GitLabApi,
) -> tuple[dict[Version, tuple[dict[str, Any], bytes]], set[Version]]:
    receipts: dict[Version, tuple[dict[str, Any], bytes]] = {}
    yanked: set[Version] = set()
    for value in gitlab.package_versions():
        try:
            version = Version.parse(value)
        except ReleaseError:
            continue
        if gitlab.package_file(value, YANK_RECORD_FILENAME) is not None:
            yanked.add(version)
        raw = gitlab.package_file(value, RECEIPT_FILENAME)
        if raw is None:
            continue
        receipt = release_tools.json_object(raw, f"receipt {value}")
        validate_receipt(receipt, version)
        receipts[version] = (receipt, raw)
    return receipts, yanked


def audit_github_publication(
    *,
    github: GitHubApi,
    gitlab: release_tools.GitLabApi,
    gate: HostGate,
) -> list[str]:
    """Compare the public repository with the durable receipts. Returns findings; empty is clean.

    Read-only and tokenless. Nothing here repairs anything: every plausible automatic repair of a
    public ref is a rewrite of published history. The gate is only used to rebuild a withdrawal
    notice exactly as the withdrawal job wrote it.
    """
    findings: list[str] = []
    receipts, yanked = load_receipts(gitlab)

    extra_branches = sorted(set(github.branches()) - {"main", "rc"})
    if extra_branches:
        findings.append(f"unexpected public branches: {', '.join(extra_branches)}")
    main = github.ref("heads/main")
    rc = github.ref("heads/rc")
    if main is None or rc is None:
        return findings + ["public main or rc is missing"]

    tags = github.matching_refs("tags/")
    releases = github.list_releases()
    findings.extend(unreceipted_public_state(tags, releases, receipts))
    findings.extend(audit_heads(github, receipts, main, rc))
    findings.extend(audit_chain(receipts))
    for version, (receipt, raw) in sorted(receipts.items()):
        tag = f"v{version}"
        findings.extend(audit_release_refs(github, tag, receipt["public"], tags, main))
        findings.extend(
            audit_release_record(github, gitlab, gate, version, receipt, raw, releases, yanked)
        )
    findings.extend(audit_latest(github, receipts, yanked))
    return findings


def audit_chain(receipts: Mapping[Version, tuple[dict[str, Any], bytes]]) -> list[str]:
    """Anchor the first-parent chain, not each receipt on its own.

    Each receipt only proves that GitHub matches what it recorded. Walking them in version order
    from the legacy prefix is what proves the recorded parents are themselves the previous
    releases, so a hand-made commit absorbed as a first parent cannot hide inside a receipt.
    """
    findings: list[str] = []
    previous = LEGACY_PREFIX_COMMIT
    for version, (receipt, _) in sorted(receipts.items()):
        first_parent = receipt["public"]["parents"][0]
        if first_parent != previous:
            findings.append(
                f"public release commit for v{version} has first parent {first_parent}, not "
                f"the previous release commit {previous}"
            )
        previous = receipt["public"]["commit"]
    return findings


def unreceipted_public_state(
    tags: Mapping[str, str],
    releases: Sequence[Mapping[str, Any]],
    receipts: Mapping[Version, Any],
) -> list[str]:
    """Every public tag and release has to trace back to a receipt; anything else is drift."""
    findings: list[str] = []
    for ref in sorted(tags):
        tag = ref.removeprefix("refs/tags/")
        if release_tools.STABLE_TAG.fullmatch(tag) is None:
            findings.append(f"unexpected public tag {tag}")
        elif Version.from_tag(tag) not in receipts:
            findings.append(f"public tag {tag} has no durable receipt")
    for record in releases:
        tag = str(record.get("tag_name", ""))
        if release_tools.STABLE_TAG.fullmatch(tag) is None or Version.from_tag(tag) not in receipts:
            findings.append(f"public release {tag or record.get('id')} has no durable receipt")
    return findings


def audit_heads(
    github: GitHubApi,
    receipts: Mapping[Version, tuple[dict[str, Any], bytes]],
    main: str,
    rc: str,
) -> list[str]:
    findings: list[str] = []
    if not receipts:
        if main != LEGACY_PREFIX_COMMIT:
            findings.append(f"public main is {main}, not the legacy prefix, with nothing published")
    else:
        # Publication never moves main backwards, so the highest receipted version is the last
        # one published, and main must be exactly its commit -- not merely some receipted commit.
        latest = str(receipts[max(receipts)][0]["public"]["commit"])
        if main != latest:
            findings.append(
                f"public main ({main}) is not the latest release commit ({latest})"
            )
    if not github.contains(main, rc):
        findings.append(f"public rc ({rc}) does not descend from public main ({main})")
    return findings


def audit_release_refs(
    github: GitHubApi,
    tag: str,
    public: Mapping[str, Any],
    tags: Mapping[str, str],
    main: str,
) -> list[str]:
    if tags.get(f"refs/tags/{tag}") != public["tag_object"]:
        return [f"public tag {tag} is missing or moved from {public['tag_object']}"]
    findings: list[str] = []
    if github.tag_target(public["tag_object"]) != public["commit"]:
        findings.append(f"public tag {tag} no longer points at {public['commit']}")
    tree, parents = github.commit(public["commit"])
    if tree != public["tree"] or parents != public["parents"]:
        findings.append(f"public release commit for {tag} differs from its receipt")
    if not github.contains(public["commit"], main):
        findings.append(f"public release commit for {tag} is not contained in public main")
    return findings


def audit_release_record(
    github: GitHubApi,
    gitlab: release_tools.GitLabApi,
    gate: HostGate,
    version: Version,
    receipt: Mapping[str, Any],
    raw: bytes,
    releases: Sequence[Mapping[str, Any]],
    yanked: set[Version],
) -> list[str]:
    tag = f"v{version}"
    matches = releases_for_tag(releases, tag)
    if len(matches) != 1 or matches[0].get("draft"):
        return [f"public release {tag} is missing or unpublished"]
    record = matches[0]
    # The notes are held to the durable changelog the receipt hashed, not to the public asset:
    # an edit made to both the body and the asset would otherwise agree with itself.
    notes = gitlab.package_file(str(version), CHANGELOG_FILENAME)
    changelog_hash = receipt["evidence"][CHANGELOG_FILENAME]["sha256"]
    findings = audit_release_assets(github, tag, record, raw, changelog_hash)
    if notes is None or sha256_hex(notes) != changelog_hash:
        return findings + [f"durable changelog for {tag} is missing or differs from its receipt"]
    try:
        # Rebuilt exactly as the publisher and the withdrawal job write it, so an edit anywhere
        # in a withdrawal notice -- the reason included -- is drift, not only a changed header.
        name, body = release_presentation(
            gitlab, gate, version, notes.decode("utf-8"), version in yanked
        )
    except ReleaseError as exc:
        return findings + [f"public release {tag} cannot be checked: {exc}"]
    if record.get("name") != name:
        findings.append(f"public release {tag} is named {record.get('name')!r}, not {name!r}")
    if normalized_body(record.get("body")) != normalized_body(body):
        findings.append(f"public release notes for {tag} differ from what was published")
    return findings


def audit_release_assets(
    github: GitHubApi,
    tag: str,
    record: Mapping[str, Any],
    raw: bytes,
    changelog_hash: str,
) -> list[str]:
    assets = {str(asset.get("name")): asset for asset in record.get("assets") or []}
    if set(assets) != set(PUBLISHED_ASSETS):
        return [f"public release {tag} carries assets {sorted(assets)}"]
    findings: list[str] = []
    if sha256_hex(github.download_asset(assets[RECEIPT_FILENAME])) != sha256_hex(raw):
        findings.append(f"public receipt for {tag} differs from the durable receipt")
    if sha256_hex(github.download_asset(assets[CHANGELOG_FILENAME])) != changelog_hash:
        findings.append(f"public changelog for {tag} differs from its receipt")
    return findings


def audit_latest(
    github: GitHubApi,
    receipts: Mapping[Version, Any],
    yanked: set[Version],
) -> list[str]:
    """GitHub's "Latest" badge must sit on the highest release that has not been withdrawn.

    With every release withdrawn there is nothing correct to point at, and GitHub cannot unset the
    marker, so that case is not a finding.
    """
    live = [version for version in receipts if version not in yanked]
    if not live:
        return []
    expected = f"v{max(live)}"
    latest = github.latest_release()
    marked = None if latest is None else latest.get("tag_name")
    if marked != expected:
        return [f"GitHub marks {marked or 'no release'} as latest, not {expected}"]
    return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    # Neither publishing command takes a context file: both read only durable state, so they
    # work however long after their tag they are played or retried.
    publish = subparsers.add_parser("publish", help="project a released tree onto public GitHub")
    publish.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    subparsers.add_parser("withdraw", help="mark a yanked public release withdrawn")
    subparsers.add_parser("audit", help="compare public GitHub with the durable receipts")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "publish":
            receipt = publish_to_github(artifacts_dir=args.artifacts_dir)
            print(
                f"[github] published v{receipt['release_version']} at "
                f"{receipt['public']['commit']}"
            )
        elif args.command == "withdraw":
            record = withdraw_from_github()
            if record is not None:
                print(f"[github] withdrew {record.get('tag_name')}")
        elif args.command == "audit":
            gitlab, _ = release_tools.gitlab_api_from_env()
            findings = audit_github_publication(
                github=GitHubApi(None), gitlab=gitlab, gate=HostGate.from_env(os.environ)
            )
            for finding in findings:
                print(f"[github] DRIFT: {finding}", file=sys.stderr)
            if findings:
                raise ReleaseError(f"public repository drift: {len(findings)} finding(s)")
            print("[github] public repository matches the durable receipts")
        else:  # pragma: no cover - argparse enforces the command
            raise ReleaseError(f"unknown command: {args.command}")
    except ReleaseError as exc:
        print(f"[github] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
