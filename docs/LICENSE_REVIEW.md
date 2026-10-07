<!--
Robot Dev Team Project
File: docs/LICENSE_REVIEW.md
Description: License classification of the release image's SBOM, and the obligations it carries.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Release Image License Review

This document classifies the license of every component the release SBOM records for one image
digest, and states what redistributing that image obliges the project to do. It is the license
evidence the pre-publication audit (`docs/SANITIZATION_REPORT.md`) cites. The statements here are
no stronger than the recorded evidence; where the evidence stops, the document says so.

This is an engineering classification, not legal advice. Where a finding needs a maintainer
decision rather than a fact, it is marked as one.

## 1. What was reviewed

| | |
| --- | --- |
| Image | manifest digest `sha256:eed2d103edddccab26b6ab2f34ce833da9cb807a842bbf6e67dfd87c199f38d7`, built by the protected `main` pipeline for commit `44d54f4` |
| SBOM | the byte-exact document `sbom_publish` staged for that digest: SPDX 2.3, Syft 1.42.2, 1,327 package entries, SHA-256 `96f4f1847540bd4680a20974f642d29fa69e8b1e0fe5018074a6e529ea559465` (as the package registry records it for `robot-dev-team-sbom/sha256-eed2d103…/sbom.spdx.json`) |
| Tool | `scripts/license_review.py`, standard library only |
| Evidence | `security/license-evidence.json`, retrieved 2026-09-23 |

