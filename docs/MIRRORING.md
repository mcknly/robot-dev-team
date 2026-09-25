<!--
Robot Dev Team Project
File: docs/MIRRORING.md
Description: Canonical-source policy and public GitHub mirror contract.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Canonical Source and Public Mirror Policy

This document is the authoritative statement of what the public GitHub repository is, what it is
not, and what may write to it. It is published in the tree rather than kept as an internal note
so that a stranger who pulls the container image can read the same policy the maintainers work
to.

Short version: **development happens on a private self-hosted GitLab instance. Public `main`,
stable `vX.Y.Z` tags, and release artifacts on GitHub are a one-way publication of that work;
GitHub Issues, pull requests, and the `rc` branch are public intake.** Nothing flows from GitHub
back to the canonical instance automatically, ever.

The distinction matters, because the public repository is not uniformly read-only and saying so
would be wrong in exactly the place contributors are invited to write. What is read-only is the
**release line**: public `main`, the stable tags, and the release artifacts are written by release
automation and by nothing else. What is writable is the **intake**: Issues, pull requests, and the
`rc` branch a maintainer merges accepted pull requests into. There is no transformed public release
tree and no second public release branch -- `rc` is contributor intake that feeds the next release
commit, not a parallel release path.

## 1. Authority model

| Function | Where it happens | Public? |
| --- | --- | --- |
| Code review and merge to `main` | Private self-hosted GitLab | No |
| CI: lint, types, tests, header guard | Private self-hosted GitLab | No |
| Container image build and smoke test | Private self-hosted GitLab | No |
| SBOM generation, vulnerability scan, release gate | Private self-hosted GitLab | No |
| Release tagging and authorization | Private self-hosted GitLab | No |
| Readable source, license, security policy | `github.com/mcknly/robot-dev-team` | Yes |
| Container image distribution | `docker.io/mcknly/robot-dev-team` | Yes, from the first public release |

There is exactly one qualification pipeline and exactly one release authority, and neither of them
is on GitHub. GitHub Actions are not used to build, qualify, tag, or publish anything, and carry no
release authority. A second pipeline on the public side would be a second source of truth about
whether a build is fit to release, which is the specific outcome this policy exists to prevent.

That prohibition is about **authority**, not about every possible use of the word "test". If a
secretless, advisory contributor-side check is ever added, it carries none of that authority: it is
a convenience signal on a pull request, it is never a statement that a change is fit to release,
and the qualification that decides that still runs only on the canonical instance. No such check
exists today, and [`CONTRIBUTING.md`](CONTRIBUTING.md) tells contributors to expect none.

The private instance is deliberately not named by hostname **anywhere in the published tree** --
not in this document, not in the operator runbooks, and not in the wrapper scripts copied into the
image. It is not reachable from the internet, so a URL serves no reader, and a literal hands a
grep-driven scraper a resolvable endpoint, a registry, and an API base already assembled.
`tests/test_public_surface.py` enforces zero occurrences across every tracked file.

Be precise about what that buys, because over-claiming it would make this document wrong in the way
it exists to prevent. The GitHub organisation is `mcknly`, the image is `mcknly/robot-dev-team`, and
the canonical namespace path appears throughout these runbooks -- the hostname is a short guess from
any of them. This is **not** secrecy and nothing may be built on the assumption that it is. What it
buys is removal from the automated target lists that work by grep, which is the threat model that
actually applies.

## 2. What is published, and when

A public push happens only when a protected stable `vX.Y.Z` tag is created on the canonical
instance and its release has been authorized. There is no continuous mirror, no nightly sync, and
no per-merge push. Between releases the public repository is simply out of date, and that is the
intended behaviour.

Each release publishes:

- one commit on public `main` whose **tree is byte-identical to the tagged canonical tree**;
- an annotated `vX.Y.Z` tag on that commit;
- a GitHub Release whose body is the changelog section for that version;
- a **publication receipt** binding the canonical tag, commit, and tree to the public commit, tree,
  and tag, and recording the public image references and digests for that release plus the SHA-256
  of every canonical evidence file.

