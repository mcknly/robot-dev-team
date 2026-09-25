<!--
Robot Dev Team Project
File: docs/SANITIZATION_REPORT.md
Description: Pre-publication sanitization, hostname, secrets, and license audit of the release image.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Pre-Publication Sanitization Audit

_Date:_ 2026-09-24
_Agent:_ Claude
_Issue:_ #51 (refs #28)

This replaces the report dated 2025-10-27, which predated the CI and release layers, five agent
harnesses, and the wrapper credential rework. It was also wrong about the tree: three of the four
files it named as the sanitization mechanism no longer existed. Nothing from it is carried over.

## 1. What was audited

| | |
| --- | --- |
| Commit | `365d1f4c81bc5f6bd50f712727445c9a00f22cc2` on `main` |
| Pipeline | protected `main` pipeline 268 (build, smoke test, SBOM, vulnerability scan: all passed) |
| Image | manifest digest `sha256:4ae482405724e770b7740e22b98cd2f7391daab311a534c61fe87b28fb0a0d74`, `linux/amd64`, 20 layers |
| Binding | the commit's full-SHA tag resolves to that digest (`crane digest`); full-SHA tags are immutable |
| SBOM | staged file SHA-256 `d22eaa7f8b994c0262d6fe11b086871f7421ca3f4dadff5379de067759b53ed1`, 1,327 entries |
| Tree | every file tracked at that commit |

The image was audited **layer by layer**, not only as a merged filesystem. A later layer can delete a
lower layer's file, and a deleted file is invisible in the merged view but still present in every
pull. All 20 layers were extracted (9,947 regular files) with `scripts/image_audit.py extract`. So
was the merged filesystem (8,382), with `crane export "$REGISTRY_IMAGE@$DIGEST" - | tar -x`, which
is what `docker run` would see. Extraction treats layers as untrusted: only regular files and
directories are written. Links and device nodes are skipped, but their names and link targets
(1,414 links, no device nodes) are recorded, along with the 62 whiteouts, in the extraction's
`layers.json`. The image manifest and config, which every puller receives, are written next to
the layers.

## 2. Tools

| Tool | Version | Used for |
| --- | --- | --- |
| `crane` | 0.21.7, checksum-pinned in `scripts/release_tools.py` | resolving and fetching the image by digest |
| `scripts/image_audit.py` | this tree | layer extraction, the hostname scan, and the secrets baseline comparison |
| `detect-secrets` | 1.5.0, `scan --all-files` | secrets in the tree and in every layer |
| `scripts/license_review.py` | this tree | license classification (`docs/LICENSE_REVIEW.md`) |
| `scripts/header_guard.py` | this tree | license headers |

## 3. Canonical hostname

The canonical instance's hostname must appear nowhere public (`docs/MIRRORING.md` section 1). It
was checked against both the server host and the registry host, case-insensitively. Hosts are
compared without their port, so the registry's host counts as one name. The scan covered:

- file contents;
- file and directory names, including empty directories;
- link names and link targets, through `layers.json`;
- the image manifest and config (`Env`, `Labels`, and the `history` of build commands).

| Surface | Files scanned | Containing a canonical host |
| --- | ---: | ---: |
| Tracked tree | every tracked file | **0** (`test_no_tracked_file_names_the_canonical_instance_by_hostname`, enforced on every pipeline) |
| Image, raw layers, plus manifest, config and `layers.json` | 9,950 | **0** |
| Image, merged filesystem | 8,382 | **0** |

A scan that finds nothing is only evidence if it can find something:

- **Positive controls.** The scan was also run on the staged release SBOM, which carries the
  registry host by construction, and on a planted file named after the host. Both were flagged,
  the named file without its name being printed, and both runs exited non-zero.
- **Fail-closed counting.** `hosts` fails outright when it scans no files. By default it also
  treats the tree as an extraction, so it fails:
  - when any of the three metadata files is missing;
  - when the layer record is malformed;
  - when it scanned any number of files other than exactly those `layers.json` records, plus the
    three metadata files.

  The merged filesystem and the controls are not extractions. They were scanned with an explicit
  `--no-extraction`, so a missing file can never select the lenient mode by accident.

