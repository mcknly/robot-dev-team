<!--
Robot Dev Team Project
File: docs/RELEASING.md
Description: Stable release authorization, image promotion, and recovery runbook.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Releasing Robot Dev Team

Robot Dev Team releases are authorized by annotated, protected Git tags and promote an image that
was already built and smoke-tested by the protected `main` pipeline. A release pipeline never
rebuilds the image.

The automated release track is stable SemVer only and `linux/amd64` only. Preview releases and an
`edge` channel are unsupported. `release_publish` promotes each stable release to the private
registry and copies the same digest to Docker Hub (see [Authorize and publish](#authorize-and-publish)),
and a yank reconciles both registries (see
[Yank and emergency rollback](#yank-and-emergency-rollback)). Once a release has completed, a manual
job projects the released tree onto the public GitHub repository (see
[Publish to GitHub](#publish-to-github)); it builds and qualifies nothing, and the private GitLab
pipeline remains the sole build root: the public image is the digest that pipeline built, scanned,
and qualified, never a second build from the public tree.

## Docker Hub target and credential probe

The sole public container target is `docker.io/mcknly/robot-dev-team`; GHCR is not a publication
target. `mcknly` is a personal Docker namespace for this personal, non-commercial open-source
project. Its personal access token is therefore account-scoped rather than repository-scoped. That
broader scope is an explicit interim exception, recorded with the credential, and must be revisited
if the project becomes an organizational or commercial publication or moves to a Docker
organization.

The project stores the Docker credentials as protected project-level CI/CD variables with the
environment scope `dockerhub-publication`:

- `DOCKERHUB_USERNAME` is `mcknly`. It is protected but visible because GitLab cannot mask values
  shorter than eight characters and the public username is not a secret.
- `DOCKERHUB_TOKEN` is protected, masked, and hidden. It must have read/write access but does not
  need delete access. Ownership, rotation, and revocation records stay in the private tracker,
  never in the repository.

Neither key may also exist with the default `*` (All) scope. Define both on the project, not the
`mcknly-labs` group: environment scope on group variables is not available on GitLab CE.

Job `rules:` do not limit a protected variable. GitLab passes it to every job that runs on a
protected ref, including `validate` and `compat_python_floor` on the shared `rdt-validate` runner,
so rules alone cannot keep the account-wide token away from those jobs. The environment scope does:
a job receives the credentials only when it runs on a protected ref **and** declares
`environment: dockerhub-publication`. `dockerhub_credential_probe`, `release_publish`, and
`release_yank` declare it with `action: prepare`, which applies the scoped variables without
recording a deployment. GitLab CE has no protected-environment approval step, so the set of jobs
that declare the environment is the remaining boundary. `test_only_approved_jobs_receive_scoped_credentials`
fails if any job, template, or `default:` other than the approved set declares it, and holds
`github-publication` to the same rule. A new holder must be added to that set explicitly and keep a
strict protected-ref rule. If the variable scope and a job ever disagree, the probe and
`release_publish` fail on the missing `DOCKERHUB_USERNAME` before any login. `release_yank`
completes the private withdrawal first and then fails the same way.

After provisioning or rotating the token, open a protected `main` pipeline whose
`build_smoke_publish` and `security_scan` jobs succeeded and run the manual
`dockerhub_credential_probe` job. The job needs both, so it cannot be played before the
vulnerability gate has passed for that digest. It takes the manifest digest emitted by
`build_smoke_publish` and performs a digest-addressed `crane copy` to:

```text
docker.io/mcknly/robot-dev-team:ci-credential-probe-<pipeline-id>
```

The job resolves the source before copying and refuses to run if the probe tag already exists, even
at the expected digest. Reading a tag on a public repository needs no push right, and
`crane auth login` does not contact the registry, so only a write to an absent tag proves the
current token. After the copy it resolves the destination and fails unless both registries report
the same manifest digest. Its `dockerhub-probe.json` artifact records the source, the destination
in the `docker.io/...` form above, the pipeline, the job, and the verified digest, without
credentials. This is a write-path acceptance test only; it does not publish a stable release.
Stable releases reach Docker Hub through `release_publish`.

The CI token intentionally cannot clean up the public tag. After recording the successful job URL
and digest in the private credential record, the Docker repository owner must delete the probe tag
in Docker Hub under **My Hub > Repositories > robot-dev-team > Tags**. Confirm that the exact
`ci-credential-probe-<pipeline-id>` tag is selected before deleting it. Do not delete any SemVer or
moving release alias.

The artifact is written only on success. If the job fails after the copy, for example on a digest
mismatch, the tag can still exist: find it on the `Docker Hub probe cleanup target:` line of the
job log and delete it the same way. Retrying a job whose tag already exists fails by design; delete
the tag first, then retry, or probe from a newer `main` pipeline.

Docker Hub supports beta immutable-tag rules. The repository has **Specific tags are immutable**
set with `^[0-9]+\.[0-9]+\.[0-9]+$` (configured by the repository owner on 2026-10-06), so `X.Y`,
`X`, `latest`, and the `ci-credential-probe-*` namespace stay mutable. Keep it that way: a pattern
that matched a moving alias would make every later release fail when it moves that alias. The
release code does not treat the beta setting as the contract. It checks existing state before
writing, rejects a digest conflict on `X.Y.Z`, and verifies every write by digest. A tag that
already names the release digest is never pushed again, so a retry does not depend on how the rule
treats a same-digest re-push, which Docker's documentation does not say.

### Docker Hub repository overview

The Docker Hub overview is edited by hand by the Docker Hub repository owner; no job or merge
request can write it. It is kept short and sends readers to the source. Whatever else the owner
writes, it must carry three things before the first stable public promotion: the public source
link, how to verify an image (by digest, since it is not signed), and the corresponding-source
pointer, which `docs/LICENSE_REVIEW.md` relies on the registry page to repeat. The other facts a
consumer needs live in the source the overview links to: `linux/amd64`-only support and boot-time
egress are in the README Quick Start, and the SBOM scope is in `SECURITY.md`. Paste the block
below, below any project description the owner adds, and re-paste it whenever this block changes.
Keep the canonical hostname out of it.

```markdown
**Source:** https://github.com/mcknly/robot-dev-team -- start with the README's Quick Start, and
check out the git tag `vX.Y.Z` for the image tag `X.Y.Z` you pull.

**Verify by digest; images are not signed.** There is no cosign signature, by decision. Each
GitHub Release carries `public-release-receipt.json`, which lists the image reference and digest
that release published. Check the digest you pulled against it, and pin `@sha256:...` in
production. See `docs/MIRRORING.md` ("Verifying what you pulled").

**Corresponding source.** `docs/LICENSE_REVIEW.md` ("Corresponding source for the image") says
where the source of every redistributed package is. In short: every Debian package's source is on
snapshot.debian.org by source name and version, and the image labels
`com.mcknly.robot-dev-team.debian-snapshot` and `com.mcknly.robot-dev-team.debian-source`
(`docker inspect`) name the snapshot the build installed from. Source for the MPL-2.0 components
compiled into `uv` and `glab` is on crates.io and the Go module proxy at the versions the image
ships.
```

### Boot egress check before the first public promotion

`docs/DEPENDENCY_MANAGEMENT.md` ("Boot-time egress") publishes the hosts a container needs at
start. They were observed by reading the installers and following redirects, not by booting behind
a firewall. Before the first public promotion to Docker Hub, and again whenever an installer
changes, qualify the table against that exact digest. Every step uses a **fresh** container
(`docker compose up --force-recreate`, or after `docker compose down`): a restarted container
keeps the harnesses its earlier start installed, and the Antigravity installer then skips its
download entirely, so a restart proves nothing about egress.

1. Start a fresh container with the stock routes behind a default-deny egress policy that allows
   only the hosts in the table's first three rows. It must reach `uvicorn` (`GET /health`
   answers).
2. Block those hosts and start a fresh container. The entrypoint must exit 1 with
   `harness binaries not on PATH after install` before the port opens. Allow for the connect
   timeouts: a dropping firewall makes this take minutes, not seconds.
3. Comment out one default agent's routes and start a fresh container. It must start without that
   agent's hosts.
4. Repeat step 1 for every optional harness whose row claims a working allowlist.

Record every denied destination the logs show and update the table in the same merge request. A
row whose vendor step was not traced -- today, `claude install` and `agy install` -- stays marked
unverified rather than promising a complete allowlist.

## Release contract

The in-tree declaration and the authorization event must agree:

- `[project].version` in `pyproject.toml` declares `X.Y.Z`.
- The editable root entry in `uv.lock` records the same `X.Y.Z`.
- An annotated protected Git tag named `vX.Y.Z` authorizes publication.
- `docs/CHANGELOG.md` contains `## [vX.Y.Z] - YYYY-MM-DD`.
- The tagged commit is contained in `origin/main`.
- The existing `$CI_REGISTRY_IMAGE:$CI_COMMIT_SHA` image is present in the registry.

The three in-tree items -- version, lockfile, and dated changelog section -- are checked by
`tests/test_release_contract.py` in the `validate` job. That job's only rule excludes the
scheduled drift-audit pipeline, so otherwise the workflow rules govern it and it runs on merge
request events, default-branch pushes, and `v*` tag pushes alike. Two consequences are worth
stating:

- A merge request is the *early* warning, not the only one. Because the stages run
  `validate -> image -> release`, the in-tree contract is re-verified inside the tag pipeline
  before any alias promotion or package publication happens.
- A commit that reaches `main` without a merge request is still checked, on the default-branch
  push.

The items that depend on the tag -- the annotated protected tag, its agreement with the declared
version, and `main` ancestry -- are checked only at tag time by `validate_release()`.

Only exact stable tags matching `vX.Y.Z` are accepted for publication. Leading zeroes, `-rc`,
`-beta`, build metadata, lightweight tags, unprotected tags, and version disagreements fail before
publication. The separate `vX.Y.Z-yank` form is reserved for an authorized withdrawal.

The release aliases are:

| Alias | Behavior |
| --- | --- |
| Full commit SHA | Immutable build identity created by the protected `main` pipeline |
| `X.Y.Z` | Immutable-by-policy stable release identity |
| `X.Y` | Highest non-yanked patch release in that minor line |
| `X` | Highest non-yanked release in that major line |
| `latest` | Highest non-yanked stable release overall |
| `@sha256:...` | Durable deployment identity; preferred for production |

**Container image aliases carry no `v` prefix.** The `v` belongs only to the Git tag and the
changelog heading that mirrors it; `pyproject.toml`, the image aliases, and the Generic Package
version are all bare `X.Y.Z`:

| Artifact | Form |
| --- | --- |
| `pyproject.toml` `[project].version` | `0.2.1` |
| Git tag (authorization) | `v0.2.1` |
| `docs/CHANGELOG.md` heading | `[v0.2.1] - YYYY-MM-DD` |
| Container image aliases | `0.2.1`, `0.2`, `0`, `latest` |
| Full-SHA image alias | the full 40-character commit SHA, bare -- no `sha-` prefix, no short form |
| Generic Package version (`robot-dev-team-release`) | `0.2.1` |
| Generic Package version (`robot-dev-team-sbom`) | `sha256-<64 hex>` -- keyed by image digest, not by release version |

This matters when verifying a release, because querying a `v`-prefixed image alias returns a
registry `not found` that is indistinguishable from "this release was never published". A failed
`v0.2.1` image lookup is a malformed query, not evidence about publication state -- query `0.2.1`.
The full-SHA alias has the same trap in a different namespace: `scripts/ci-build-image.sh` pushes
`$CI_REGISTRY_IMAGE:$CI_COMMIT_SHA`, so a guessed `sha-<short>` form -- common elsewhere, including
GitLab's own `Dockerfile` templates -- misses in exactly the same way.

No automation in this repository writes a `v`-prefixed image alias: the only image push in CI is
that full-SHA tag, and `release_publish` never builds, it only promotes aliases from
`Version.aliases` and `desired_moving_aliases()` in `scripts/release_tools.py`, which are bare
SemVer. A `v`-prefixed lookup therefore cannot resolve to anything this project published. It says
nothing about the registry as a whole, which the repository cannot constrain: a `vX.Y.Z` image
alias that *does* resolve was written out of band by someone with registry write access, is drift,
and must not be trusted as release evidence.

A maintenance release prepared and merged through `main` can therefore advance its `X.Y` alias
without regressing `X` or `latest`. Releases are not cut directly from maintenance branches.

Project governance must limit release authorization, publication, deletion, and emergency
recovery to designated release operators. Release jobs must run only from protected refs on
trusted runners. Registry policy must protect full-SHA build identities and release aliases, while
package policy must protect durable release records. Immutable release evidence must be retained,
and conflicting state must fail closed. Instance-specific GitLab configuration is intentionally
maintained outside this repository.

## Prepare a release

Open a release-preparation merge request targeting `main`. It must:

1. Set `pyproject.toml` to the intended stable version, then run `uv lock` so the editable root
   entry in `uv.lock` records the same version. `uv sync --frozen` in the `validate` job does not
   re-resolve the lockfile, so an un-relocked version bump is caught by the release contract test
   rather than by dependency installation.
2. Add a fresh empty `[Unreleased]` section to `docs/CHANGELOG.md`.
3. Rename the prior `[Unreleased]` content to `[vX.Y.Z] - YYYY-MM-DD` and finalize it. The dated
   section must carry the notes: it becomes the GitLab Release description and the durable
   `changelog.md` package file, and an empty one fails the release contract test. The date is
   the release-preparation date. Do not correct it after the merge if the tag slips: a date-only
   commit on `main` builds a new digest, and every pre-tag check would have to run again against
   it. Nothing checks the date against the tag, and the public release dates come from the
   tagger time.
4. Include any final code or documentation required by the release.
5. Bring `security/license-evidence.json` up to date with the latest protected `main` digest,
   using the refresh commands in `docs/LICENSE_REVIEW.md` section 7, so that `classify` exits 0
   against it. Neither `security/` nor `docs/` is copied into the image, so this change does not
   alter the components the release image will contain.

The merge request pipeline performs normal validation, enforces the in-tree half of the release
contract above, and publishes no release aliases. After review, merge the MR and wait for the
protected `main` pipeline to build, smoke-test, generate the SBOM, scan the pushed digest for
vulnerabilities, and publish the full-SHA image. Do not tag a commit whose `main` pipeline failed
-- including a `security_scan` failure, which means the tag pipeline would fail the same way with
a fresher database.

Before tagging, re-check **that** pipeline's digest against the last audit
(`docs/SANITIZATION_REPORT.md` section 11). Image builds aren't reproducible, so the digest being
released is never the one an earlier report named; these checks are what make the report true of
it. Run them from a canonical checkout, with `REGISTRY_IMAGE`, `DIGEST`, and the crane login set
up as in `docs/LICENSE_REVIEW.md` section 7, `SERVER_HOST` and `REGISTRY_HOST` set to the
canonical server and registry hosts, and `detect-secrets==1.5.0` installed. Save this as a
script and run it with `bash`, so the first failure stops it:

```bash
set -euo pipefail
audit="$(mktemp -d)"   # outside the checkout: the extraction is roughly 10,000 files
echo "audit directory: $audit"
python -m scripts.image_audit extract --crane .release-bin/crane \
  --image "$REGISTRY_IMAGE@$DIGEST" --dest "$audit/layers"
python -m scripts.image_audit hosts --root "$audit/layers" \
  --host "$SERVER_HOST" --host "$REGISTRY_HOST"
(cd "$audit/layers" && detect-secrets scan --all-files \
  --exclude-files '^(image-manifest|image-config|layers)\.json$' .) > "$audit/secrets-scan.json"
python -m scripts.image_audit compare-secrets --scan "$audit/secrets-scan.json" \
  --baseline security/image-secrets-baseline.json
```

The directory is kept on failure, because reading a hit needs the extracted file. Delete it once
the results are recorded.

The secrets scan excludes the three metadata files `extract` writes, and exactly those:

- the hostname scan already covers them;
- the image config changes on every build, so a hit in it could never be classified;
- detect-secrets records the exclusion among its filters, which the baseline binds.

Every step fails closed rather than passing on nothing:

- `extract` refuses a non-empty destination.
- `hosts` fails when it scanned no files, when a metadata file is missing or the layer record is
  malformed, and when it scanned fewer or more files than the extraction recorded.
- `compare-secrets` requires an exact match. It fails on:
  - any hit outside the baseline (`UNCLASSIFIED`);
  - any baseline hit the scan no longer reports (`LOST`), because a scan that kept one known hit
    but skipped most of the image would otherwise pass;
  - a scan that is empty;
  - a scan that shares no hit with the baseline;
  - a scan whose detect-secrets version, plugins or filters differ from the baseline's, including
    a missing or extra `--exclude-files` pattern.

`hosts` accepts a host name with a port, and an IPv6 address bare or in brackets, with or
without a port. It refuses anything it would otherwise search for as a literal that no bare
occurrence of the host contains: a URL, user info, whitespace anywhere in the value, a character
outside letters, digits, `-`, `_` and `.`, an empty label or a leading or trailing dot or hyphen,
a multi-colon value that is not an IPv6 address, and an IPv6 address with a zone id, an
IPv4-mapped or dotted one, and `::`. An IPv6 address is searched in its compressed spelling only. The error never
repeats the refused value. To scan a tree that `extract` did not produce, such as a merged
filesystem or a positive control, pass `--no-extraction`. Then only an empty scan fails, because
there is no layer record to count against.

Then run the license review against the same digest: the full procedure in
`docs/LICENSE_REVIEW.md` section 7, not a shortcut, because it is what binds the result to the
digest.

All three must exit 0. Record the results on the release-prep MR. What a failure means:

- **A hostname finding** is a publication blocker. Fix it and release a later commit.
- **An unclassified secrets hit** needs reading at its line. If it's a false positive, the audit
  baseline is refreshed deliberately in a follow-up MR. If not, it is a blocker.
- **A lost secrets hit** means the scan did not read a file the audit classified, or the file
  changed or left the image. Check that the scan ran over the whole extraction first. A lost hit
  whose file really changed is resolved by the same deliberate refresh.
- **Hits confined to a component a merged MR knowingly bumped**, lost or unclassified, are
  expected. uv's own SBOM contributes most of the baseline, so a uv bump loses its old hashes and
  adds new ones. A `TOOL_RELEASES` pin change moves the checksums in `scripts/release_tools.py`.
  A regenerated `notices/` tree moves the per-file SHA-256 values in the two
  `/usr/share/doc/{glab,uv}/notices.json` manifests, which detect-secrets reports as hex strings.
  Those hits are handled the same way: read them, refresh the baseline from the new `main` digest
  in a follow-up MR, and name the bump in that MR. They are not a trigger for the full audit
  (`docs/SANITIZATION_REPORT.md` section 12). Refresh from the kept scan with:
  `python -m scripts.image_audit baseline --scan <audit directory>/secrets-scan.json --digest
  "$DIGEST" --output security/image-secrets-baseline.json`. `baseline` records only the profile
  the runbook scan produces, because every later release is held to it: the detect-secrets
  version in `DETECT_SECRETS_VERSION`, exactly the plugins in `PLUGINS`, and filters drawn only
  from `DEFAULT_FILTERS` plus the one metadata exclusion (all in `scripts/image_audit.py`). It
  compares whole records, parameters included, so it refuses `--only-verified` and a retuned
  plugin as well as a custom filter or a word list. A detect-secrets bump that changes the default
  records makes `baseline` refuse until those constants are updated. Update them in the same
  reviewed MR as the version pin in this runbook, and name the changed records there.
- **An unresolved or needs-review license entry** means the release-prep MR did not cover a
  component change. Fix the evidence in a follow-up MR and tag the `main` commit that follows
  instead.

These are release-preparation reviews, not pipeline gates. A CI gate is deferred until a few
releases show how often the evidence actually changes (see `docs/LICENSE_REVIEW.md` section 8).

Last, prove the Docker Hub push right: play `dockerhub_credential_probe` on that same `main`
pipeline, record the job URL, and delete the probe tag (see
[Docker Hub target and credential probe](#docker-hub-target-and-credential-probe)). The release's
own preflight logs in and reads Docker Hub, but a read needs no push right and `crane auth login`
never contacts the registry. So a revoked or read-only token, or a Docker Hub outage, first shows
up at the release's first `crane copy`. By then the private aliases have already moved.

## Authorize and publish

From an up-to-date checkout, an authorized release manager creates an annotated tag on the
successful merge commit and pushes it only to the private GitLab remote:

```bash
git fetch gitlab main --tags
git tag -a v0.2.0 <merge-commit-sha> -m "Robot Dev Team v0.2.0"
git push gitlab v0.2.0
```

The tag pipeline then:

1. Runs the normal validation suite and validates the release contract.
2. Resolves the existing full-SHA image digest from the registry.
3. Fetches the SBOM staged under that exact digest and rejects the release if it is missing, is
   not an SPDX-2.3 document, or does not name the image being released. The staged scan evidence
   is verified at the same point (see [The vulnerability gate](#the-vulnerability-gate)).
4. Logs in to Docker Hub and rejects the release if `docker.io/mcknly/robot-dev-team:X.Y.Z`
   already names a different digest.
5. Uses checksum-pinned Crane to add the eligible aliases to that exact digest in the private
   registry.
6. Copies the same digest from the private registry to `X.Y.Z` and the same eligible moving aliases
   on Docker Hub (`crane copy`, never a rebuild). A tag that already names the digest is left alone.
7. Verifies every alias in both registries by resolving it back to the release digest.
8. Publishes `release-manifest.json`, `changelog.md`, `sbom.spdx.json`, and the scan evidence under
   the Generic Package `robot-dev-team-release/X.Y.Z`. The manifest's `public_aliases` lists every
   verified Docker Hub tag, and `public_references` holds the immutable `X.Y.Z` reference with its
   digest. `github_release_publish` later copies `public_references` into the public receipt.
9. Creates or updates the GitLab Release and links the durable package files.

Steps 3 and 4 precede step 5 deliberately, and the ordering is a safety property rather than a
convenience: they run in the only window where the digest is known and no alias has moved and no
package file has been written, so a missing or unusable SBOM, failing scan evidence, or a
conflicting public `X.Y.Z` aborts the release with nothing mutated. Tests assert it directly by
failing on any alias write in those cases. The Docker Hub check has to come first because the
private `X.Y.Z` is immutable too: found after the private aliases moved, a public conflict would
leave a release that no retry could finish. The check reads; it cannot prove the token may push,
which is why the credential probe runs before tagging.

The manifest is written only after every public tag has verified, so a receipt can never list a
reference that was not checked. Docker Hub gets exactly the moving aliases the private registry
gets, computed by the same non-regression rule from the private release record. A maintenance
release therefore takes its own `X.Y` on Docker Hub and leaves `X` and `latest` with the newer
line.

The GitLab Release is written through the project Releases API with `CI_JOB_TOKEN` -- the same
client that writes the durable package files -- so the release image needs no `git` executable and
no CLI to identify the project: it is `CI_PROJECT_ID`, never discovered from the checkout. No `ref`
is sent with the release, so the API can attach a release to the protected tag but can never create
a tag of its own.

The release job is serialized and idempotent. A retry accepts an existing `X.Y.Z` alias, in either
registry, or package file only when its content matches, and a retry after a partial Docker Hub
copy writes only the tags still missing. The durable manifest is write-once, so a retry must agree
with it on the public references too. Release asset links are reconciled the same way: a link whose
name and URL already match is left alone, a missing link is added, and a name or URL that is
already used for something else fails closed. Conflicting immutable state fails closed.

The reconciliation reads the existing release before any alias or package file is written, and it
is strict about what it reads, because a release record that is misread as empty would re-send
links that already exist and fail the job after those writes. A release the job may not read
answers `404` exactly like a tag with no release, so a `404` is confirmed against the releases
collection before it counts as "not published"; a release whose `assets.links` payload cannot be
read as GitLab documents it raises instead of being treated as having no links.

After success, confirm that all of the following name the same commit and manifest digest, minding
the prefix difference above:

- The GitLab Release (on Git tag `vX.Y.Z`).
- The Generic Package manifest (`robot-dev-team-release/X.Y.Z`).
- The full-SHA image alias -- the full 40-character commit SHA, not a `sha-` short form.
- The version image alias `X.Y.Z` -- bare, not `vX.Y.Z`.
- The digest-pinned image.
- The Docker Hub tag `docker.io/mcknly/robot-dev-team:X.Y.Z`, and the manifest's
  `public_references`. Without `--platform`, `crane copy` pushes the source manifest unchanged, so
  this is the same digest as the private one, not merely an equivalent image.

Check the public side from a machine with no Docker Hub credentials, so the check also proves the
repository is public:

```bash
crane digest docker.io/mcknly/robot-dev-team:X.Y.Z
docker pull docker.io/mcknly/robot-dev-team@sha256:<digest>
```

Deployments should record and use the `image_reference` value from `release-manifest.json`. Keep
the host-specific compose override that pins it in `docker-compose.release.yml`, which is
git-ignored: it names a registry and a deployment-specific digest that should not reach the
repository. The README Quick Start's public path is different and uses the auto-merged
`docker-compose.override.yml` (`image: docker.io/mcknly/robot-dev-team:X.Y.Z`,
`platform: linux/amd64`, and `build: !reset null`). A host that keeps both should set `image:` in
only one of them: once `-f` or `COMPOSE_FILE` names files, Compose loads the override only if it is
listed, and the last listed file's `image:` wins.

## Publish to GitHub

The tag pipeline ends blocked on the manual `github_release_publish` job. Nothing is public until
someone plays it; it is not `allow_failure`, so an unplayed projection leaves the pipeline visibly
unfinished rather than green. Before playing it, confirm the canonical release above, and that
public `rc` holds only pull requests that were already qualified here -- the job includes `rc` as
the release commit's second parent, which makes those commits permanent public ancestry.

The job pushes one release commit, fast-forwarding public `main` and `rc` onto it, plus the
annotated `vX.Y.Z` tag, in a single atomic push; then creates the GitHub Release as a draft,
uploads `changelog.md` and `public-release-receipt.json`, reads both back, and publishes. The
receipt is also stored as `robot-dev-team-release/X.Y.Z/public-release-receipt.json`, which is
what the scheduled audit compares the public repository against. `docs/MIRRORING.md` is the
contract; `docs/CI.md` lists each step.

After success, from any machine with no credentials:

```bash
git ls-remote https://github.com/mcknly/robot-dev-team.git main rc "refs/tags/vX.Y.Z^{}"
curl -fsSL https://github.com/mcknly/robot-dev-team/releases/download/vX.Y.Z/public-release-receipt.json
```

`main`, `rc`, and the peeled tag should all be the receipt's `public.commit`, and
`public.tree` should equal `git rev-parse vX.Y.Z^{tree}` in a canonical checkout.

Failure handling:

- **Retry** the job in the same pipeline for anything transient, however long after the tag: it
  consumes no job artifact, so nothing it needs expires. It converges: existing matching refs, a
  draft release, or already-uploaded assets are reused, and an interrupted upload on a draft is
  replaced.
- **`outbound host gate`** names the file or object that carries a canonical host. Nothing was
  published. The tagged tree cannot change, so fix it on `main` and release a new version; this
  version stays canonical-only.
- **`the changelog section for X.Y.Z cites the private tracker`** lists the issue or
  merge-request shorthand, pipeline or job numbers, or tracker paths in that version's notes.
  Nothing was published. Every tag cut before `test_no_tracked_file_cites_the_private_tracker`
  existed fails here by design; do not play the job on those. Like the host gate, it is fixed on
  `main` and in a new release; this version stays canonical-only. The exception is `v0.3.0`, which
  was projected before this check existed. It is already public with those citations, and a retry
  on its tag now stops here without changing anything public.
- **A rejected atomic push** usually means a pull request was merged into `rc` while the job ran.
  Retry; the job re-reads `rc`.
- **Anything reported as conflicting** -- a public tag or commit that is not byte-identical to what
  this release would create, a published release with different notes or bytes or a missing
  asset, a diverged `rc`, an unexpected asset -- is drift. Do not delete or force-push anything
  public, and do not re-attach a missing asset by hand. Establish what wrote it, per
  `docs/MIRRORING.md` section 7.
- **`public main is ..., not ...`** has two causes, and the message says which.
  - *"it is the vA.B.C release commit ... Retry github_release_publish in the vA.B.C pipeline
    first"*: an earlier publication pushed its refs and stopped before writing its receipt. Retry
    that version's job until it succeeds, then retry this one.
  - *"something wrote to public main outside publication"*: the job refuses to build on it,
    because the commit would become permanent ancestry of every later release, and the scheduled
    audit reports the same commit. Establish what wrote it before going further; there is no
    in-tree way to publish over it.
- **A version older than one already public** is refused, because publishing it would move public
  `main` backwards. It stays canonical-only.
- **Retry until it succeeds before yanking.** If the job fails after its push, finish it before
  cutting a `-yank` tag. If the yank came first anyway, `github_release_withdraw` fails and says
  so: retry `github_release_publish` in the original tag pipeline, which completes the release as
  withdrawn and writes its receipt, then retry the withdrawal job.

A yanked version is never published. If the yank came after publication, the automatic
`github_release_withdraw` job marks the public release withdrawn (see below).

## Retrieve the SBOM for a release

Every stable release carries the SPDX SBOM of the exact image it publishes, addressable two ways.
Both are the same bytes: the document Syft produced from an archive of the smoke-tested image,
uploaded once by the protected `main` pipeline and copied -- never regenerated -- by the release.

| Addressed by | Location |
| --- | --- |
| Release version | `robot-dev-team-release/X.Y.Z/sbom.spdx.json` |
| Image digest | `robot-dev-team-sbom/sha256-<64 hex>/sbom.spdx.json` |

The GitLab Release for `vX.Y.Z` links the version-scoped copy as `sbom.spdx.json`, alongside
`release-manifest.json` and `changelog.md`. The digest-scoped copy is keyed by the manifest digest
with `:` replaced by `-`, because GitLab rejects the colon form as a package version. Take the
digest from `image_digest` in `release-manifest.json`:

```bash
glab api "projects/:id/packages/generic/robot-dev-team-release/0.2.2/sbom.spdx.json" \
  > /tmp/v0.2.2-sbom.spdx.json

DIGEST="$(glab api \
  "projects/:id/packages/generic/robot-dev-team-release/0.2.2/release-manifest.json" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["image_digest"])')"
glab api "projects/:id/packages/generic/robot-dev-team-sbom/${DIGEST/:/-}/sbom.spdx.json" \
  > /tmp/sbom-by-digest.spdx.json
```

The digest-scoped package accrues one version per protected `main` commit and is never pruned;
generic packages have no expiry policy. That is a deliberate cost, and it is what makes an SBOM
retrievable for a digest that was built but never released.

**Scope.** The SBOM describes the built image: the Debian packages from the pinned snapshot,
CPython, the application virtualenv, `glab`, `uv`/`uvx`, the installed license notices, and the
application. The harness CLIs that `docker-entrypoint.sh` installs at boot are **outside it**, and
outside the vulnerability gate too, which scans the same digest. They are received from their
vendors under the vendors' terms, which is also why `docs/LICENSE_REVIEW.md` excludes them. The
reason they are installed at boot is recorded in `docs/SYSTEM_DESIGN.md` section 7.

Both ends of this path check that the document's top-level `name` is the image reference that was
scanned, `<registry image>:<commit sha>`, which `scripts/generate-sbom.sh` sets with
`--source-name`. The package path alone binds nothing -- a manual upload can write any bytes under
any digest -- so the name check is what makes "the SBOM for this digest" mean the SBOM of that
image. Keep `--source-name` on any hand-run rescan.

Releases published before this workflow existed -- `v0.2.0` and `v0.2.1` -- carry no SBOM package
file and no SBOM release link. Their images are unchanged; only the durable record is absent.
Yanking one of them does not invent a link for a file that is not there.

### Inventory a running container

To inventory a **running** container, wait for the entrypoint to finish (`GET /health` answers),
then scan the container's filesystem with the same digest-pinned Syft `scripts/generate-sbom.sh`
uses -- not a `syft` on your `$PATH`. The result is not a release artifact, so there is no
`--source-name` to check.

This does **not** inventory the harness CLIs. Their vendor binaries carry no package metadata
Syft reads, so a scan of a container with every harness installed adds only Pi's Node runtime.
Harnesses track latest and are installed at boot, so no inventory of them is published.

Export the filesystem; never `docker commit` the container. A commit records the container's
configuration, including every variable Compose injected from `.env` -- the agent GitLab tokens
and `GITLAB_WEBHOOK_SECRET` -- and `docker image save` then writes that configuration into the
tarball. `docker export` carries the filesystem only, does not pause the container, and leaves out
the contents of volumes and bind mounts, so the host credential directories are not in it.

```bash
(
  set -euo pipefail
  scratch="$(mktemp -d)"   # mode 0700: private to you
  trap 'chmod -R u+w "$scratch"; rm -rf "$scratch"' EXIT   # deleted however the scan ends
  mkdir "$scratch/rootfs"
  # Harnesses land in ~/.local, which is container-local, so the export includes them.
  docker export robot-dev-team | tar -x -C "$scratch/rootfs"
  docker run --rm -v "$scratch/rootfs:/rootfs:ro" \
    anchore/syft:v1.42.2@sha256:15952b4306fd990724afaaf7f1c71fcd03546b89fbf6f2d32b0be5f81e3ef431 \
    dir:/rootfs --base-path /rootfs --output spdx-json > rdt-running.spdx.json
)
```

**The export still holds secrets**: treat the scratch directory as a credential store, which is
why the recipe keeps it private and deletes it on exit. Leaving out bind mounts does not leave out
everything sensitive. The entrypoint writes a `glab-token` under `~/.<agent>/` for every agent
token in `.env`, and only the directories you mount are left out. Each dispatch also leaves the
agent's GitLab token in the container-local `~/.config/glab-cli/` (the `glab` configuration, and
the fallback git credential store used outside a repository). The SBOM itself lists packages and
file paths, not file contents.

This is a different inventory from the release SBOM, not a more complete copy of it: it varies
from one start to the next, and nothing about it is reproducible or bound to a release.

## The vulnerability gate

`security_scan_release` re-scans the digest being released with a current database and stages the
report and the evaluation under `robot-dev-team-scan/sha256-<hex>/`. `release_publish` needs that
job and only reads its output: the evaluation must bind to the digest Crane just resolved, come
from the pinned Grype, record a passing database freshness check, and carry a passing verdict.
Anything else fails the release closed in the window before the first alias write, with nothing
mutated. Policy details, the blocking rule, and the exception contract are in `docs/CI.md`.

Two operational consequences worth knowing before you tag:

- **A release now depends on Anchore's vulnerability database being reachable.** The 48-hour
  freshness requirement fails closed, so a database outage blocks publication. This is deliberate
  -- a scan against an unknown-age database is not a passing scan -- but it means "the release is
  blocked" can mean "a third-party service is down" rather than "the image is unsafe". Wait for
  the service and retry the tag pipeline. Do not work around it by editing the policy.
- **Withdrawal is never gated.** `release_yank` installs Crane alone, never reads scan evidence,
  and never invokes the scanner. That is on purpose: a bad release most needs pulling during
  exactly the outage that would fail a scan, and worse, the CVE motivating a yank could be the
  finding that fails it.

A retry of a successful scan job is a no-op: it reads the staged evidence and returns rather than
re-scanning, because the evaluation embeds a timestamp and a re-derived document would conflict
with the durable-file content check.

### Recovering a half-staged scan

The report is uploaded before the evaluation, so the partial state a failure between the two can
leave is **report staged, evaluation missing**. A retry handles that on its own: it re-uses the
staged report verbatim -- re-scanning would produce different bytes and be rejected -- and derives
only the evaluation.

That recovery has a deadline. The re-evaluation re-checks the database freshness recorded in the
staged report, so if the retry happens more than 48 hours after the original scan it fails, and it
cannot be fixed by re-scanning: the report path is already occupied by different bytes. Delete the
staged report and let the job scan again:

```bash
glab api --method DELETE \
  "projects/:id/packages/<package-id>/package_files/<file-id>"
```

Find the ids with `glab api "projects/:id/packages?package_name=robot-dev-team-scan"` and then
`glab api "projects/:id/packages/<package-id>/package_files"`. Deleting scan evidence is safe in a
way that deleting a release manifest is not -- it is regenerated by the next run, and the release
cannot proceed without it. Re-run the tag pipeline afterwards.

The opposite state -- an evaluation with no report -- is unreachable through the normal upload
order, so if the job reports it, something was staged by hand. It stops rather than re-scanning,
because a new report would strand the existing evaluation against a document it never evaluated.

### Reused evidence and its age

The staged evidence is keyed by image digest, so a second release of the same digest reuses it
rather than re-scanning. That is the intended binding -- the release is a claim about a digest --
but it does mean the evidence's age is the age of the **first** scan of that digest, and
`release_publish` does not re-check it against the clock.

This is an accepted exception to the "re-scan at tag time" argument rather than an oversight. A
hard age cap at publish time would have no escape hatch: the scan job cannot refresh evidence it
has already staged, so a capped release would deadlock until an operator deleted package files.
In normal use each release tags a distinct commit, which is a distinct digest, and gets a fresh
scan. If you are deliberately re-releasing a digest that was scanned long ago, delete its staged
evidence with the runbook above so the tag pipeline scans it again.

One thing is **not** left to age, though: every exception the evidence relied on is re-checked
for expiry at the moment it is consumed, on both the release path and the scan job's own retry.
Reusable evidence would otherwise let a later release pass on an acceptance that lapsed in
between, which is the one part of the policy that is explicitly time-bound. If a release fails
with "relies on exception ... which expired on ...", the fix is to renew or remove the entry on
`main` and let the digest be re-scanned -- not to edit the staged evidence.

## Failure before or during publication

- Before the tag is created, fix the problem in another MR and tag the new successful `main`
  commit.
- If `security_scan_release` fails on a blocking finding, the fix is a dependency bump or a
  reviewed exception on `main`, then a new higher version. Do not move the tag, and do not add an
  exception in order to release a specific tag -- an exception needs an owner, a rationale, and an
  expiry, and @cavin owns that decision.
- If release validation fails, do not move or recreate the tag. Fix the release-preparation
  contents in another MR and use a new higher version.
- If publication stops partway through for a transient or environmental reason, retry
  `release_publish` in the same protected tag pipeline. It verifies correct existing state and
  completes missing work.
- A retry cannot repair a defect that is baked into the tagged commit. The retried job checks out
  `.gitlab-ci.yml` and `scripts/release_tools.py` from the tag, so fixing the defect on `main`
  changes nothing about what that job runs, and the tag must not be moved or recreated to reach
  the fix. Publish the correction as a new higher patch release instead. The partially published
  version keeps its aliases, package files, and evidence; a later release repoints the moving
  aliases normally. If the earlier version is left without a GitLab Release record, backfill it
  out of band from its durable package files and say so in the release notes, because that record
  will not carry pipeline provenance. From a host that has `glab` and an authenticated session
  (the release image does not, by design):

  ```bash
  GITLAB_HOST="<your-gitlab-host>"
  PROJECT="https://${GITLAB_HOST}/mcknly-labs/robot-dev-team"
  PACKAGES="https://${GITLAB_HOST}/api/v4/projects/<id>/packages/generic/robot-dev-team-release"
  glab api "projects/:id/packages/generic/robot-dev-team-release/0.2.0/changelog.md" \
    > /tmp/v0.2.0-changelog.md
  glab release create v0.2.0 --repo "$PROJECT" \
    --name "Robot Dev Team v0.2.0" \
    --notes-file /tmp/v0.2.0-changelog.md \
    --assets-links "[
      {\"name\":\"release-manifest.json\",\"url\":\"$PACKAGES/0.2.0/release-manifest.json\"},
      {\"name\":\"changelog.md\",\"url\":\"$PACKAGES/0.2.0/changelog.md\"}
    ]"
  ```

  Add a `sbom.spdx.json` link to that list only if the version actually has that package file.
  GitLab does not check that an asset link URL resolves, so linking a file that is not there
  produces a permanent 404 on the release page.

- If `release_publish` stops with `no durable SBOM is published for sha256:...`, the tagged
  commit's `main` pipeline ran before `sbom_publish` existed, or that job never succeeded. The
  release fails closed before any alias moves, and CI cannot repair it on its own: the build job
  refuses to re-run against an image it has already pushed, and `sbom_publish` needs artifacts
  that expire after 30 days. Stage the file by hand, then retry `release_publish`. Nothing records
  which run produced the SBOM, so a manual upload completes the release exactly like a pipeline
  one -- and a first upload is unconstrained, since the content check only forbids a *conflicting*
  second one.

  Prefer the retained build artifact of the pipeline that published the digest:

  ```bash
  DIGEST="sha256:<64 hex>"                      # from `crane digest`, or the failed job log
  glab api "projects/:id/jobs/<job-id>/artifacts/artifacts/sbom.spdx.json" > /tmp/sbom.spdx.json
  glab api --method PUT \
    "projects/:id/packages/generic/robot-dev-team-sbom/${DIGEST/:/-}/sbom.spdx.json" \
    --input /tmp/sbom.spdx.json
  ```

  If the artifact has expired, rescan the digest with the same digest-pinned scanner container
  `scripts/generate-sbom.sh` uses -- not a `syft` on your `$PATH`, whose version you do not
  control -- and note in the release that the SBOM was regenerated from the registry copy rather
  than the locally smoke-tested image. `--source-name` is required, not cosmetic: the release job
  rejects a document that does not name the image it is releasing. Substitute your real registry
  image path for the `IMAGE` placeholder below -- it is not decoration in this one command. The
  resulting document name must equal `${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHA}` exactly, and a
  document named after the placeholder is rejected on the `release_publish` retry -- by
  `release_sbom_for_digest`, after the rescan and the manual upload have both already run. The
  upload itself will not catch it: `sbom_publish` validates the name when a pipeline stages the
  document, and this recovery path does not run that job at all.

  ```bash
  IMAGE="<your-gitlab-host>/mcknly-labs/robot-dev-team"
  SHA="<the commit sha whose main pipeline built ${DIGEST}>"
  docker run --rm \
    anchore/syft:v1.42.2@sha256:15952b4306fd990724afaaf7f1c71fcd03546b89fbf6f2d32b0be5f81e3ef431 \
    "registry:${IMAGE}@${DIGEST}" \
    --source-name "${IMAGE}:${SHA}" \
    --output spdx-json > /tmp/sbom.spdx.json
  ```

  The rescan reads the registry, so the scanner container needs credentials for a private
  registry -- authenticate it the way your environment does, or scan a locally pulled copy with
  `docker-archive:` as `generate-sbom.sh` does.

- If a link name or URL is already used for something else, the job fails closed and will keep
  failing on retry: it will not repoint or replace an existing link. Inspect the release's links
  (`glab api "projects/:id/releases/vX.Y.Z"`), and either delete the conflicting link
  (`glab api --method DELETE "projects/:id/releases/vX.Y.Z/assets/links/<link-id>"`) once you have
  established what wrote it, or leave the release alone and publish the correction as a higher
  patch version. Do not delete a link that points at a durable package file of that same release.
- If `X.Y.Z` already points to another digest or a durable package file conflicts, stop and
  investigate. Never overwrite or delete the evidence.
- If `release_publish` stops with `public release alias docker.io/mcknly/robot-dev-team:X.Y.Z
  already points to ...`, Docker Hub already has that version at another digest, and that attempt
  mutated nothing. Read the rest of the message, because there are two cases:
  - *"this attempt mutated nothing"* alone: the private `X.Y.Z` does not name the release digest,
    so no attempt of this release has moved anything. Nothing in this repository writes a SemVer
    tag to Docker Hub outside this job, so the tag was written by hand or by a release of the same
    version from a different commit. Establish which before going further.
  - *"an earlier attempt of this release moved the private aliases"*: an earlier attempt copied
    `X.Y.Z`, failed to verify it (next bullet), and left the private aliases moved with no
    manifest. Read that attempt's log. Until a manifest exists, the release cannot be yanked
    either.

  In both cases the immutable-tag rule means only the Docker Hub repository owner could remove the
  tag, and that would replace a public version people may already have pulled. Publish the
  correction as a higher patch version instead.
- If it stops with `alias verification failed for docker.io/...`, the public tag resolved to
  another digest after the copy. Without `--platform`, `crane copy` pushes the source manifest
  unchanged, so that is unexplained registry state. The manifest is not written, so the receipt
  cannot record the bad tag. Inspect it with `crane manifest docker.io/mcknly/robot-dev-team:<alias>`
  first. What a retry does depends on the alias. For `X.Y.Z`, the retry stops at the preflight
  with the earlier-attempt message above and writes nothing. For a moving alias (`X.Y`, `X`,
  `latest`), the retry copies it again, and if the registry now stores the right bytes the release
  completes.
- If it stops inside `crane copy` to `docker.io/...` (`DENIED`, `UNAUTHORIZED`, `TOOMANYREQUESTS`,
  or a timeout), the push itself failed. The private aliases have already moved, but no manifest,
  package file, or GitLab Release was written, so the release cannot be yanked yet. Restore the
  token's write access, wait out the rate limit or outage, then retry `release_publish` in the
  same pipeline. It skips every tag that is already in place and writes only the missing public
  tags. Running the credential probe before tagging (see
  [Prepare a release](#prepare-a-release)) is what keeps this case rare.
- If it stops with `DOCKERHUB_USERNAME is required` or `DOCKERHUB_TOKEN is required`, the job no
  longer receives the environment-scoped variables. Nothing was mutated. Restore the job's
  `environment: dockerhub-publication` or the variables' scope (see
  [Docker Hub target and credential probe](#docker-hub-target-and-credential-probe)) and retry.

## Yank and emergency rollback

Normal recovery is a higher patch release. Preserve the bad Git tag, `X.Y.Z` image alias, digest,
manifest, and release record for auditability.

When moving aliases must be removed from a bad release, create an annotated protected withdrawal
tag on a current `main` commit. The annotation is the durable withdrawal reason:

```bash
git fetch gitlab main --tags
git tag -a v0.2.0-yank gitlab/main -m "Withdraw v0.2.0: startup regression"
git push gitlab v0.2.0-yank
```

The protected tag is the authorization boundary; pipeline variables cannot select the release,
replacement, reason, or operator. The job derives the bad version and reason from the tag, obtains
the actual job user from GitLab, and resolves all three of the version's moving aliases (`X.Y`, `X`,
and `latest`) from the registry. It does not rely on the manifest's list, because a release also
inherits aliases after publication, above all when an earlier yank fell back to it. Only an alias
that names the yanked digest is acted on, and an alias already moved by a newer release is left
untouched. Each one goes to a fallback chosen by digest, not only by version:

- **The newest compatible release with a different image.** Two releases can share a digest, and
  retagging to one of them would record a rollback while still serving the yanked bytes.
- **Kept, if the newest compatible release is newer than the yanked one and publishes the same
  image.** A yank withdraws a version, not an image, and that tag belongs to a release that is
  still current. If the image itself is the problem, yank every version that publishes it.
- **None otherwise.** The job refuses before making changes, because the private registry holds
  every earlier release, so no compatible release with a different image exists. Publish a
  corrected higher patch release first.

A release that was promoted to Docker Hub has its Docker Hub aliases reconciled next, by the rules
in [Docker Hub during a yank](#docker-hub-during-a-yank). Every change in both registries is
verified before the job writes an immutable `yank-record.json` and marks the GitLab Release as
withdrawn. The record lists, per registry, what each alias now tracks (`alias_targets`), which
stayed with a newer release of the same image (`aliases_kept`), and which were left alone or
removed.

`github_release_withdraw` then runs automatically. It reads the reason from the immutable
`yank-record.json` rather than from an artifact. If the version was published to GitHub, it renames
that release `[WITHDRAWN] Robot Dev Team vX.Y.Z`, prepends the reason, and moves GitHub's "latest"
marker to the highest release left; the public tag, commit, and assets stay as the record of what
was published. It is a no-op for a version that was never published, and it fails -- pointing back
at `github_release_publish` -- when the public tag exists without a published release, since that is
a publication that stopped partway (see "Publish to GitHub"). The reason is the one free-text input
on this path, so it is held to the outbound host gate differently from everything else: a reason
that names the canonical instance is **withheld** from the public notice rather than failing the
job, because the yank tag cannot be re-cut and a failure would leave the public release looking
current. A reason that cites the private tracker -- an issue or merge-request shorthand, a note
anchor, or a pipeline or job number -- is withheld the same way, with its own placeholder, because
on GitHub that shorthand points at an unrelated issue or at nothing. Write the reason by role and in
full, and it is published as written. The changelog section the notice appends gets the same
treatment. If it cites the tracker, the notice carries a placeholder instead, and the `changelog.md`
asset stays as published. Publication already refuses such notes, so this only happens to a release
published before that check existed. `v0.3.0` is one, and withdrawing it completes normally.

If no compatible durable release exists, disable deployment and publish a corrected higher patch
release before withdrawing the bad release; do not make a SemVer alias point outside its line.

Deprecation alone does not move or delete image tags. Record it in the changelog and GitLab Release
notes and name the recommended replacement.

### Docker Hub during a yank

When the yanked release was promoted (its manifest carries `public_references`), the job then
resolves `X.Y`, `X`, and `latest` on Docker Hub. It applies the same digest-aware fallback rule with
one restriction: **a Docker Hub alias falls back only to a release that was itself published to
Docker Hub**, one whose manifest carries a valid `public_references`. Releases from before promotion
have a private image only, and were never held to the public-surface checks (the license notices,
the private-tracker cleanup, and the hostname scan of image layers), so a yank never copies one to
Docker Hub. Repointing is a `crane tag` within the Docker Hub repository, and every alias is
verified by digest. Only an alias that still names the yanked digest is written, and the bad
`X.Y.Z` stays, as the immutable-tag rule requires anyway.

`public_aliases` is still validated, and it must agree with `public_references` about whether the
release was promoted. It no longer decides which tags are checked, though; the live lookup does.
It only says what a missing tag means:
- A missing tag the release took, with a public fallback, is recreated at that fallback.
- One with no fallback counts as removed (see below).
- One that belongs to a newer live release of the same image stays missing and is recorded as
  skipped. The yank is not authorized to republish a tag for a release it is not withdrawing.
- A missing tag the release never took is not this yank's business, stays missing, and is not
  recorded.

Two orderings are deliberate:

- **The private side goes first, and Docker Hub never holds it back.** Every Docker Hub step,
  including the credential check, runs after the private aliases are reconciled. A Docker Hub
  outage, a missing credential, or an unreadable `public_aliases` leaves the private withdrawal
  done and the job failed.
- **The yank record is written only once both registries agree.** A failure on the Docker Hub side
  leaves no `yank-record.json`, so `github_release_withdraw` does not run, and the public release
  is never marked withdrawn while Docker Hub still serves it. Retry `release_yank` once the cause is
  fixed. It finds the private aliases already moved and completes.

**When Docker Hub has no fallback but the private registry does, you delete the alias.** The
private side runs first and refuses before any write when an alias has no compatible release with
a different image there. So the Docker Hub deletion path is reached only when the private registry
*has* such a release and Docker Hub does not. Every release since promotion is published to both,
so in practice that happens in only two situations:

- **The only compatible release with a different image is from before promotion** (`v0.3.x` and
  earlier), so it exists privately but was never published. This is the promotion boundary. For
  example, if the first public release is a patch on a line that began privately, yanking it
  repoints the private aliases to the earlier private-only patch and sends the Docker Hub tags to
  you for deletion. It stops happening once each line has a public patch behind it.
- **The private alias already names something else, while the Docker Hub alias still names the
  yanked digest.** Realistically this is a newer `release_publish` that moved the private aliases
  and then failed on Docker Hub, leaving no manifest. Retry that publication until it succeeds
  before yanking: it moves the Docker Hub aliases itself.

Every other case with no fallback, including yanking the first release of a new `X.Y` line or the
only release that ships a given image, ends in the **private** refusal (`cannot yank while aliases
lack a compatible non-yanked release with a different image`) before Docker Hub is touched.
Deleting Docker Hub tags does not let that job finish. Fix forward instead, as described at the end
of this subsection and of [Yank and emergency rollback](#yank-and-emergency-rollback).

The CI token cannot delete tags, by design: it is account-wide, and destructive registry access
stays with the repository owner. So in the deletion case the job repoints every alias it can, then
fails, listing the exact tags:

```text
Docker Hub aliases still name the yanked digest and no compatible public release with a different
image exists to repoint them to. The CI token cannot delete tags: delete these in Docker Hub, then
retry release_yank: docker.io/mcknly/robot-dev-team:0.4, docker.io/mcknly/robot-dev-team:0, ...
```

Delete exactly those tags under **My Hub > Repositories > robot-dev-team > Tags**. They are moving
tags, so the immutable-tag rule does not cover them. Never delete a SemVer `X.Y.Z` tag. Then retry
the job. An alias that is gone counts as removed: the retry verifies the rest, records the removed
aliases in `public_aliases_removed`, and notes each one in the withdrawn release's notes. With
`latest` deleted, `docker pull mcknly/robot-dev-team` reports the image as not found. That is
correct while no good public release exists, and better than serving the bad one.

Fixing forward avoids both the deletion and the refusal. A corrected higher patch release, published
before the yank, moves the moving aliases in both registries through normal promotion, so the yank
finds nothing that still names the bad digest.