The release commit and tag are authored, committed, and tagged by `MCKNLY LLC` at the GitHub
noreply address of the `mcknly` account, and dated from the canonical tag, so the same release
always produces the same public objects. The commit message records the canonical commit in a
`Canonical-Commit:` trailer. The publication job stores the receipt on the canonical instance as
well, and that stored copy is what the drift audit in section 7 compares the public repository
against.

The tree is published as-is. Nothing in it is stripped, rewritten, or filtered on the way out: the
publish filter is "the tagged tree as-is" on purpose, because that is what makes *the published tree
is byte-identical to the tagged canonical tree* a verifiable statement rather than a promise.
Anything that would be unpublishable is fixed in the canonical tree instead, where the fix is
reviewed like any other change.

### The release evidence bytes are not published yet

That as-is rule governs the **tree**. It cannot govern the release evidence, and this is the one
place where two things this policy wants are in genuine conflict.

The canonical release manifest, SBOM, vulnerability report, and vulnerability evaluation each carry
the private hostname **by construction**, not by oversight:

| Asset | Where the host is | Why it cannot simply be removed |
| --- | --- | --- |
| `release-manifest.json` | `source_image`, `image_reference`, `pipeline_url`, `job_url` | The manifest's job is to name the artifact the canonical pipeline authorized. |
| `sbom.spdx.json` | SPDX `name` | Required to equal `${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHA}`; `sbom_publish` rejects any other name. |
| Grype report | scanned `userInput`, `repoDigests` | The scan is bound to the digest from inside the document; a tag reference is rejected as evidence. |
| Scan evaluation | `image_repository` | Binds the verdict to the repository the digest was scanned in. |

So "publish the same bytes" and "the hostname appears nowhere public" cannot both hold. Publishing
those four assets unchanged would republish the host on every release, which would defeat the
section 1 rule no matter how thoroughly the tracked tree is scrubbed.

The resolution for the first iteration is to publish the changelog and the receipt, and to **defer
the four evidence assets** until sanitized public variants are designed and tested. The receipt
records each asset's SHA-256, so the canonical bytes stay pinned by this release even while they
are unpublished, and the gap is a missing artifact rather than an unverifiable claim.

Be precise about what that hash does and does not buy, because the obvious reading of it is wrong.
A canonical SHA-256 verifies **only the canonical bytes**. It is the check that a byte-identical
later publication is the document this release authorized -- and a byte-identical publication is
exactly the one the section 1 rule forbids. It can never validate a sanitized variant: a
field-aware transformation produces a different document with a different hash, so a reader who
recomputes the canonical hash against a sanitized file gets a mismatch, which is the correct
result and not a verification. A sanitized variant therefore needs **its own** published hash,
plus provenance metadata naming the canonical hash it was derived from and recording the
equivalence check that was run against it. The canonical hash pins what is missing; it does not
certify a replacement for it.

If sanitized variants are built later, two constraints apply. They must be produced by field-aware
transformation -- never a blind string replacement over the document bytes -- and they must be
validated to preserve digest, package, and finding equivalence with the canonical document. And
once transformed they must stop being described as "the same bytes", here and everywhere else,
because they will not be.

### The outbound host gate

The publication job fails closed, before any public write, if a canonical host occurs anywhere in
the complete outbound surface: the projected tree, the commit and tag objects, the author,
committer, and tagger identities on them, the release body, the receipt, and the bytes of every
published asset.

"A canonical host" is plural on purpose. The gate checks `CI_SERVER_HOST` **and** the host
component of `CI_REGISTRY`, because GitLab permits the container registry on a separate host or
subdomain. That is not a hypothetical split here: the four deferred evidence assets carry the
*registry* host rather than the server host, since `CI_REGISTRY_IMAGE` is what reaches
`source_image`, the SPDX `name`, the scanned `userInput`, and `image_repository`. A gate spelled
against `CI_SERVER_HOST` alone would pass every one of those documents on a split-host deployment.