The nine occurrences the tree carried when #51 was opened were removed in !49. That includes the
two in `gitlab-connect` and `glab-usr`, which ship in the image.

This is target-list hygiene, not secrecy. The organisation, image, and namespace names are public,
and the host is a short guess from any of them; nothing is built on the assumption that it is
secret.

## 4. Secrets

### Tracked tree: 22 hits in 8 files, none a secret

| Where | Plugin | What it is |
| --- | --- | --- |
| `docs/GROUP_SETUP.md:115`, `gitlab/.env.example:15` | GitLab Token | the placeholder `glpat-xxxxxxxxxxxxxxxxxxxx` |
| `docs/GROUP_SETUP.md:117` | Secret Keyword | the placeholder `your-shared-secret` |
| `scripts/release_tools.py:81, 95` | Hex High Entropy | SHA-256 pins of the crane and grype release archives |
| `scripts/github_publication.py:42` | Hex High Entropy | the public legacy-prefix commit SHA |
| `tests/test_release_tools.py:26, 357, 2549, 2661` | Hex / Secret Keyword | test fixtures (`0123…`, `ci-password`, `credential-sentinel`, a string asserted *not* to reach logs) |
| `tests/test_goose_config.py:64`, `tests/test_wrappers.py:698, 722, 822` | Basic Auth | fixture URLs such as `https://user:pw@localhost` |
| `sbom/sbom.spdx.json` (8) | Base64 / Hex High Entropy | generated identifiers. Re-scanned pretty-printed, every hit is a `checksumValue`, an `SPDXID`, or a `packageVerificationCodeValue` |

### Image layers: 560 hits in 42 files (552 distinct), none a secret

| Where | Hits | Plugin | What it is |
| --- | ---: | --- | --- |
| `…/uv-0.12.1.dist-info/sboms/uv.cyclonedx.json` | 491 | Hex High Entropy | hash `content` values in uv's own SBOM |
| `/usr/sbin/pam-auth-update` | 12 | Hex High Entropy | MD5 sums of known earlier PAM config versions |
| Debian and Perl configuration (`openssl.cnf`, `nsswitch.conf`, `debconf.conf`, `Config_heavy.pl`, `Cwd.pm`, `c_rehash`) | 17 | Secret Keyword | commented examples (`# input_password = secret`), `passwd: files`, and variable names like `$pwd` |
| CPython standard library (`base64.py`, `shlex.py`, `tempfile.py`, `hashlib.py`, `secrets.py`, `urllib/request.py`, `pydoc_data`, sysconfig) | 13 | Base64 / Hex / Keyword | alphabet tables, known-answer hashes, docstring examples, module names |
| Perl headers and modules (`uni_keywords.h`, `perl.h`, `BigFloat.pm`, `HTTP/Tiny.pm`), `gitweb.cgi` | 7 | Base64 / Hex / Basic Auth | generated lookup tables, constants, and documented `user:pass@` examples |
| Python packages (`pydantic`, `pydantic_settings`, `fastapi`, `urllib3` in pip) | 12 | Keyword / Basic Auth / Entropy | docstrings and documented examples (`user:pass@host`, `"supersecret"` in a FastAPI tutorial file) |
| `CACHEDIR.TAG` (two) | 2 | Hex High Entropy | the standard cache-directory signature |
| `/work/scripts/release_tools.py`, `/work/scripts/github_publication.py` (each in two layers) | 6 | Hex High Entropy | the same pins and public SHA as in the tree |

Every hit was read at its line. The classified set is committed as
`security/image-secrets-baseline.json`, keyed by path (layer number removed), plugin, and
detect-secrets' hash of the matched string. `scripts/image_audit.py compare-secrets` is how a
later image is checked against this audit (section 11). It requires an exact match, and fails in
either direction:

