<!--
Robot Dev Team Project
File: docs/CI.md
Description: GitLab CI validation, image build, and protected release workflow.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# GitLab CI

The pipeline validates every merge request. After a reviewed change is merged, the protected
default-branch push validates again, builds and smoke-tests a `linux/amd64` image, and publishes a
content-addressed image. It does not create a public release. An optional manual job can copy one
qualified digest to a disposable Docker Hub tag to prove the protected credential path; stable
public promotion remains disabled until #8. Protected stable Git tags promote the tested digest
into private release aliases through the separate workflow documented in `docs/RELEASING.md`, and
a manual job then projects the released tree onto the public GitHub repository (see
[Public GitHub projection](#public-github-projection)).

## Pipeline flow

`workflow: rules` creates pipelines for merge-request events, default-branch pushes, `v*` tag
pushes, and scheduled default-branch pipelines. This avoids duplicate branch pipelines for a source
branch with an open merge request while allowing malformed release tags to enter validation and
fail visibly. A scheduled pipeline runs the public drift audit and nothing else: `validate` is the
one job with no admitting condition of its own, and its single rule excludes schedules, because a
failed scheduled pipeline is the drift alert and a flaky test must not look like one.

The `validate` job uses the same slim Python 3.14 base as the runtime image, installs the minimal
`git` package the test suite and the header guard need, and creates a frozen `uv.lock` environment.
The binding dependency is `tests/test_wrappers.py`, which drives a real `git` in several places
with no skip for its absence -- `scripts/header_guard.py` only needs `git ls-files` and is a
directory walk away from not needing it at all, so rewriting the header guard would not remove
the package. It runs:

```text
ruff check --no-cache --no-fix .
mypy app scripts
python scripts/header_guard.py
pytest tests
```

That `git` is **not** installed from the `Dockerfile`'s `DEBIAN_SNAPSHOT`, and the scope of the
snapshot-enforcement guarantee in `SECURITY.md` stops at the shipped image (#65). The image is
the only thing that carries a digest, an SBOM, and digest-keyed scan evidence for a
reproducibility claim to bind to; `validate`, `compat_python_floor`, `release_contract`, and
`github_release_publish` are the only four jobs that touch APT, and none of them contributes a
package or a layer to that image, so a `git` build that differs between two runs of the same
commit cannot change what is shipped.

State that boundary precisely, because the obvious broader version of it is false.
`release_contract` *does* declare a release artifact -- `artifacts/release-context.json`, which
`release_publish` and `release_yank` consume -- and on the yank path the annotated tag message
that `scripts/release_tools.py` reads through `git for-each-ref` is uploaded to the Generic
Package Registry as durable release metadata. What `git` does in that job is validate the tag
and read its annotation; it supplies no package contents, and the snapshot guarantee is a
statement about package contents. `github_release_publish` uses `git` to write the public release
commit and tag objects over a tree that is already fixed -- the canonical tagged tree, byte for
byte -- so it too supplies no package contents. `validate` and `compat_python_floor` produce no
release artifact at all. Anyone who later finds `release-context.json` on the release path should
find it already accounted for here rather than read it as evidence that the scoping decision was
made on a wrong premise.

Bringing the four under the snapshot was considered and rejected: it would put a second home
for `DEBIAN_SNAPSHOT` in `.gitlab-ci.yml`, and point every merge request at
`snapshot.debian.org`, which is materially less available than the CDN-backed
mirrors -- on the release path that trades away availability for a property the release does
not need. A bare `git=<version>` pin with no snapshot behind it is worse than either: Debian
prunes superseded binaries, so the pin stops resolving at the next point release. What the
jobs do instead is log `git --version`, because `tests/test_wrappers.py` tests version-sensitive
git behaviour and the job log is then the only record of which build ran.

CI supplies non-secret placeholder values for model variables referenced by the shipped route
configuration. Tests do not contact model providers. The explicit `tests` path prevents pytest
from collecting repositories under the ignored local `projects/` mount.

`compat_python_floor` runs in the same stage on the oldest supported interpreter -- the
`requires-python` floor in `pyproject.toml`, currently 3.12 -- with a frozen sync and `pytest`
only. It is the one job that deliberately does **not** mirror the runtime image, and
`tests/test_release_contract.py::test_runtime_python_image_pins_agree` excludes exactly that job
from the pin equality set while requiring every other Python reference to match the `Dockerfile`.
Lint and type checking are not repeated there: neither reads the interpreter it runs on, and
`mypy.ini` already targets the floor. Raising the floor means moving `requires-python`, this
lane's `image:`, `mypy.ini`, `README.md`, and `docs/AGENT_ONBOARDING.md` together; the lane's
*patch* level moves on its own, with 3.12 security maintenance (see
`docs/DEPENDENCY_MANAGEMENT.md`).

It does **not** have the same standing as `validate`, and its `rules:` say so: merge requests
and default-branch pushes only, never a `v*` tag. Nothing `needs:` it, and the release jobs are
`needs:`-driven, so on a tag it could not have stopped `release_publish` or `release_yank` --
those start as soon as their own dependencies finish, whatever else in the `validate` stage is
failing. What it could have done is turn a tag pipeline red *after* the release had already
published, and spend a full `pytest` run on `rdt-validate` in the middle of a yank. The floor is
a property of the code, so the merge request is where it is checked; a tag re-runs `validate`
and `release_contract`, which are real gates.

Only a protected default-branch push creates `build_smoke_publish`; merge-request pipelines end
after validation. The image job uses Buildx against a disposable rootless Docker-in-Docker daemon.
It builds one `linux/amd64` image and loads it into that daemon. The same local image is then used
for both smoke cases:

1. A credentialed CI-only route using the image's existing `python3` binary must pass preflight
   and return `{"status":"ok"}` from `/health`.
2. The same route without credentials must exit non-zero during preflight, before Uvicorn starts.

The fixture is copied into stopped containers with `docker cp`. A bind mount is deliberately not
used because bind paths in Docker-in-Docker resolve inside the service daemon, not the job
container. The negative case polls container state for at most 30 seconds, so a preflight hang
fails with its container logs instead of blocking the job indefinitely.

After smoke testing, the job exports that completed local image and scans the archive with the
digest-pinned Syft v1.42.2 container. Keeping Syft outside the runtime build ensures the generated
SPDX document describes every final image layer without adding scanner files to the image. The job
then refuses to publish if the full commit-SHA tag already exists, pushes the exact smoke-tested
image, and records the registry manifest digest.

`sbom_publish` follows on the same rule, consuming that job's `sbom.spdx.json` artifact and its
`IMAGE_DIGEST` dotenv variable, and uploads the SBOM to the Generic Package Registry under
`robot-dev-team-sbom/sha256-<hex>/sbom.spdx.json` with `CI_JOB_TOKEN`. That is what makes the SBOM
outlive the 30-day artifact and lets a later tag pipeline -- which never runs the image job -- link
the SBOM of the exact digest it releases (`docs/RELEASING.md`).

It is a separate job rather than a step in `scripts/ci-build-image.sh` for two reasons. The build
job runs an Alpine `docker` image with no Python, and an upload that failed after the push would
be unrecoverable: re-running the build job trips its own guard against the SHA tag it has already
created. As a separate job it retries on its own. It uploads the artifact bytes verbatim and never
re-derives them, so a retry is a no-op against the content check; a regenerated SPDX document
carries a fresh `documentNamespace` and timestamp and would be rejected as conflicting content.
It validates `IMAGE_PUBLISHED=true` and a well-formed digest rather than trusting its rule, because
the unpublished path emits no `IMAGE_DIGEST` at all. It also checks that the document names the
image this pipeline built -- `scripts/generate-sbom.sh` passes `--source-name`, so the SPDX `name`
is `${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHA}` -- which is what binds an SBOM's content to the digest
it is filed under. The release job repeats the check on the copy it fetches.

It runs on the protected `rdt` runner, not `rdt-validate`, even though it needs neither Docker nor
the DinD service. It is a durable write to the package registry with `CI_JOB_TOKEN`, and the object
it writes is the project's supply-chain evidence; `rdt-validate` also executes untrusted
merge-request containers. Every job that writes durable release state stays on the protected-ref
runner. It needs no `resource_group`: `build_smoke_publish` has already completed and released
`image-publication` before this job starts.

The digest-addressed package gains one version per protected default-branch commit, including
commits that are never released, and GitLab has no expiry policy for generic packages. This is an
accepted cost: it is what allows an SBOM to be retrieved for any published digest.

## Runner requirements

Two project-locked runners separate untrusted validation from protected image work:

- `rdt-validate` uses tag `rdt-validate`. It is available to unprotected merge-request refs, runs
  only ordinary unprivileged containers, has `services_privileged = false`, and exposes no host
  Docker socket.
- The image runner uses tag `rdt`. It is restricted to protected refs and provides the
  rootless-DinD service needed by the post-merge image job. It also runs the jobs that write
  durable release state without needing Docker at all (`sbom_publish`, the GitHub projection
  jobs, and the scheduled drift audit), because that state -- and the protected-ref tokens those
  jobs hold -- must not be on a host that also executes untrusted merge-request containers. It
  needs HTTPS egress to `github.com`, `api.github.com`, and `uploads.github.com` for the
  projection.

Both runners require a `linux/amd64` Docker executor, project locking, and untagged jobs disabled.
The image runner additionally requires these capabilities and controls:

- Runner manager major/minor aligned with the GitLab server.
- Ordinary jobs unprivileged: `privileged = false`.
- Job volumes limited to `[/cache]`; `/var/run/docker.sock` must not be exposed to jobs.
- `services_privileged = true`, restricted to the exact rootless DinD service used by the
  pipeline. For Docker 27.5.1, allow only the normalized and short exact names:

```toml
allowed_privileged_services = [
  "docker.io/library/docker:27.5.1-dind-rootless",
  "docker:27.5.1-dind-rootless",
]
```

Do not add wildcard, major-only, or `latest` entries. The pipeline checks that the host socket is
absent and that `docker info` reports the disposable daemon as rootless before it builds anything.

The runner manager still needs the host Docker socket so the Docker executor can create job and
service containers. Repository-controlled jobs cannot access that mount. Rootless DinD limits a
job to its disposable inner daemon, but the service still shares the host kernel; kernel/runtime
vulnerabilities and resource exhaustion remain residual risks. Move the runner to a dedicated or
single-use VM if contributor trust or project scope expands.

## GitLab project controls

Configure these controls before merging the CI change:

1. Protect `main`, permit changes through reviewed merge requests, and disable ordinary direct
   pushes.
2. Keep `rdt-validate` project-locked and available to unprotected refs. Keep the `rdt` image
   runner project-locked and protected-only. Disable untagged jobs on both.
3. Keep **Allow merge request pipelines to access protected variables and runners** disabled.
4. Do not add custom registry credentials other than the Docker Hub exception below. GitLab's
   per-job `CI_REGISTRY_USER` and `CI_REGISTRY_PASSWORD` are used only by the
   protected-default-branch publication path. `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` must be
   protected project-level (not group-level) variables with environment scope
   `dockerhub-publication` and no `*`-scoped copy, so only jobs declaring that environment receive
   them. GitLab CE has no protected-environment approval step; see `docs/RELEASING.md`.
   `GITHUB_MIRROR_TOKEN` follows the same rule with environment scope `github-publication`.
5. After the first pipeline succeeds, enable **Pipelines must succeed** for merge requests.
6. Create a pipeline schedule on `main` (daily is enough) for the public drift audit, owned by
   the maintainer who should receive its failure notification. It needs no variables.

The publication script additionally checks `CI_COMMIT_REF_PROTECTED=true`; a default-branch
pipeline fails closed instead of pushing if branch protection is missing.

Release configuration must restrict stable-tag creation, release publication, artifact deletion,
and emergency recovery to authorized operators. Release jobs must use protected refs and trusted
runners. Full-SHA build tags, stable and moving aliases, and durable release packages must be
protected against writes from ordinary project pipelines, and immutable build and release evidence
must be retained. Exact access levels and instance-specific controls are managed outside this
repository.

## Docker Hub credential probe

`dockerhub_credential_probe` is an optional manual job on protected default-branch push pipelines.
It does not appear in merge-request or tag pipelines, runs on the protected `rdt` runner, and needs
the `build_smoke_publish` artifacts and a successful `security_scan` from the same pipeline, so it
cannot be played before the vulnerability gate passes for the digest. It declares
`environment: dockerhub-publication` (`action: prepare`), the scope that delivers the Docker Hub
variables to it and to no other job. The job is allowed to fail so an
unplayed or failed provisioning check cannot block ordinary `main` qualification; acceptance
requires its individual result to be successful.

The job installs the checksum-pinned Crane release and calls `probe-dockerhub`. That command:

1. Revalidates that the job belongs to a protected default-branch push and that this pipeline
   published the source image.
2. Authenticates to the private registry with the per-job GitLab credentials and to Docker Hub with
   the environment-scoped `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` variables. Passwords are passed
   on standard input and never appear in arguments or artifacts.
3. Resolves `$CI_REGISTRY_IMAGE@$IMAGE_DIGEST`, copies it to the non-SemVer tag
   `docker.io/mcknly/robot-dev-team:ci-credential-probe-$CI_PIPELINE_ID`, and resolves the public
   tag again.
4. Fails if the probe tag already exists at any digest, because only a real write proves the
   token, and on any source/destination manifest-digest mismatch.
5. Retains `artifacts/dockerhub-probe.json` for 30 days as the non-secret acceptance record.

The token needs read/write but not delete access. Cleanup is deliberately an owner action in the
Docker Hub Tags UI, which keeps destructive registry permission out of CI. Full operating and
cleanup instructions, the accepted personal-PAT scope exception, and related issue boundaries are
in `docs/RELEASING.md`.

## Image identity and retention

The pushed tag is:

```text
$CI_REGISTRY_IMAGE:$CI_COMMIT_SHA
```

Tags can be mutable in GitLab CE, so the script rejects an existing SHA tag instead of replacing
it. The durable deployment identity is the manifest digest:

```text
$CI_REGISTRY_IMAGE@sha256:<manifest-digest>
```

Successful image jobs retain these artifacts for 30 days:

- `image-reference.txt`: exact digest reference for the published image.
- `image.env`: dotenv metadata including SHA tag, digest, and publication status.
- `build-metadata.json`: Buildx build metadata.
- `sbom.spdx.json`: the SBOM generated from an archive of the exact smoke-tested image. The
  retained artifact is a convenience copy; `sbom_publish` stages the same bytes in the Generic
  Package Registry, keyed by image digest, where they do not expire.

For the initial private test track, published full-SHA images are retained until the release and
cleanup policy is explicitly changed. Do not enable a cleanup rule that can remove a digest in an
active deployment or durable release record. Record the digest reference with the deployment so
loss or tag movement is visible.

## Vulnerability gate

The `security` stage sits between `image` and `release` and runs checksum-pinned Grype against an
immutable digest -- never a rebuild and never a tag. Grype scans a private registry reference
directly with no Docker daemon, so both jobs run the same slim Python image as the release jobs.
Registry credentials are passed through the environment under the `GRYPE_REGISTRY_AUTH_*` names
Grype actually binds, never as command-line arguments. Before scanning, the job loads the resolved
configuration and fails closed unless it contains the expected authority and non-empty credentials;
the captured document is never logged.

`security_scan` runs on protected `main` against the digest `build_smoke_publish` just pushed. It
is a fast fail: the report and the evaluation are 30-day artifacts and nothing durable is written.
`security_scan_release` runs on a protected `vX.Y.Z` tag, re-scans the same digest with a current
database, and stages both documents under `robot-dev-team-scan/sha256-<hex>/`. The re-scan is
deliberate -- vulnerability knowledge is time-dependent, so a result that passed when the image was
built is not a claim about the day it is released.

**Blocking rule.** A High or Critical finding blocks when a fix is available. Unfixed and wont-fix
High/Critical are recorded in the durable evaluation and do not block; tightening that is tracked
with a date in #60. This is not `--only-fixed` on the scanner: the report keeps every match and
only the blocking decision reads fix state, so the evidence survives the release that accepted it.
Medium and below are visible and never block.

The scanner's database is required to be valid and no more than 48 hours old, hash-validated on
start, with a successful update check. That check is read from the report being evaluated rather
than from a separate `grype db status` run, because that copy cannot be swapped for another run's
metadata. A stale or unreachable database fails closed, which means a release depends on Anchore's
database service being reachable -- see the runbook in `docs/RELEASING.md` for what to do during an
outage. The **yank** path deliberately keeps no such dependency: it installs Crane alone and never
reads scan evidence.

Exceptions live in `security/vulnerability-exceptions.yaml`, are scoped to an exact package,
version, and type, and require an owner, a rationale, an expiry, and a tracking issue. Expired
entries, entries matching no finding, entries naming a version that is no longer installed, and
entries shadowed by an earlier one all fail the job -- the failure mode an exception file has to
defend against is entries outliving the findings that justified them with nothing ever failing.
The summary warns for three weeks before an entry expires, so the first notice is not a red
pipeline on every branch and tag at once.

**Checking an exception edit before it merges.** The gate cannot run on a merge request: no image
is built, so there is no digest to scan. Since the hygiene rules are hard failures, an MR that
moves a pinned version -- a `PYTHON_IMAGE` bump, say -- can pass every check, merge, and only then
break `main` with "names a version that is no longer installed" on every entry at once. Run the
policy offline against a report you already have, such as the artifact from the last
`security_scan`:

```bash
python scripts/release_tools.py evaluate --report artifacts/vulnerability-report.json
```

It needs no scanner, no network, and no credentials. Two checks are deliberately not enforced in
this mode, because both describe the evidence rather than the policy: the digest binding (a report
produced locally from a `docker-archive` has no registry reference) and the 48-hour database age
(the artifacts this workflow points at are kept for 30 days, so they are routinely older than
that). Both are still reported, and the document it writes records `policy_only: true` with an
unpassed age check, which the release-side validator rejects outright -- so a policy-only result
cannot be staged and read as a passing gate. Only `publish-scan` produces release evidence.

The evaluation records the image digest and commit, the SHA256 of the report it was computed from,
scanner version and archive checksum, policy version and hash, database schema, build time, source
and age, counts by severity and fix state, every exception applied with the ids it absorbed, the
blocking findings, and whether the image's distribution is end-of-life. That last one is recorded
and printed rather than swallowed, because under-reporting is the one failure mode a CVE gate
cannot detect on its own.

Several of those fields are checked rather than merely recorded when the evidence is consumed,
because the two documents are staged and published separately and the report is the one a reader
opens. Before any alias moves, `release_publish` requires the evaluation's `report_sha256` to match
the staged report, the staged report's own `manifestDigest` and scanned reference to name the
digest being promoted, the recorded grype archive checksum to match the pin, `policy.sha256` to
equal a fingerprint recomputed from the **tag's own** `security/vulnerability-exceptions.yaml`
bytes, and every applied exception to be unexpired as of that moment. The policy check hashes the
exception file rather than parsing it, which is what keeps it available on a release path that may
not import a third-party module.

The hash and the target check are separate on purpose: the hash binds the evaluation to *a*
report, and the target check binds that report to *this* image. The expiry re-check exists because
evidence is keyed by digest and deliberately reusable, so a later release of the same digest would
otherwise pass on an acceptance that has since lapsed. All of it runs through one validator, so
the scan job's own retry path gets the same checks rather than a subset.

## Protected release promotion

An annotated protected `vX.Y.Z` tag creates a tag pipeline. `release_contract` requires exact
agreement between the Git tag, `[project].version`, and the dated changelog section; checks that the
tagged commit is contained in `origin/main`; and emits a validated context artifact.

After normal validation succeeds, `release_publish` resolves the existing full-SHA image directly
from the registry. It uses checksum-pinned Crane to add `X.Y.Z` and eligible `X.Y`, `X`, and
`latest` aliases without rebuilding. Every write is resolved and compared with the source digest.
Between resolving the digest and the first alias write -- while nothing has been mutated -- it
fetches the SBOM staged under that digest and fails the release closed if it is missing or is not
an SPDX-2.3 document, then verifies the staged vulnerability evaluation for the same digest: it
must bind to that digest from inside the document, come from the pinned scanner, record a passing
database freshness check, and carry a passing verdict. The job never runs the scanner itself -- a
Grype report embeds a scan timestamp, so a re-derived document would break the durable-file content
check on an ordinary retry, with the aliases already moved. The job then publishes a durable
release manifest, changelog, and the exact SBOM and scan bytes to the Generic Package Registry and
creates the GitLab Release through the project Releases API with `CI_JOB_TOKEN`. Crane is the only
binary `release_publish` and `release_yank` download; the release record needs no CLI and no `git`.

The release resource group prevents concurrent alias updates. Existing identical state is an
idempotent retry; an immutable alias or durable package conflict fails closed. An annotated
protected `vX.Y.Z-yank` tag authorizes `release_yank`, which records a withdrawal and recomputes
each affected moving alias from compatible non-yanked releases. Full operator and recovery
instructions are in `docs/RELEASING.md`.

## Public GitHub projection

Three jobs in the final `publish` stage carry the contract in `docs/MIRRORING.md`. All run the
runtime Python image on the protected `rdt` runner and are implemented in
`scripts/github_publication.py`, invoked as `python -m scripts.github_publication`.

`github_release_publish` is a **manual** job on protected stable-tag pipelines. It needs a
successful `release_publish` for ordering only, and shares its `release-publication` resource
group. It consumes **no job artifact**: every input is the protected tag or the durable
version-scoped package, because a manual job may be played or retried after 30-day artifacts
expire, and a blocked pipeline does not keep them as the latest successful ones either. It is not
`allow_failure`, so the tag pipeline stays blocked, visibly unfinished, until the projection has
run. The job:

1. Refuses a yanked version or an unprotected tag, and requires the durable manifest (which binds
   the version, tag, and commit, and exists only if the release contract passed), changelog,
   SBOM, scan report, and scan evaluation. The durable changelog must be the tagged tree's own
   changelog section, since it becomes the release body.
2. Runs the outbound host gate over every path and blob of the tagged tree, against both
   `CI_SERVER_HOST` and the host component of `CI_REGISTRY`, before anything is fetched.
3. Copies only the tagged tree's objects into a scratch repository, so no canonical commit can be
   reached from anything it pushes, and fetches public `main`, `rc`, and tags.
4. Requires public `main` to be exactly the previous receipted release commit -- the highest
   receipted version below this one, withdrawn ones included, or the legacy prefix for the first
   release. Anything else was written outside the job, and building on it would make it the new
   release's first parent: permanent, receipted, and invisible to the audit. It then builds one
   release commit whose tree is the tagged tree, with that commit as first parent and `rc` as
   second parent when `rc` carries accepted work, and an annotated tag on it. Author,
   committer, and tagger are the fixed noreply identity; every date is the canonical tag's tagger
   time, so a retry reproduces the objects. It refuses to publish a version older than one already
   public, since that would move `main` backwards.
5. Runs the host gate over the commit and tag objects, the release body, and every asset, then
   pushes `main`, `rc`, and the tag in **one atomic, unforced push**. An `rc` merged in the window
   rejects the whole push instead of being reset over.
6. Creates the GitHub Release as a draft, uploads the changelog and the publication receipt, reads
   each asset back and compares its SHA-256, and only then publishes it.
7. Stores the receipt in the release's Generic Package version and as a job artifact.

A retry converges: a matching tag, release, or asset is success, an interrupted upload on a draft
is replaced, and an `rc` that moved on from the release commit right after the push is accepted.
An existing public tag is adopted only when the commit and tag object ids are exactly the ones the
job would create -- the commit rebuilt from its recorded parents -- and its first parent is the
previous receipted release, so a same-tree tag with any other author, date, message, tagger, or
ancestry is rejected rather than receipted.

A yanked version is never published, with one exception: if its refs already landed before the
yank (the job stopped after the push), a retry finishes that release under its withdrawn name and
notes, never as latest, and writes the receipt. The tag cannot be deleted, and without a receipt
it would stay unexplained drift and leave every later release with no anchor to build on. A conflicting tag, a
published release with different notes or bytes, a published release missing an asset (it was
visible without its evidence, so it is not quietly repaired), an unexpected asset, or a diverged
`rc` fails closed. Git authenticates through an askpass helper that
reads `GITHUB_MIRROR_TOKEN` from its environment, with global and system config disabled, so the
token never appears in an argument, a remote URL, or a credential store.

`github_release_withdraw` runs automatically on a protected `vX.Y.Z-yank` pipeline after
`release_yank`. It reads the reason from the immutable `yank-record.json`, not from an artifact,
renames the public release with a `[WITHDRAWN]` prefix, prepends the reason to its notes, and
moves GitHub's "latest" marker to the highest remaining release. The rename sends
`make_latest: "false"` explicitly, because GitHub documents that field as defaulting to true on
update, and "latest" is read only after the rename. The tag, commit,
and assets stay as the record of what was published. Like `release_yank`, it installs nothing and
reads no scan evidence. It is a no-op when nothing of the version is public, and it **fails** when
the public tag exists without a published release -- a publication that stopped partway -- naming
`github_release_publish` in the tag pipeline as the job that finishes it. Succeeding there would
leave the tag public with nothing marking it withdrawn. A yank reason that names a canonical host
is withheld from the public notice rather than failing the job.

`github_publication_audit` runs alone in scheduled default-branch pipelines. It declares no
environment, so it never holds the GitHub token; every read is anonymous. It fails when public
`main` is not the highest receipted release commit (or, before the first release, not the legacy
prefix), when the receipts' first parents do not chain from the legacy prefix through each
previous release, when `rc` does not descend from `main`, when a public tag, branch, or release
has no durable receipt, when a release commit or tag differs from its receipt, when a release's
name or notes differ from what was published -- for a withdrawn release, the whole notice is
rebuilt from the durable yank reason, so an edited reason is drift too -- when GitHub marks
anything but the highest non-withdrawn release as latest, or when a public asset's bytes differ
from the receipt. It repairs nothing. Rebuilding a withdrawal notice applies the same
host-withholding rule as the withdrawal job, from the predefined `CI_SERVER_HOST` and
`CI_REGISTRY`; if the registry host is ever renamed, a reason withheld under the old name can
report as drift.

Anonymous GitHub API reads are limited to 60 per hour per egress address. The audit makes about
seven calls plus three per published release (asset downloads use the public download URL and do
not count), so the limit is far off, but it is shared with anything else behind the same address.
A rate-limited run fails like drift; re-run it before investigating. If the limit is ever reached
in practice, give the audit a separate **read-only** token -- never through `github-publication`,
which would hand the auditor the token that writes the repository it audits.

`test_only_approved_jobs_receive_scoped_credentials` pins the two jobs allowed to declare
`github-publication`, and fails on any environment name it does not already know.

## Pull and smoke-deploy an exact image

For a private project, create a project deploy token with only `read_registry`. Do not use a
personal token as a shared deployment credential.

```bash
REGISTRY_HOST="<your-gitlab-host>"   # the canonical instance's container registry
docker login "$REGISTRY_HOST"
IMAGE_REFERENCE="$(cat image-reference.txt)"
docker pull "$IMAGE_REFERENCE"
```

From a checkout of the matching commit, run the same hermetic startup probe on a clean machine:

```bash
docker run --rm --name rdt-sha-smoke -p 8080:8080 \
  -e ROUTE_CONFIG_PATH=/work/config/routes-ci-smoke.yaml \
  -e CI_SMOKE_AGENT_GITLAB_TOKEN=ci-smoke-token \
  -e CI_SMOKE_AGENT_GIT_EMAIL=ci-smoke@example.invalid \
  -v "$PWD/tests/fixtures/ci/routes.yaml:/work/config/routes-ci-smoke.yaml:ro" \
  "$IMAGE_REFERENCE"
```

In another terminal, verify `curl --fail http://127.0.0.1:8080/health`. Stop the container with
Ctrl-C. This fixture proves startup without downloading an agent harness; production deployment
must use the operator's real route configuration and credentials described in `docs/ENVIRONMENT.md`.

## Troubleshooting

- A pending validation job usually means the unprotected runner is offline or lacks the
  `rdt-validate` tag. A pending image job points to the protected runner or its `rdt` tag.
- `service is not allowed to run in privileged mode` means the runner's exact privileged-service
  allowlist does not match `docker:27.5.1-dind-rootless`.
- A host-socket assertion failure means `/var/run/docker.sock` leaked into the job volumes; stop
  using the runner until its `config.toml` is corrected.
- A default-branch publication refusal means `main` is not protected in GitLab.
- `immutable SHA tag already exists` means that commit was already published. Use the original
  pipeline's digest artifact; do not overwrite the tag.
- A release contract failure means the protected tag, project version, changelog, annotation, or
  default-branch ancestry disagrees. Preserve the tag and prepare a new higher release version.
- An immutable release alias or durable package conflict indicates prior publication with
  different content. Do not overwrite it; compare the registry digest and Generic Package record.
- Registry authentication or pull failures should be checked against
  `https://<your-gitlab-host>/v2/` and the `read_registry` scope of the deploy token.
- `outbound host gate` from `github_release_publish` names the file or object that carries a
  canonical host. Nothing was published. Fix it in the canonical tree and release a new version;
  a retry cannot pass.
- A rejected atomic push from `github_release_publish` usually means `rc` moved while the job ran.
  Retry the job; it re-reads `rc` and includes the new work.
- A failed `github_publication_audit` is a drift alert. Do not force-push anything; follow the
  recovery steps in `docs/MIRRORING.md` section 7.