The identities are called out because they are the path with no undo. Public `main` is append-only
and never rewritten, so a release commit stamped with an `@<canonical-host>` author or committer
address puts the hostname into the permanent public record one commit per release, and nothing short
of the history rewrite this policy forbids removes it. Neutral author, committer, and tagger
identities are pinned by the job, and checking the author alone is not sufficient.

`github_release_publish` implements this gate. It checks every path and blob of the tagged tree
before it fetches anything, and the commit and tag objects, release body, receipt, and every
asset before its first public write. The withdrawal job holds the notice it writes to the same
gate, with one deliberate difference: a yank reason naming a canonical host is withheld from the
notice instead of failing the job, because withdrawal must never become impossible. The four deferred evidence assets pass through it only as SHA-256 values in the receipt,
which is why their embedded host never trips it.

### Verifying what you pulled

What you can check today is on the GitHub Release for that version, and none of it requires access
to the canonical instance:

1. **Get the digest you are actually running**, not the tag you asked for:
   `docker inspect --format '{{index .RepoDigests 0}}' docker.io/mcknly/robot-dev-team:X.Y.Z`.
   A tag is a moving pointer; the digest is the thing.
2. **Match it against the release.** The receipt published with `vX.Y.Z` records the public image
   references and digests that release authorized. If your digest is not in it, you are not running
   that release.
3. **Tie the source to the image.** The public commit tagged `vX.Y.Z` has a tree byte-identical to
   the canonical tagged tree, and the same receipt binds the canonical tag, commit, and tree to the
   public ones.

**What you cannot check yet, stated plainly:** all four evidence assets -- the release manifest,
the SBOM, the vulnerability report, and the evaluation that authorized publication -- are **not
published**, for the reason given above: each embeds a canonical host by construction. The receipt
records the SHA-256 of each, so the canonical documents stay pinned by the release, but until they
land you cannot independently inspect what the pipeline authorized, the package inventory, or the
vulnerability verdict for a digest. Those hashes pin the canonical bytes only; read the note above
on what a canonical hash cannot prove about a future sanitized variant. Treat all of this as a
known gap in this policy, not as an implied guarantee.

**The checked-in `sbom/sbom.spdx.json` does not close that gap.** It is a reference document kept in
the tree for convenience, it describes no particular published image, and it can lag the canonical
branch. It is not the authoritative SBOM for any release; the authoritative document is the
digest-keyed one the canonical release produced, which is the one not yet published.

### Not published

- GitLab issues, merge requests, comments, and review history.
- CI job logs, pipeline records, and runner configuration.
- The canonical package registry itself, and the digest-keyed artifacts it holds.
- The release manifest, SBOM, vulnerability report, and vulnerability evaluation, whose bytes carry
  the canonical hostname by construction. Their SHA-256 values are in the receipt; the documents
  themselves are deferred until sanitized public variants exist.
- The canonical hostname, in any file, commit object, identity, release body, or asset.
- Any branch other than `main` and `rc`, and any tag other than stable `vX.Y.Z`.

## 3. Branch and tag rules on the public repository

| Ref | Who writes it | Rule |
| --- | --- | --- |
| `main` | Release automation only | Append-only, fast-forward. Never force-pushed, never rewritten, never deleted. |
| `rc` | Maintainers merging accepted pull requests | Contributor intake. Pull requests target this branch. Fast-forwarded onto each release commit. |
| `v*` | Release automation only | Create-only. An existing tag is never moved or deleted. |

`rc` is advanced by **fast-forward only**. Each release commit takes the `rc` tip at publication
time as its second parent, so fast-forwarding `rc` onto that release commit loses nothing. If `rc`
has moved since the release commit was built -- a pull request merged in the window -- the
projection fails closed rather than resetting, because a non-fast-forward reset would drop accepted
commits that are not yet in any release.

Direct pushes to `main` by a human are not part of any supported workflow. If public `main` has
moved in a way the automation did not produce, the projection fails closed rather than
reconciling: divergence is a signal that something wrote to the public repository outside this
policy, and silently overwriting it would destroy the evidence.

## 4. Why the public history looks the way it does

The public repository carries a **legacy prefix** of ordinary development commits from before this
policy existed, ending at `447f674` (2026-04-28). Those commits are kept exactly as they are; the
first release projection uses that commit as its first parent, so the public history fast-forwards
rather than being rewritten.