- on a hit outside the baseline (`UNCLASSIFIED`);
- on a baseline hit the scan no longer reports (`LOST`).

A lost hit means the scan did not read part of the image, or the image changed. Either way, the
scan is not evidence about the files the audit classified.

The baseline also records the scanner's version, plugins, and filters. The comparison refuses:

- a scan with a different version, plugin set, or filter set;
- an empty scan;
- a scan that shares no hit with the baseline.

A retuned or misdirected scanner reports *fewer* hits, and each of these would otherwise read as
clean. A baseline can only be written from a scan whose one regex filter is the metadata exclusion
below. An `--exclude-lines` or `--exclude-secrets` filter, or any other file pattern, is refused,
because every later release would then be held to a profile that suppresses hits.

The secrets scan excludes exactly the three metadata files `extract` writes, with
`--exclude-files '^(image-manifest|image-config|layers)\.json$'`. The hostname scan already
covers them. The image config changes with every build, so a hit in it could never be classified.
Scanned without the exclusion, they produced no hits for this digest. The pattern is part of the
filter set the baseline binds, so omitting it, changing it, or adding a pattern fails the
comparison. The runbook's scan of a fresh extraction reproduced exactly the 552 classified hits.

## 5. Host-specific paths and identities

- **The image's project-owned files** (`/work/app`, `/work/config`, `/work/prompts`,
  `/work/scripts`, `/work/pyproject.toml`, `/work/README.md`, and the three `/usr/local/bin`
  scripts) contain no operator home, mount, or workstation path. The only operator reference is the
  README's intentional link to the author's public essay.
- **The tracked tree** contains no operator paths. Maintainer handles appear only where they state
  ownership (for example, the vulnerability exceptions owner).
- **Commit identities** on canonical `main` carry the canonical host in agent email addresses.
  They are never published: the public history is one synthetic commit per release, authored by a
  fixed noreply identity, and the outbound host gate checks author, committer, and tagger before
  any public write (`docs/MIRRORING.md` section 2).

## 6. Default configuration a stranger runs first

- **`.env.example`**: every token and secret is blank; the example agent emails use `your-org.tld`.
  `GLAB_HOST=host.docker.internal` is a working, non-placeholder value: it points at a GitLab on the
  Docker host, so an unconfigured setup fails at connection time rather than at the wrappers'
  placeholder guard. Deliberate, as recorded on #51.
- **`docker-compose.yml`**: mounts are relative (`./prompts`, `./config`, `./projects`) or the
  operator's own `$HOME` agent directories. The optional harness mounts are commented out.
- **`config/routes.yaml` and `prompts/`**: role-based routes and generic prompts, with no instance
  names, URLs, or credentials.

## 7. The wrappers shipped in the image

`gitlab-connect` and `glab-usr` are copied to `/usr/local/bin`. Re-read as a stranger would meet
them:
- Their errors name no host. The placeholder guard says `Export GLAB_HOST=<your-gitlab-host>`, and
  never suggests `gitlab.example.com`, the value the guard rejects.
- They echo only values the operator supplied.
- `glab-usr` redacts the token in its tool-call log line.

## 8. What else the image carries, recorded as decisions

- **The release tooling ships in the runtime image.** `COPY scripts/` puts `release_tools.py`,
  `github_publication.py`, `license_review.py`, `image_audit.py`, and the CI helpers under
  `/work/scripts`. None holds a credential or a canonical host; the project is always
  `CI_PROJECT_ID`. It is kept because the harness installers in the same directory are needed at
  runtime, and splitting the directory is not worth the churn. Recorded here so a reader who finds
  it knows it is intended.
- **FastAPI ships an `.agents/skills` directory** of agent documentation inside its package. It is
  third-party and inert, since the agents work on mounted project checkouts, not on the
  environment's `site-packages`.
- **uv's build cache is left in the image** (`/root/.cache/uv`, 33 MB of public PyPI artifacts).
  Not a secret; tracked in **#84**.