The unit of analysis is the **image**: the Debian base, CPython, the application's Python
dependency closure, and the two third-party binaries the `Dockerfile` installs (`uv`/`uvx` from
PyPI and `glab` from GitLab's release `.deb`). The agent CLIs that `docker-entrypoint.sh` installs
at first boot are not in the image and are out of scope, as `docs/RELEASING.md` describes;
an operator who enables a harness receives it from its vendor under the vendor's terms.

## 2. Why the SBOM showed `NOASSERTION`

The staged SBOM declared no license for 1,183 of its 1,327 entries and concluded none for 1,326.
Neither number indicates a problem with a component.

- **`licenseConcluded` is almost always `NOASSERTION`.** Syft reports what packages declare and
  does not conclude licenses. The single exception is `libcrypt1`, whose concluded value is an
  unidentified `LicenseRef-<hash>`: Syft found license text it could not name. The actionable
  field is `licenseDeclared`.
- **1,038 of the gaps are 519 Rust crates counted twice.** `uv` and `uvx` are two copies of one
  Rust build, and Syft reads the dependency list compiled into each (cargo-auditable data), which
  names crates and versions but carries no licenses.
- **134 are Go modules compiled into `glab`,** for the same reason: Go build information records
  module paths and versions, not licenses.
- **The Debian packages are nearly complete.** Syft reads `/usr/share/doc/<package>/copyright`
  and fails only where that file is free-form (`libcrypt1`) or absent (`glab`, whose GitLab-built
  `.deb` installs no copyright file).
- **The rest are known quantities** with license files in the image: CPython, `annotated-types`
  (which states its license only as a classifier), six Windows launcher executables pip vendors
  from distlib, and the SBOM's root entry for the image itself.

## 3. Method and sources

`scripts/license_review.py classify` first binds the SBOM to the image. The document's SPDX
`name` must equal the image it was generated from (`${CI_REGISTRY_IMAGE}:${CI_COMMIT_SHA}`,
the same check `sbom_publish` and `release_publish` apply), and its SHA-256 can be required to
equal the file hash the package registry records for the digest-keyed package. `--digest` on
its own is only a label. It then takes each entry's `licenseDeclared` when present, and
otherwise the entry in `security/license-evidence.json` keyed by `type/name@version`. Every
evidence entry names where it was read and when:

| Components | Source | Entries |
| --- | --- | --- |
| Debian packages, Python packages, Go `stdlib` | the SBOM's own `licenseDeclared` (Debian copyright files, Python metadata, Go toolchain) | 144 |
| 518 Rust crates in `uv` / `uvx` | **the CycloneDX SBOM uv's publisher ships inside the image**, `uv-0.12.1.dist-info/sboms/uv.cyclonedx.json` (cargo-cyclonedx 0.5.9) | 1,036 |
| `uv` itself | crates.io, `uv` 0.12.1 | 2 |
| 133 Go modules in `glab` | deps.dev API v3, which detects licenses in the module source | 133 |
| 12 others | read by hand from the image, the upstream source, or the module source; each entry records its path and a note | 12 |

The publisher SBOM is preferred for the crates because it is exact to this build, authored by the
party that built it, inside the audited artifact, and checkable offline. Each imported entry
records that file's SHA-256, so the answers are pinned to one document. An import never
overwrites a manual entry, and it reports any disagreement with an earlier one. It was cross-checked
against an independent crates.io lookup of all 519 crates: every license agrees, and the 25
textual differences are crates.io still storing Cargo's legacy `/` separator where the SBOM
writes `OR`.

A web lookup cannot be version-pinned the way a CLI is, so the *answers* are pinned instead: the
evidence file records each resolved expression, its source URL or image path, and the retrieval
date, and the classification is reproducible offline from it.

**Classification.** Each expression is reduced to the obligation a distributor must accept: an
`OR` takes the least restrictive alternative, an `AND` the most restrictive part. The rules fail
closed rather than guess:
- An expression with any character the parser can't place (`MIT?`, a comma, a legacy `/`) is
  rejected rather than read as its recognisable part.
- A deps.dev answer that includes unidentified (`non-standard`) license text goes to review by
  hand, even when a standard license sits beside it.
- Debian's non-SPDX short names (`Expat`, `public-domain-md5`, `BSD-3-clause-Regents`, ...) count
  as permissive only when they match a listed name exactly or followed by `-`. Any other name is
  **needs review**. The GNU families (`GPL`, `LGPL`, `GFDL`, ...) are matched first.

One caveat makes the Debian figures conservative: a Debian copyright file covers the whole
*source* package -- documentation, tests, build scripts -- so Syft's `AND` of every license in it
overstates what the installed binary package carries.

## 4. Result

| Type | Entries | `NOASSERTION` before | Unresolved after | Permissive | Weak copyleft | Strong copyleft |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| cargo (`uv`, `uvx`) | 1,038 | 1,038 | 0 | 1,030 | 8 | 0 |
| golang (`glab`) | 135 | 134 | 0 | 129 | 6 | 0 |
| deb | 124 | 2 | 0 | 16 | 3 | 105 |
| pypi | 22 | 1 | 0 | 22 | 0 | 0 |
| other (CPython, launchers, image root) | 8 | 8 | 0 | 8 | 0 | 0 |
| **Total** | **1,327** | **1,183** | **0** | **1,205** | **17** | **105** |

Every entry is resolved, and none needs review. `classify` lists every entry that is unresolved,
and every entry resolved to a license it cannot classify, by key, and **exits non-zero while
either list is non-empty**. A re-run therefore can't report success with an entry unaccounted
for. That is the tool's exit status, not a release gate; see section 8.

**Copyleft components:**

- **Strong copyleft appears only in Debian base-system packages** -- the GNU userland, glibc, and
  similar (105 entries, GPL family). No application dependency and no component of `uv` or `glab`
  is strong copyleft.
- **Weak copyleft**, 17 entries:
  - three LGPL-only Debian libraries: `libcrypt1`, `libseccomp2`, and `sqv`;
  - four MPL-2.0 crates in `uv`/`uvx` (8 entries): `astral-pubgrub`, `astral-version-ranges`,
    `option-ext`, and `priority-queue` (`LGPL-3.0-or-later OR MPL-2.0`);
  - six MPL-2.0 Go modules in `glab`: five `hashicorp/*` modules and `oss.terrastruct.com/d2`.
- **All 22 Python packages are permissive** (MIT, BSD-3-Clause, Apache-2.0, PSF-2.0).

## 5. What this supports, and what it does not

**Supported:** no component's license prohibits distributing the image, and the project's own
code remains under MIT. The image is an **aggregate**: each component keeps its own license, and
MIT does not extend to them. The application is a Python program running on CPython (PSF-2.0); it
links no GPL code, and invokes GPL programs such as `git` and `bash` as separate processes, which
does not make its own code subject to the GPL.

**Not supported:** a statement that the image as a whole is "MIT-licensed", or that
redistribution carries no obligations. It carries the ones in section 6.

## 6. Obligations found

1. **Corresponding source for GPL and LGPL Debian packages.** Redistributing these binaries
   obliges the distributor to make the corresponding source available. The image installs or
   upgrades packages from the Debian snapshot archive pinned by `DEBIAN_SNAPSHOT` in the
   `Dockerfile`, and inherits the rest from its base image, whose versions can be newer than that
   snapshot. Either way, the exact source for every binary version is on snapshot.debian.org,
   looked up by source name and version. What remains is to *tell* recipients so. **Decided:** a published pointer, not a mirror.
   - The public consumer documentation, the Docker Hub description, and an OCI label on the image
     name the pinned snapshot timestamp and where to fetch each package's exact source.
   - Mirroring was declined: it would store hundreds of megabytes of Debian source per snapshot
     bump, and pointing at the archive is the common practice for Debian-based images.
   - The residual reliance on snapshot.debian.org is accepted for a personal, non-commercial
     project, and revisited if the project becomes organizational or commercial.

   **Met in the tree.** "Corresponding source for the image" at the end of this section is the
   public statement, and the Docker Hub overview block in `docs/RELEASING.md` repeats it for the
   registry page. The image carries the labels `com.mcknly.robot-dev-team.debian-snapshot` (the
   timestamp) and `com.mcknly.robot-dev-team.debian-source` (both archive roots). Both are set from
   `DEBIAN_SNAPSHOT`, so they cannot drift from the build, and `scripts/ci-smoke-image.sh` checks
   the built label against the `Dockerfile`. The Docker Hub overview is a manual edit by the
   repository owner and must be in place before the first image push to Docker Hub.
2. **MPL-2.0 source availability** for the ten MPL components in `uv` and `glab`. Both binaries are
   redistributed unmodified from their publishers. MPL-2.0 requires informing recipients how to
   obtain the source of the covered files, which is published at the exact versions on crates.io
   and the Go module proxy. The same public statement covers it, pointing at section 4 for the
   component list rather than copying it.
3. **Attribution notices for the compiled-in dependencies of `uv` and `glab`.** MIT, BSD,
   Apache-2.0, and the FreeType License all require their notices to accompany copies. Debian
   packages carry theirs under `/usr/share/doc`, and Python packages carry theirs in their
   `dist-info`. The two binaries' upstream packages did not. GitLab's `glab` `.deb` installs no
   license text at all, not even glab's own MIT notice. uv's wheel carries uv's own license files
   but not those of its 518 crates, and its shipped SBOM names them without the notice texts.
   The gap was inherited from both upstream distributions, but the image is what the project
   redistributes.

   **Closed.** The image now installs:
   - `/usr/share/doc/glab/copyright`: glab's own `LICENSE` at the pinned tag.
   - `/usr/share/doc/glab/third-party/`: the license and notice files of every Go module compiled
     into `glab`, and of the Go standard library it was built with.
   - `/usr/share/doc/uv/third-party/`: the same for every crate compiled into `uv` and `uvx`, and
     for the Rust standard library, including the crates it vendors.
   - `notices.json` beside each tree, recording which files cover which component, where each file
     was taken from, and its SHA-256.

   The files are the upstream bytes, never text generated from an SPDX identifier.
   `scripts/third_party_notices.py` collects them from the pinned versions into `notices/`, which
   is committed and copied into the image. Eight crates publish an archive that omits a file; each
   has a reviewed, hash-pinned fallback taken from the upstream repository. One of them, `seahash`,
   has no license text anywhere upstream, so its `Cargo.toml` declaration (`license = "MIT"` and
   its authors) is what ships. Before the image is pushed, the build checks inside it, as the app
   user, that every `cargo` and `golang` entry in its SBOM has an installed notice
   (`docs/CI.md`). `docs/DEPENDENCY_MANAGEMENT.md` covers regeneration.

   **The SBOM's license for some crates understates what they link.** Four crates compile C code
   whose notices the crate's own identifier does not name:
   - `tikv-jemalloc-sys` builds jemalloc (BSD-2-Clause).
   - `zstd-sys` builds zstd (`BSD-3-Clause OR GPL-2.0-only`). The BSD branch is the one taken, and
     both texts ship.
   - `aws-lc-sys` builds AWS-LC, with its OpenSSL- and BoringSSL-derived code, fiat-crypto,
     s2n-bignum, and the Jitter Entropy library (which AWS-LC takes under BSD-3-Clause).
   - `ring` builds BoringSSL-derived C and assembly.

   All of that code is permissive, so section 4's classification does not change. The notices
   are installed from the crates' subdirectories. Where `aws-lc-sys` strips a file its
   `aws-lc/LICENSE` points at, the file comes from AWS-LC at the pinned submodule commit. The
   evidence file still records the publisher SBOM's answer for the Rust wrapper; this note is
   where the C code is accounted for.

   **d2 compiles other projects' MIT code into `glab`.** `oss.terrastruct.com/d2` ships its own
   MPL-2.0 license, which is all the SBOM join asks for, but its `NOTICE.txt` files only *link*
   to the licenses of code `glab` carries:
   - `lib/textmeasure` is derived from `faiface/pixel`.
   - `d2svg` embeds a modified `github-markdown.css` from `sindresorhus/github-markdown-css`.
   - `d2dagrelayout` embeds a `dagre.js` that d2 bundled from dagre 0.8.5, graphlib 2.1.8, and
     lodash modules.

   All five are MIT, whose notice has to accompany the copy. Each license file is installed from
   upstream at a pinned commit, beside the `NOTICE.txt` that names it. The bundled graphlib also
   keeps the BSD-3-Clause header of its `index.js` (`Copyright (c) 2014, Chris Pettitt`), whose
   binary condition asks for the notice, conditions, and disclaimer in the documentation. That
   header is installed whole beside graphlib's MIT `LICENSE`, as
   `graphlib/LICENSE-index-js-header`: lines 1-29 of `index.js` at the `v2.1.8` tag, pinned by
   hash, and `collect` checks that the same bytes occur in d2's `dagre.js`. graphlib's code in
   `glab` is therefore covered by both texts. `d2elklayout` (elk.js, EPL-2.0) is not linked into
   `glab`.

   One expected change in the next SBOM: Syft reads `/usr/share/doc/glab/copyright`, so it may now
   declare a license for `deb/glab`. The manual evidence entry for that key then drops out at the
   next `prune`.
4. **FreeType License credit clause.** `glab` compiles in `github.com/golang/freetype`, licensed
   `FTL OR GPL-2.0-or-later`. The FTL branch applies, and it requires the distribution
   documentation to state that the software is based in part on the work of the FreeType Team.
   **Closed:** `/usr/share/doc/glab/CREDITS` carries that statement and the credit line the
   FTL's authors ask for. The FTL text itself is installed with the module's other license files.

None of these obligations gates the first GitHub publication, which carries source only. They attach
to distributing the **binaries**, which first happens with the first Docker Hub push. The
corresponding-source statement and the license notices are ordered ahead of it for that reason.

### Corresponding source for the image

This is the public statement that obligations 1 and 2 require. The project points at the archives
that already hold this source, rather than mirroring it.

**Debian packages.** A Debian package in the image is one of two kinds, and the exact source of
both is on [snapshot.debian.org](https://snapshot.debian.org):

- **Installed or upgraded by the build.** These come from the snapshot the image's labels name.
  `com.mcknly.robot-dev-team.debian-snapshot` is the `DEBIAN_SNAPSHOT` timestamp.
  `com.mcknly.robot-dev-team.debian-source` gives both archive roots:
  `http://snapshot.debian.org/archive/debian/<snapshot>/` (`trixie`, `trixie-updates`) and
  `http://snapshot.debian.org/archive/debian-security/<snapshot>/` (`trixie-security`). A package's
  `.dsc` and source tarballs are in that snapshot's `pool/`.
- **Inherited from the Python base image and left alone by the build.** These are **not**
  guaranteed to be in the labelled snapshot. The build's `apt-get upgrade` replaces an inherited
  package only when the snapshot has a newer version. A base image is often built after the
  snapshot, so its version can be newer than anything under the labelled roots
  (`docs/DEPENDENCY_MANAGEMENT.md`). Those versions came from the Debian archive when the base
  image was built.

So look a package's source up by **source name and version**, which works for both kinds. List
them from the image, then fetch each version from snapshot.debian.org:

```bash
docker inspect --format '{{ index .Config.Labels "com.mcknly.robot-dev-team.debian-source" }}' <image>
docker run --rm --entrypoint dpkg-query <image> -W -f '${source:Package} ${source:Version}\n' \
  | sort -u
# For each "<source> <version>" line:
#   https://snapshot.debian.org/package/<source>/<version>/                   (browse)
#   https://snapshot.debian.org/mr/package/<source>/<version>/srcfiles?fileinfo=1   (JSON)
```

Nothing in CI resolves each of those versions against snapshot.debian.org. The labels are a
record of the build's APT sources, not a proof that every installed version is under them.

**MPL-2.0 components compiled into `uv` and `glab`.** The image redistributes both binaries
unmodified, at the versions pinned by `UV_VERSION` and `GLAB_VERSION` in the `Dockerfile`. Their
MPL-2.0 components (section 4) are published at the exact versions on
[crates.io](https://crates.io) and the [Go module proxy](https://proxy.golang.org).

**Agent CLIs** are not in the image. The container downloads them from their vendors at start,
under the vendors' terms (`docs/SYSTEM_DESIGN.md` section 7), so neither this statement nor the
release SBOM covers them.

## 7. Reproducing this

Three checks together tie the SBOM to the digest, and a run counts as verified only when all
three pass:

- **The bytes:** they are the file the registry records under the digest-keyed package
  (`--sbom-sha256`, checked by `classify`).
- **The name:** the document names the image built from one commit (`--source-name`, checked by
  `classify`).
- **The digest:** that image resolves to this digest (`crane digest`, below).

`--digest` alone is only the report's label. From a canonical checkout, with `glab` authenticated,
`jq` installed, and a token that can read the registry:

```bash
set -euo pipefail
PROJECT=mcknly-labs%2Frobot-dev-team
REGISTRY_IMAGE="<your-gitlab-host>/mcknly-labs/robot-dev-team"   # the project's CI_REGISTRY_IMAGE
DIGEST=sha256:eed2d103edddccab26b6ab2f34ce833da9cb807a842bbf6e67dfd87c199f38d7
PIPELINE_ID=263                  # the protected main pipeline whose build pushed DIGEST
VERSION="${DIGEST/:/-}"

# 1. The digest-keyed package and the hash the registry recorded for its sbom.spdx.json.
#    package_name is a substring filter, so the exact name and version are matched in jq.
PACKAGE_ID=$(glab api --paginate "projects/$PROJECT/packages?package_type=generic&package_name=robot-dev-team-sbom&per_page=100" \
  | jq -r -s --arg v "$VERSION" '[flatten[] | select(.name == "robot-dev-team-sbom" and .version == $v) | .id]
      | if length == 1 then .[0] else error("expected exactly one package for \($v), found \(length)") end')
RECORDED=$(glab api "projects/$PROJECT/packages/$PACKAGE_ID/package_files" \
  | jq -r '[.[] | select(.file_name == "sbom.spdx.json") | .file_sha256] | unique
      | if length == 1 then .[0] else error("sbom.spdx.json records \(length) distinct hashes") end')
glab api "projects/$PROJECT/packages/generic/robot-dev-team-sbom/$VERSION/sbom.spdx.json" > sbom.spdx.json

# 2. The image name comes from the build, never from the SBOM -- taking it from the document
#    would compare the document with itself.
COMMIT=$(glab api "projects/$PROJECT/pipelines/$PIPELINE_ID" | jq -r .sha)
SOURCE_NAME="$REGISTRY_IMAGE:$COMMIT"

# 3. That name resolves to this digest. Full-SHA tags are immutable (docs/CI.md), so this binds
#    the commit to the digest. The login is kept out of your Docker config.
python scripts/release_tools.py install-tools --directory .release-bin --tools crane
export DOCKER_CONFIG="$(mktemp -d)"
.release-bin/crane auth login "${REGISTRY_IMAGE%%/*}" -u "<username>" --password-stdin < "<token-file>"
test "$(.release-bin/crane digest "$SOURCE_NAME")" = "$DIGEST" || { echo "tag is not $DIGEST"; exit 1; }
rm -rf "$DOCKER_CONFIG"
```

Then classify:

```bash
python -m scripts.license_review classify --sbom sbom.spdx.json --digest "$DIGEST" \
  --source-name "$SOURCE_NAME" --sbom-sha256 "$RECORDED" --output summary.json
```

That run is offline and reads only the committed evidence. It exits non-zero if the bytes are
not the recorded file, if the document names a different image, or if any entry is unresolved
or needs review. `--sbom-sha256` and `--source-name` are both required. The summary records the
SBOM's SHA-256 and never the image name, which carries the canonical registry host. For a later digest, refresh the
evidence first, then classify:

```bash
python -m scripts.license_review import-cyclonedx \
  --cyclonedx <extracted>/usr/local/lib/python3.14/site-packages/uv-<version>.dist-info/sboms/uv.cyclonedx.json \
  --kind cargo --label "image:<path of that file in the image>"
python -m scripts.license_review resolve --sbom sbom.spdx.json   # crates.io and deps.dev, only what is missing
python -m scripts.license_review prune --sbom sbom.spdx.json     # keep only what this SBOM uses
```

Any entry `resolve` cannot answer, and any Debian or other entry without a declared license, stays
**unresolved** until a manual evidence entry records its source. A license name the classifier does
not know shows up as **needs review** and is added to the classifier deliberately. The release
digest will differ from the one reviewed here once further changes land on `main`, so the final
pre-publication audit re-runs this against that digest; only new or changed components need new
evidence.

The same SBOM also shows whether the committed license notices (section 6, obligation 3) cover
the digest's compiled-in components. This is offline, like `classify`:

```bash
python -m scripts.third_party_notices verify --sbom sbom.spdx.json
```

The build already ran the stronger form of this check inside the image before pushing it, so a
failure here means the tree changed after that build. It is a cross-check for the release-prep MR,
not a substitute for the build's check.

## 8. Decisions on the open questions

The review left two policy questions to the maintainer. Both were decided when this review
merged.

- **Gate or periodic review: periodic, at release preparation, for now.** Every release runs
  section 7 against the release digest before tagging (`docs/RELEASING.md`, "Prepare a release").
  A CI gate is deferred, not rejected: it would fail every release that adds a dependency until
  the evidence is updated by hand, and whether that friction is worth it should be judged from a
  few releases of real evidence churn. If a gate is added, it should fail on any unresolved or
  needs-review entry, and on strong copyleft **outside** Debian packages. GPL in the Debian base
  is expected, and gating on it would only produce noise.
- **Enriching the SBOM itself: no.** Syft misses licenses that exist in the image (uv's shipped
  SBOM, `libcrypt1`'s free-form copyright). Fixing that on the build side -- a Syft configuration
  change, a version bump, or an enrichment step -- would change the staged SBOM, which
  `sbom_publish` stages byte-exact and the release path must never re-derive. The SBOM stays
  Syft's raw output, and enrichment stays in this classification.

One follow-up this makes possible: the classification summary contains no hostname, so unlike the
SBOM it could be attached to each GitHub Release as public license evidence well before sanitized
SBOM variants exist. That is an option for the sanitized public evidence work, not a commitment.