From that point the public history is **one commit per release**. A release commit is not a squash
of the intervening private commits in any meaningful sense -- it is a synthetic commit whose tree
is the tagged tree. The per-change history, the review discussion, and the CI evidence behind it
live on the canonical instance and are not public.

Two consequences worth stating plainly:

- **The first release commit is a very large diff.** It spans the gap between the legacy prefix and
  the first release published under this policy. That is expected and is not a history rewrite.
- **`git log --first-parent` on the public repository is a release log, not a development log.**
  The `--first-parent` matters: once a merged pull request makes `rc` a second parent of a release
  commit, a default `git log` walks those contributor commits too. For what changed in a release,
  read [`CHANGELOG.md`](CHANGELOG.md) and the GitHub Release body, both of which are written for
  that purpose.

Merged contributor pull requests are the exception that keeps attribution intact: `rc` is included
as a second parent of the next release commit, so the contributor's own commits stay **reachable in
the public history** under their own authorship. Be precise about what that does and does not buy.
The commits are reachable, with their authorship, and that is the guarantee. The release commit's
tree is still the canonical snapshot, so `git blame` and a first-parent walk attribute the released
lines to that synthetic commit rather than to the contributor's commit.

The guarantee depends on a repository setting, not only on the projection: a squash merge omits the
contributor's original commits entirely and a rebase merge rewrites their identities, so only merge
commits preserve them. Section 8 records that as a required setting.

## 5. No reverse sync

**Nothing is ever pushed, merged, cherry-picked, or scripted from GitHub into the canonical
instance automatically.** Accepted contributions are re-applied by a maintainer onto canonical
`main` and go through the same review and qualification as any internal change. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the contributor-facing description of that flow.

This is a hard rule, and the reasons are worth keeping written down:

- The public repository is writable by more people and protected by fewer controls than the
  canonical instance. An automatic inbound path would make GitHub's access control the effective
  access control for the release line.
- The qualification gates -- tests, header guard, SBOM, vulnerability evaluation -- run on the
  canonical instance. Code that arrived without passing through them has not been qualified,
  regardless of how it looks.
- Ordering matters for acceptance: a pull request is qualified on the canonical instance **first**,
  and only then merged into `rc`. What merging early makes irreversible is the **ancestry**, not
  the code. Once a release commit takes `rc` as a second parent, the contributor's commit is
  reachable from public `main` permanently, and `main` is append-only, so nothing removes it
  without a history rewrite. The change itself is never forced to ship: a release commit's tree is
  the canonical tagged tree, so it carries what canonical `main` contains and omits an unqualified
  `rc` change on its own, and before any projection a forward revert on `rc` undoes the intake
  merge without rewriting it. Every correction after a release is forward-only, which is why the
  qualification happens while the commit is still outside the permanent public record.

## 6. Credentials

The credential that writes to the public repository is a single-repository, fine-grained token
owned by the `mcknly` account, with **Contents: read and write** and nothing else. It is installed
as the protected, masked CI variable `GITHUB_MIRROR_TOKEN`, scoped to a dedicated
`github-publication` environment. Section 8 has the owner action that mints it.

The environment scope is the grant, not a label. A protected CI variable is otherwise readable by
every job running on a protected ref regardless of that job's `rules:`, and GitLab CE has no
protected environments, so the set of jobs permitted to declare that environment is pinned by a
test rather than left to review attention. Exactly two jobs declare it: `github_release_publish`
and `github_release_withdraw`. `test_only_approved_jobs_receive_scoped_credentials` fails if any
other job does, or if any job declares an environment the test does not already know. The drift
audit declares none: it reads the public repository anonymously and can never write to it. See
[`CI.md`](CI.md).

The token is never embedded in a remote URL. A URL-embedded credential survives in `git remote -v`,
in error text, and in any traced command, which makes it a leak waiting for the first failing job
that prints a command line. Git receives it from an askpass helper that reads it from the
environment, with global and system configuration disabled so no credential helper can supply or
store anything. The REST client never forwards it across a redirect, since release asset downloads
redirect to storage on another host.