## 9. Licenses

Classified per `docs/LICENSE_REVIEW.md`, re-run for this digest with the section 7 procedure
(package 45, recorded hash above, name bound to pipeline 268's commit): **1,327 entries resolved,
0 unresolved, 0 needing review**, `classify` exit 0.

What that supports, and no more: no component's license prohibits distributing the image, and the
project's own code is MIT. The image is an **aggregate**; it is not MIT-licensed as a whole, and
distributing it carries obligations. The obligations attach to distributing the binaries, which
first happens with the Docker Hub push in #8, not with the source-only GitHub publication:

- the corresponding-source statement for GPL/LGPL/MPL components: **#54**;
- license notices for `glab` and `uv`'s compiled-in dependencies, including the FreeType credit:
  **#83**.

`scripts/header_guard.py`: every tracked source, documentation, and configuration file carries
the license header.

## 10. Findings

| Finding | Status | Owner |
| --- | --- | --- |
| Canonical host in the tree (nine occurrences, two image-borne) | Fixed in !49; guarded tree-wide | done |
| Canonical host in the image | None found | done |
| Secrets in the tree or image | None found; all hits classified | done |
| Stale, partly false 2025 report | Replaced by this document | done |
| Release-SBOM license metadata gap | Resolved in #82 (!52) | done |
| Corresponding-source statement | Open; gates #8 | #54 |
| License notices for `glab` and `uv`'s compiled-in crates | Open; gates #8 | #83 |
| uv build cache in the image | Open; not a publication blocker | #84 |

Owners for open issues are the maintainer, @cavin, as the project records ownership outside the
tracker.

## 11. The release delta, and the pre-tag re-check

Image builds are not reproducible: every commit, even a docs-only one, produces a new digest. So
the digest a release promotes can never be the one a report committed before it names. This audit
therefore binds the release to itself in two parts:

1. **An allowed delta.** The release commit (v0.3.0) differs from the audited commit only by:
   - version metadata (`pyproject.toml`, the root entry in `uv.lock`);
   - documentation (`docs/`, not copied into the image);
   - `security/` and `tests/` (not copied into the image);
   - one added file that *is* copied: `scripts/image_audit.py`, the tool that performed this audit.

   `git diff 365d1f4 <release-commit> --stat` shows it.
2. **A mechanical re-check of the release digest before tagging**, in `docs/RELEASING.md`:
   - the hostname scan of every layer plus the manifest and config, for both hosts, which must
     find 0 and must scan exactly the files the extraction recorded;
   - a fresh secrets scan compared against `security/image-secrets-baseline.json`, which must match
     it exactly: 0 unclassified hits and 0 lost;
   - the license review of section 9, which must exit 0.

   The results are recorded on the release's issue or MR.

## 12. Cadence

- **Every stable release:** the section 11 re-check, before tagging. It is mechanical and
  comparative, and it is what keeps this report true of each release's image.
- **A full audit like this one:** before any change of base image or distribution; when a harness
  or installer is added; when the wrappers or credential handling change; when the `Dockerfile`'s
  copy boundary changes; or when the re-check reports a hit, unclassified or lost, that no known
  change explains. Otherwise at least once a year.
- **A baseline refresh, not a full audit:** hits, lost or unclassified, confined to a component a
  merged MR knowingly bumped. Most baseline entries come from uv's own SBOM (491 of 552), and the
  `TOOL_RELEASES` checksums sit in `scripts/release_tools.py`, so a uv or pin bump moves them
  routinely. Read the changed hits, refresh `security/image-secrets-baseline.json` from the new `main`
  digest in a follow-up MR, and name the bump there (`docs/RELEASING.md`).

Not covered here, by design: vulnerability scanning (the release gate on the exact digest,
`docs/CI.md`), the agent CLIs installed at runtime (not in the image), and the public GitHub side
(`docs/MIRRORING.md`, with its own outbound gate and drift audit).