Rotation: rotate on maintainer change, on any suspected exposure, and otherwise on the schedule
that applies to the project's other publication credentials. Rotation is a variable replacement --
no code or policy change is needed, and no in-tree file records the token's value or identity.

## 7. Drift detection and recovery

Drift is any difference between what this policy says the public repository should contain and
what it actually contains: a public `main` that is not the expected commit, a stable tag pointing
somewhere unexpected, a missing or altered receipt, or a release asset whose bytes no longer hash
to the recorded value.

**Ownership.** Drift is a maintainer-owned alert, not an automated remediation. Nothing repairs the
public repository on its own, because every plausible automatic repair is a rewrite of published
history.

**Status.** The projection is the `github_release_publish` job, which an operator starts on a
protected stable tag pipeline once the canonical release has completed. It is the **only** writer:
outside that job, **no public publication happens at all** -- not automatically, and not by hand.
There is no exception for a maintainer, because sections 1 and 3 admit no writer to public `main`,
the stable tags, or the release artifacts other than the publication job: a hand-made release
commit would be a published tree that nothing checked against the canonical tagged tree, and no
receipt would bind the two. The first public push is the job's, on the first ordinary protected
stable tag after it landed. Until that run the public repository stays at the legacy prefix
described in section 4, so any change there is drift by definition.

**Detection.** `github_publication_audit` runs on a schedule and compares the public repository
with the receipts the publication job stored durably on the canonical instance. It fails when
public `main` is not the latest release commit (before the first release: not the legacy prefix),
when the release commits do not form an unbroken first-parent chain from the legacy prefix, when
`rc` does not descend from `main`, when any public tag, branch, or release has no receipt, when a
release commit or tag differs from its receipt, when a release's name or notes differ from what
was published -- a withdrawal notice included, reason and all -- when GitHub's "latest" marker is
on anything but the highest release that has not been withdrawn, or when a published asset's
bytes differ. A failing scheduled pipeline is the alert.

Publication holds to the same chain before it writes: public `main` must be exactly the previous
release commit, or the legacy prefix for the first release. A commit written to `main` outside the
job therefore stops the next release instead of becoming its parent, and stays visible as drift
until someone establishes what wrote it.

**Recovery.** Because `main` and `v*` are append-only, recovery is forward-only:

- *Projection failed partway.* Re-run it. The operation is designed to converge: a commit, tag,
  release, or asset that already matches is success, and a conflicting one is a hard failure.
  `main`, `rc`, and the tag move in one atomic push, so a retry never finds one of them moved
  without the others, and the release stays a draft until its assets have been read back. The job
  reads only the protected tag and durable release state, so it can be re-run however long after
  the tag. A public tag or commit that is not byte-identical to what the job would create, or that
  does not follow the previous release, is never adopted, and a published release missing an
  asset is not quietly repaired: both are drift.
- *Projection stopped partway, then the version was yanked.* The public tag cannot be deleted, so
  the withdrawal job fails rather than reporting success over it. Re-run the projection: for a
  yanked version whose refs already landed it pushes nothing new and finishes the release under
  its withdrawn name and notes, never as latest, and writes the receipt that the audit and every
  later release anchor to.
- *Public `main` diverged.* Stop. Do not force-push. Establish what wrote to it and how, then
  reconcile deliberately -- publishing a new release on top of an unexplained commit inherits it
  into the permanent public ancestry.
- *A published release must be withdrawn.* `github_release_withdraw` runs after the canonical
  yank and marks the public release as withdrawn through the API; it does not delete the tag or
  rewrite history. Withdrawal must remain possible
  during the outage that would prevent a scan from running, and when the vulnerability motivating
  the withdrawal is the one that would fail that scan -- so the withdrawal path deliberately
  carries no qualification gate. See [`RELEASING.md`](RELEASING.md).

## 8. Owner-side setup checklist

These are one-time account-level actions on the public repository. They are recorded here because
they are part of the policy's enforcement, not merely setup trivia. **The order matters in two
places**, and both are called out below.

Refs and protection:

- [ ] `rc` branch created at `447f674`, the current public `main` and the end of the legacy prefix
      described in section 4. Naming the commit rather than "wherever `main` is" keeps this
      checklist and section 4 describing the same point in history.
- [ ] Rulesets split in two per ref, because a ruleset's bypass list applies to every rule in it:
      one with **no bypass** holding the rules nothing may break, and one that only the
      publication identity bypasses. Putting both kinds in one ruleset would let the job
      force-push too.
- [ ] `main` and `rc` protected against force-push and deletion, with no bypass. For the job, that
      is what makes its own updates fast-forward only.
- [ ] `main` protected against direct writes, bypassed only by the **Repository admin** role.
- [ ] `v*` protected against deletion and update with no bypass, and against creation bypassed
      only by Repository admin.
- [ ] `rc` requiring a pull request, merge commits only, bypassed only by Repository admin -- the
      job fast-forwards `rc` onto each release commit, and would otherwise fail on its last update.
- [ ] Understood: on a repository owned by a user account, a ruleset cannot name one token. The
      fine-grained token acts as the account that owns it, and the only bypass available is a
      role, so "the publication identity" is the owner account and its tokens. The rulesets keep
      every other writer out; a hand-made write by the owner is caught after the fact by the
      drift audit (section 7), not prevented. Moving the repository to an organization, or
      publishing through a GitHub App, would let a ruleset name the publisher alone.
- [ ] Merge commits enabled, and **squash merging and rebase merging disabled**. This is what makes
      the contributor-ancestry guarantee in section 4 true: a squash merge omits the contributor's
      original commits and a rebase merge rewrites their identities, and neither is detectable from
      the projection afterwards.

Intake, in this order:

- [ ] Private Vulnerability Reporting enabled. This is a repository setting with no dependency on
      the tree, and it must be live **before the first public push**, because the published
      `SECURITY.md` names its URL and that URL 404s until the setting exists -- see
      [`../SECURITY.md`](../SECURITY.md).
- [ ] Issues enabled **only after** the first projection has carried
      `.github/ISSUE_TEMPLATE/config.yml` onto public `main`. GitHub reads the issue chooser from
      the default branch, so Issues enabled before that file is published are blank issues with no
      chooser -- precisely the first-contact path the chooser exists to close.
- [ ] Pull requests enabled.
- [ ] GitHub Actions left disabled for build, qualification, and release purposes (section 1).

Credentials and egress:

- [ ] Publication token minted as described in section 6 -- this repository only, Contents: read
      and write only, with an expiry whose rotation date is recorded -- and installed as the
      protected, masked, hidden variable `GITHUB_MIRROR_TOKEN` scoped to `github-publication`. It
      needs no Workflows permission, and should not get one: while it lacks it, a push that would
      carry a workflow file is rejected, which keeps section 1's prohibition enforced by GitHub
      rather than by review.
- [ ] Docker Hub repository metadata links to the public source repository, so the source is one
      click from the registry page.
- [ ] Runner egress to `github.com`, `api.github.com`, and `uploads.github.com` (release asset
      uploads use their own host) confirmed.
- [ ] A pipeline schedule on canonical `main` for the drift audit, owned by the maintainer who
      should receive its failure notification.

Ongoing, not one-time: GitHub bases a pull request on the repository's **default branch**, which is
`main`. No repository setting keeps `main` as the clone default while making `rc` the pull-request
base, so pull requests will arrive against `main`. Maintainers retarget them to `rc` or close them
with a pointer; [`CONTRIBUTING.md`](CONTRIBUTING.md) tells contributors to expect that and gives
them a compare link whose base is already `rc`.

## 9. Related documents

- [`../README.md`](../README.md) -- project overview, carrying a short source publication note.
- [`../SECURITY.md`](../SECURITY.md) -- vulnerability reporting path and container hardening.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) -- internal and external contribution workflows.
- [`RELEASING.md`](RELEASING.md) -- the release contract the projection is triggered by.
- [`CI.md`](CI.md) -- pipeline stages, publication gates, and environment-scoped credentials.
