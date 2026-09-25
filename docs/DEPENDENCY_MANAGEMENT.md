<!--
Robot Dev Team Project
File: docs/DEPENDENCY_MANAGEMENT.md
Description: Dependency management policy and SBOM workflow.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Dependency Management & Reproducibility

This guide documents how we keep runtime tooling deterministic, how to update dependencies safely, and the minimum platform versions the project supports.

## Python dependencies
- All runtime and development packages are pinned in `pyproject.toml` and resolved through `uv.lock`.
- Container builds run `uv sync --frozen --no-dev --no-install-project` into `/opt/venv`.
  The application source remains importable from `/work`, while dependency resolution is locked
  and does not invoke the project's floating PEP 517 build requirement.
- Use `uv` to refresh locks. Inside the running container:
  - `docker compose exec app uv lock --upgrade` resolves both runtime and `dev` extras.
  - Copy the refreshed `uv.lock` back to the host: `docker compose cp app:/work/uv.lock uv.lock`.
- Review upstream changelogs before bumping versions and stage Python updates together with lockfile changes.
- **`fastapi` and `starlette` are both pinned exactly, and that coupling has a maintenance
  cost.** `starlette` carries its own direct pin because FastAPI's floor (`>=0.46.0`) is far
  below the version we need for its security fixes, so a FastAPI bump alone would not hold
  them. The consequence is that every future `fastapi` bump must re-check the `starlette`
  version it supports before the pin moves, or `uv lock` fails outright on conflicting
  specifiers. `starlette` is a genuine direct dependency regardless of the CVE argument --
  `app/api/dashboard.py` imports `WebSocketDisconnect` from it -- so the pin also fixes a
  declaration that was previously only transitive.
- Run `pytest` (or the relevant subset) before committing dependency bumps.
- Target cadence: evaluate monthly for minor updates and apply security fixes as soon as advisories land.

## System packages (APT)
- Base image: `python:3.14.7-slim-trixie` (Debian 13), pinned to its OCI index digest in the
  `Dockerfile`. When updating Python, update both the readable tag and digest and verify the
  selected `linux/amd64` manifest before rebuilding. The same tag and digest are repeated on
  every `.gitlab-ci.yml` reference that runs in a plain Python image -- currently six literals
  across seven consumers, since `.security_scan` is a hidden template extended by both scan
  jobs -- and they move together with the `Dockerfile` so the interpreter that builds and signs
  a release matches the one the release ships. Do not trust that count: discover the references
  by grepping for the current pin, and rely on
  `tests/test_release_contract.py::test_runtime_python_image_pins_agree` to fail on any copy
  that was missed. The one deliberate exclusion is `compat_python_floor`, which pins the
  `requires-python` floor rather than the runtime and must not move with it.
- **The runtime interpreter and the supported floor are two different numbers.** The image runs
  the current stable minor; `requires-python` records the oldest interpreter the project claims
  to work on. Raising the floor is a support-policy change and does not belong in a version
  bump. `compat_python_floor` in `.gitlab-ci.yml` runs a frozen sync plus `pytest` on the floor
  image so the claim in `pyproject.toml`, `README.md`, and `docs/AGENT_ONBOARDING.md` stays
  executed rather than merely asserted; the contract test above also requires that lane's
  interpreter series to equal the declared floor.
- **The floor lane's minor and its patch move for different reasons.** "Leave
  `compat_python_floor` alone" above means leave its *minor* alone: it is the support policy,
  and it moves only when `requires-python` does. Its *patch* is an ordinary security number and
  moves with current 3.12 maintenance, independently and on its own schedule. Nothing in the
  contract test compares patches -- it compares the lane's series to the declared floor -- so
  without this distinction the lane would be introduced on one patch and stay there forever,
  which is how it would come to run CI on a knowingly vulnerable interpreter while the runtime
  is being hopped precisely to clear CVEs. Bump it when 3.12 publishes a security release, the
  same way any other pin is bumped: readable tag *and* OCI index digest, re-resolved and
  verified against the current `3.12-slim-<codename>` tag.
- **Its distro is not its own axis, and the tree enforces that too.** The version exclusion is
  a support policy; a codename encodes nothing about a support floor, so this lane's codename
  moves with the runtime's. `test_runtime_python_image_pins_agree` requires the floor pin's
  codename to equal `PYTHON_IMAGE`'s while continuing to require its *version* to differ, which
  is the whole exclusion stated precisely. Without it a distro hop can move the `Dockerfile`,
  the `.gitlab-ci.yml` literals and the APT suites and still leave this lane on an end-of-life
  Debian -- the condition #59 exists to end, surviving on the one job nobody rebuilds locally.
  Re-resolve the floor image against the new codename in the same change as the base move.
- OS packages are installed via a Debian snapshot pinned by `DEBIAN_SNAPSHOT` in the `Dockerfile`. We track a snapshot no more than one month old (current: `20260915T194013Z`) to balance deterministic rebuilds with timely security fixes.
- **The snapshot must be the only Debian source.** The base image ships its own deb822 source
  (`/etc/apt/sources.list.d/debian.sources`) pointing at the live `deb.debian.org` mirrors, and
  APT reads `sources.list.d/` in addition to `sources.list`. Writing our snapshot entries
  without clearing that directory leaves both active, and the pin then governs nothing: APT
  picks the highest version across all sources, so any package the live mirror publishes ahead
  of the frozen archive silently enters the image. The `Dockerfile` clears the directory before
  `apt-get update` and then asserts, from `apt-get indextargets`, that every active index URI
  is under `snapshot.debian.org`. The assertion is deliberately on that property and not on the
  known filename, so a base-image layout change cannot quietly reintroduce a live mirror.
- **The suites and the base image name one Debian release, and the tree enforces that.**
  `tests/test_release_contract.py::test_apt_suites_match_the_base_image_codename` requires
  the three suites to be `<codename>`, `<codename>-updates`, and `<codename>-security` for
  the codename in `PYTHON_IMAGE`. The `indextargets` assertion above does not cover this:
  it governs where indexes come from, not which release they describe, and a base image
  bumped to a new codename with the suites left behind builds cleanly and produces a mixed
  image nothing downstream flags. That is the likelier direction of drift, since the base
  pin moves on a security cadence and the suites move only on a distro migration.
- The build runs `apt-get upgrade` under that snapshot before installing the explicit package
  list. Without it the snapshot would only govern the packages we name and their closure; the
  debs the base image ships preinstalled -- where most of the deb CVE surface lives -- would
  never move. The archive is frozen, so `upgrade` resolves identically on every rebuild.
- **Expect that upgrade to be a no-op most of the time, and do not read that as failure.** A
  freshly bumped base image is normally already at or ahead of a recent snapshot, so there is
  nothing left to upgrade. The step earns its place in the window where `DEBIAN_SNAPSHOT` has
  moved ahead of the base image's build date -- that is, exactly when the base image has gone
  stale relative to published security fixes. When judging whether an upgrade delivered
  anything, compare against the **base image the same build uses**, not against the previously
  released image: a base-image bump and an `apt-get upgrade` deliver overlapping sets, and
  attributing the base image's updates to the upgrade step overstates what the step does.
- `apt-get upgrade` never installs or removes packages, so any update whose new version needs a
  new dependency is *held back* and the command still exits 0 -- a security fix can vanish from
  a rebuild with no signal. The `Dockerfile` therefore parses the `N not upgraded` count from a
  follow-up `apt-get -s upgrade` and fails the build when it is non-zero. If that fires, decide
  deliberately: add the new dependency to the explicit install list, or move the snapshot. Do
  not switch the step to `dist-upgrade` to make it pass, which would let the resolver add and
  remove packages unattended.
- **That check fails closed, and it has to.** The simulation is captured before it is parsed, so
  `set -e` sees `apt-get`'s own exit status rather than a broken pipe feeding a parser; the
  parse runs under `LC_ALL=C` so a translated summary line cannot change the wording; and a
  result that is empty or not entirely digits fails the build instead of defaulting to zero. A
  guard whose own failure mode reads as "nothing was held back" would reproduce, one level up,
  exactly the silent-skip bug it exists to catch. If you change the parser, re-check it against
  a normal summary, a non-zero count, empty output, a non-zero `apt-get` exit, a translated
  summary, and output containing two summary lines.
- To update system packages:
  1. Pick the latest viable snapshot timestamp from <https://snapshot.debian.org> (verify with `curl -I` that the packages exist). Check all three suites the `Dockerfile` configures -- `trixie`, `trixie-updates`, and `trixie-security` -- because they are archived independently.
  2. Choose a timestamp at or after the base image's build date. This only prevents pinning an archive *older* than the image; it does not guarantee the upgrade has candidates, and usually it will not have any. Verify what the step actually resolves with `apt-get -s upgrade` inside the built image rather than assuming a package count.
     - The two archives are imported independently and `debian-security` normally lags `debian` by several hours, so a timestamp new enough to sit after the base image's build date is often *ahead* of the newest security snapshot. `snapshot.debian.org` resolves each request to the latest snapshot at or before the requested time, which means a timestamp ahead of an archive's newest import can silently start resolving somewhere else once that archive catches up. Prefer an exact timestamp that already exists in the listing, and record in the MR what each of the three suites resolved to at pin time -- the value in the `Dockerfile` is a request, not by itself a statement about what was installed.
     - Two guards will fail loudly on a codename change, and neither should be relaxed to make a build pass. The held-back parser reads `N not upgraded.` out of `apt-get -s upgrade`; trixie ships APT 3.x, which still emits that sentence (verified on the #59 migration build, APT 3.0.3). If a future APT changes the wording, re-anchor the parser to the new wording. And because `apt-get upgrade` never installs new packages, a library transition whose upgrade path needs a newly-named package is held back -- the remedy is to add the package to the explicit install list or move the snapshot, never `dist-upgrade`.
  3. Update `DEBIAN_SNAPSHOT`, rebuild with `docker compose build --no-cache`, and confirm the image installs successfully.
  4. Note the snapshot date in your commit message or MR description to show awareness of CVE coverage.
- Add or update packages in the `apt-get install` list sparingly and keep `--no-install-recommends`. That flag is meaningful only on `install`; `upgrade` never installs new packages, so it is inert there.
- **The snapshot cadence and the release vulnerability gate are coupled.** Because OS packages come
  from a frozen archive, `apt-get upgrade` in a rebuild changes nothing until `DEBIAN_SNAPSHOT`
  moves -- so the gate's deb findings drift back every month unless the one-month policy above is
  actually enforced. A release blocked by a *fixable* deb CVE is normally telling you the snapshot
  is stale, not that a package needs handling by hand. The gate and the cadence have to be
  maintained together or the gate turns into a monthly exception-writing exercise.
- **The base distro is supported again, and that changes what a deb finding means** (#59). While
  the image was on bookworm, Grype annotated every report with "all N deb packages come from an
  EOL distro; vulnerability data may be incomplete or outdated" -- the one failure mode a CVE
  gate cannot detect on its own, since under-reporting looks exactly like a clean image. On
  Debian 13 that warning is gone and `release_tools.eol_distro_note()` records
  `end_of_life: false`. The consequence is not that deb findings disappear. It is the opposite:
  on an EOL distro Debian stops issuing fixed-versions, so High/Critical deb CVEs surface as
  `not-fixed`/`wont-fix` and are recorded rather than blocking, while on a supported distro the
  same class of CVE carries a fixed-version as soon as one exists -- and the blocking rule is
  High/Critical **with an available fix**. Whether that blocks then depends entirely on the
  snapshot already carrying the fix, which is what makes the one-month cadence above load-bearing
  rather than tidy. The `EOL_DISTRO_RELEASES` entry for Debian 12 is retained on purpose: scan
  evidence is digest-keyed and outlives the image it describes, so a yank or a forensic
  re-evaluation can still be handed a bookworm report, and an entry deleted the moment it stopped
  matching would make that report read as clean.

### Agent CLI installs
The entrypoint runs the `scripts/install-*.sh` scripts that the active route config actually needs -- a harness is installed only when an enabled route runs its binary *and* that agent is credentialed (see `app/preflight.py` and issue #20). The **base image** ships no Node.js / npm (dropped when Gemini's harness became `agy`); the sole exception is the optional Pi harness, which bootstraps a pinned, user-local Node runtime at boot only when a `pi-*` route is enabled and credentialed (see the Pi row below), so operators who never enable Pi still get a Node-free image:

| Agent      | Method                          | Notes                                         |
|------------|---------------------------------|-----------------------------------------------|
| Claude Code | Native installer (`curl \| bash`) | Installs to `~/.local/bin/claude`; npm package is deprecated |
| Codex CLI  | Native binaries (GitHub Releases) | Installs the `codex` and required `codex-code-mode-host` musl binaries from one resolved release to `~/.local/bin` |
| Gemini (Antigravity CLI) | Native installer (`curl \| bash`) | Installs the `agy` binary to `~/.local/bin/agy`; replaces the deprecated `@google/gemini-cli` npm package. Agent identity is still `gemini`. |
| OpenCode (optional) | Native installer (`curl \| bash`) | Installs to `~/.opencode/bin/opencode`, symlinked onto `PATH`. Backs every `opencode-*` agent. |
| Goose (optional) | Native binary (GitHub Releases) | Fetches the `stable` release tarball directly to `~/.local/bin/goose`. Backs every `goose-*` agent. |
| Grok Build (optional) | Native binary (xAI artifact endpoint) | Resolves the current version from `https://x.ai/cli/stable`, then fetches the raw binary to `~/.local/bin/grok` (GCS mirror as fallback). Deliberately does not pipe xAI's `install.sh` -- see below. |
| Pi (optional) | npm package (`@earendil-works/pi-coding-agent`) on a pinned user-local Node | `scripts/install-pi.sh` downloads a **pinned, SHA256-verified** Node 22 tarball into `~/.local/node-v<ver>` (symlinking only `node` onto the shared `~/.local/bin` PATH; `npm`/`npx` stay in the versioned dir so an enabled Goose harness gets no `npx`), then `npm install -g --ignore-scripts --prefix ~/.local` drops `pi` in `~/.local/bin`. Node is pinned + cached; Pi tracks latest (npm install runs each boot). Behind the preflight gate, so installed only when a `pi-*` route is enabled. Backs every `pi-*` agent. |

All of them pull the latest version on every container start. Auto-updates are allowed.

- **Exception -- Grok suppresses its *runtime* self-updater** (`--no-auto-update` in the grok routes). This is not a version pin and not a deviation from latest-tracking: `scripts/install-grok.sh` still fetches the current stable build on every container start. What it stops is the *running agent* updating itself mid-deployment, which matters because grok's updater stages into `~/.grok/downloads` and repoints `~/.grok/bin/{grok,agent}` -- and `~/.grok` is bind-mounted from the host. A container-side update would therefore drop a ~150 MB binary into the operator's home directory and flip the symlink their **host** `grok` resolves through, to a version the container chose. (Verified by running `grok update --force-reinstall` against an isolated `HOME`: it writes `downloads/grok-<version>-linux-<arch>` plus both `bin/` symlinks.) The container's own binary lives in `~/.local/bin`, which is container-local, so nothing is lost by installing it once per boot.
- **Exception -- Pi's Node runtime is version-pinned.** Unlike the agent CLIs (which track latest), the Node runtime `scripts/install-pi.sh` bootstraps is pinned to a specific 22.x release (currently `22.23.1`, which satisfies the Pi package's `engines` constraint of `>=22.19.0`) and SHA256-verified against that release's `SHASUMS256.txt`, mirroring how the Dockerfile pins `glab`/`uv` and the SBOM workflow pins Syft. A full language runtime warrants a pin -- an unattended latest-Node bump could silently change behaviour under Pi. Pi itself (the npm package) still tracks latest on each boot. Bump the `NODE_VERSION` in `scripts/install-pi.sh` deliberately.
- Record deviations from the latest-tracking policy in this document if operations ever require pinning.
- **Prefer fetching a release artifact over piping a vendor install script**, where the vendor publishes one. Both track latest equally, but an install script is itself a mutable asset the vendor can change or withdraw: Block removed Goose's `download_cli.sh` from its release while continuing to publish the binaries, which took the container from booting to `curl: (22) ... 404` with no change on our side. Codex, Goose, and Grok fetch artifacts directly for this reason. Grok's vendor script adds three further reasons to avoid it in a container: it symlinks a bare `agent` command onto `PATH` (hopelessly ambiguous in a repo whose whole vocabulary is "agents"), it appends a block to `~/.bashrc` / `~/.zshrc`, and it stages downloads under `~/.grok` -- which is bind-mounted from the host, so all of that would land in the operator's home directory. The remaining `curl | bash` installers are a deliberate trade -- the vendor scripts handle platform detection and install-path conventions -- and the entrypoint's post-install binary check turns any such breakage into a refused boot rather than a dispatch-time failure.

## SBOM generation
- `scripts/generate-sbom.sh` exports the completed local image and scans its Docker archive with the digest-pinned Syft v1.42.2 container. Syft is not installed in the runtime image.
- Run `scripts/generate-sbom.sh` with no arguments to rebuild the Compose image and refresh `sbom/sbom.spdx.json`. CI passes its already smoke-tested image and artifact path explicitly, so both workflows use the same scanner.
- The helper copies the archive into the scanner container instead of bind-mounting it, which also works with Docker-in-Docker, and removes the temporary archive and container automatically.
- Commit updated SBOMs when dependency footprints change so downstream consumers can audit releases quickly. Protected default-branch image jobs also retain the final-image SBOM as a pipeline artifact.
- **Step 3 above is enforced, not remembered.**
  `tests/test_release_contract.py::test_reference_sbom_records_the_runtime_interpreter` asserts
  that the `python` package in `sbom/sbom.spdx.json` is the version `PYTHON_IMAGE` ships, so a
  pin bump that skips the regeneration fails in `validate` rather than going silent (#73). The
  binding is scoped to the interpreter deliberately: regeneration needs a Docker daemon, and
  Syft stamps a per-run `documentNamespace` and timestamp, so the document itself is not
  reproducible from the tree and cannot be compared byte for byte. The interpreter is the field
  that moves on every bump and is derivable from a file the test can read. Only the checked-in
  reference artifact is in scope -- the release path stages the smoke-tested image's own
  document, so a stale in-tree copy never reached a published release.
- **The same file is also bound to the base image's Debian release**, by
  `test_reference_sbom_records_the_base_image_distro`, because the interpreter binding cannot
  see a distro-only hop: #59 moved bookworm -> trixie with `PYTHON_VERSION` unchanged at 3.14.7,
  and an SBOM left on the old image would have satisfied the interpreter check verbatim while
  describing 125 packages the image no longer ships. The check reads the `distro` qualifier off
  the document's own deb purls
  (`pkg:deb/debian/base-files@13.8%2Bdeb13u6?arch=amd64&distro=debian-13`) -- a value Syft takes
  from the scanned image's /etc/os-release, not from anything this repository asserts, so the
  artifact stays evidence about the built image rather than a restatement of the pin. The
  qualifier carries the major release and never the codename, so the comparison goes through a
  codename-to-major table (`DEBIAN_RELEASES`) in the test module. That table is a maintenance
  cost with the same justification as `EOL_DISTRO_RELEASES`: the mapping is not derivable from
  the tree, and an unknown codename is a hard failure naming the line to add rather than a
  check that quietly stops applying.
- **Do not treat a vulnerability count as a property of the image alone -- it depends on what
  you hand the scanner.** Grype returns materially different results for an image than for the
  Syft SBOM of that same image. Measured on the **post-#58** `setpriv` image with Grype v0.116.1
  and a single DB built 2026-08-04T07:02:51Z: **376 matches / 10 fixable High-Critical scanning
  the image, 385 / 15 scanning its SBOM**. This is not a cataloging difference: the divergent
  artifacts carry identical purls and lookup keys. Always state the **image identity** (digest or
  commit), the scanner version, the DB build date, and the input form alongside a count. A count
  quoted without those four things cannot be compared to anything -- and image identity is in that
  list because it is the one most easily left out: the bullet below reports 381 / 12 and 458 / 49
  from the same scanner version and the same DB build, differing only in that it measures a
  *pre-#58* image. Three matching attributes, four different numbers. Expect these figures to go
  stale on any commit that moves the dependency footprint; refresh them by re-scanning the image
  whenever the SBOM is regenerated, or leave them attributed to the commit that produced them.
- **Scan the image, not the SBOM. The published SPDX document is not a substitute** (#61). Grype
  v0.116.1 forces `capture-symbols=all` when it catalogs an image itself, and its `gosymbols`
  qualifier then discards Go matches whose vulnerable symbols the compiled binary does not use; a
  package carrying no symbol evidence keeps module-granularity matching instead. SPDX 2.3 has no
  field for symbol evidence, and Syft's SPDX decoder (`spdxhelpers/to_syft_model.go`) rebuilds a Go
  package from its H1 digest alone -- so nothing on the read side would consume symbols even if an
  encoder started emitting them, and an SPDX input takes the coarse path. Confirmed on one archive,
  one Grype v0.116.1 binary, and one frozen DB (2026-08-04T07:02:51Z) against `sha256:602736dc`,
  which is a **pre-#58** image -- its Go stdlib residue is `gosu`'s, cleared since, which is why
  its counts are larger than the post-#58 pair above. The image scan and a symbol-bearing
  `syft-json` scan agree exactly (381 matches / 12 fixable High-Critical), while `spdx-json` and a
  `syft-json` written without symbols also agree exactly (458 / 49) and both emit Grype's
  missing-symbols warning. Those SBOM rows used Syft **v1.50.0** -- deliberately newer than the
  pinned v1.42.2 -- with `capture-symbols=all` explicitly set, and the SPDX it wrote still carried
  no symbols for any of the 137 catalogued Go modules while its `syft-json` carried them for 125.
  That is the load-bearing result: the loss is in the format, not in the pinned version, so it
  cannot be fixed by bumping the pin. The image result is a strict **subset** of the SBOM result --
  the entire 37-finding delta in fixable High-Critical is Go module-granularity matches that the
  symbol-aware path judged unreachable, so nothing the image scan drops is a match the precise
  matcher would have kept. A second SPDX round-trip loss: the CPython artifact catalogs as type
  `binary` on the image path and returns as `UnknownPackage` through SPDX, so an exception file
  keyed on an exact `type` (#50) can only be evaluated against a report produced by the image scan.
  Keep the SBOM for inventory, provenance, and audit -- that is what it is good at.
- That artifact expires; the durable copy does not. `sbom_publish` uploads the same bytes to the Generic Package Registry keyed by the published image digest, and every stable release copies them to its own package version and links them from the GitLab Release. `--source-name` is what ties a document to its image: the reference it records is checked at both ends against the digest the document is filed under, so keep it on any hand-run scan. Retrieval and recovery are in `docs/RELEASING.md`. The document describes the built image only -- harnesses installed at boot by `docker-entrypoint.sh` are outside it.

## Release CI tools

Protected release jobs download Crane 0.21.7 and the `security` stage downloads Grype 0.116.1,
both from their upstream release archives. `scripts/release_tools.py` pins each archive URL and
SHA256 checksum, extracts only the named binary, and installs it into the ephemeral CI workspace.
These versions are independent of anything in the runtime application image. Update the version,
URL, checksum, release tests, and `docs/RELEASING.md` together after reviewing the upstream
changelog. Everything else the release jobs do -- durable package files and the GitLab Release
record -- goes through the project API with `CI_JOB_TOKEN`.

Each job installs only what it needs: `install-tools --tools crane,grype` on the tag-time scan,
`--tools grype` on the default-branch scan, and `--tools crane` on both `release_publish` and
`release_yank`. The last one is load-bearing rather than tidy -- withdrawal must not depend on the
scanner's release asset or vulnerability database being reachable, which is exactly the outage
during which a bad release most needs pulling.

**Bumping Grype is not a routine dependency update.** The pin is what makes the release threshold
reproducible: Go matching alone changed materially between 0.106 and 0.116 (symbol reachability),
which moved this image's High/Critical count by more than any dependency bump has. Treat a version
change as a measurement change -- re-scan the current released digest before and after, and record
the delta -- and note that `release_tools.py` rejects a report produced by any version other than
the pinned one, so a bump that is not carried through the pin fails the gate rather than quietly
changing its meaning. Also re-verify that `grype config --load` remains offline and renders
`registry.auth` as the mapping list validated by `validate_grype_registry_auth()`; the guard fails
closed if that version-specific shape changes. Syft, on the SBOM path, is pinned separately and for
different reasons.

## Supported toolchain versions
- Python (runtime image): 3.14.x (current image 3.14.7)
- Python (supported floor): 3.12.x, declared as `requires-python = ">=3.12"` in `pyproject.toml` and executed by the `compat_python_floor` CI lane on 3.12.14. This is the oldest interpreter the project claims to run on, not the one it ships. Its **minor** is the support policy and moves only with `requires-python`; its **patch** is a security number and tracks current 3.12 maintenance on its own schedule -- see the floor-lane rule below.
- Static type checking: `mypy.ini` sets `python_version = 3.12`, which tracks the **floor** above, not the runtime image -- that is what keeps it catching syntax a local 3.12 developer environment could not run. Move it only when the floor moves, together with the `compat_python_floor` pin and the `requires-python` specifier.
- uv: 0.12.1 (pinned in both `Dockerfile` and the `validate` job in `.gitlab-ci.yml`; bump both)
- FastAPI stack: see pinned versions in `pyproject.toml`
- GitLab CLI (`glab`): 1.111.0
- Docker Engine: ≥ 24.0 (required for the compose features we rely on)

## Update checklist
1. Create a feature branch and refresh `uv.lock` (and `pyproject.toml` when bumping direct pins).
   Prefer `uv lock --upgrade-package <name>` over a blanket `uv lock --upgrade`, so the
   transitive delta stays attributable to the pins you intended to move.
2. Adjust the pinned versions and rebuild. These live in more than one file, and a skew between
   them is what makes `uv sync --frozen` fail on lock revision rather than at review time:
   - `Dockerfile`: `PYTHON_IMAGE` (tag *and* digest), `PIP_VERSION`, `UV_VERSION`,
     `GLAB_VERSION`, `DEBIAN_SNAPSHOT`.
   - `.gitlab-ci.yml`: the same Python image on every job that runs in a plain Python image,
     plus the `uv` version in the `validate` job. Move every Python reference together with the
     `Dockerfile` -- grep for the outgoing pin rather than counting jobs, and note that
     `.security_scan` is a hidden template -- and keep `uv` identical in both files. Leave
     `compat_python_floor`'s *version* alone: it pins the supported floor, not the runtime. Its
     *codename* does move with the base, and the contract test fails if it does not.
   - `scripts/generate-sbom.sh`: the Syft image and digest, when bumping the scanner.
   - `SECURITY.md`, `README.md`, `docs/CI.md`, `docs/SYSTEM_DESIGN.md`, and the "Supported
     toolchain versions" list above all restate these numbers; grep for the old literals before
     you finish. `README.md` and `docs/AGENT_ONBOARDING.md` state the supported **floor**, so
     they move only when the floor does.
3. Regenerate the SBOM, copy it to `sbom/`, and commit the new artifact. Generate it from the
   image you actually validated, not a fresh rebuild -- see the SBOM section above.
4. Run the automated test suite and linting.
5. Document significant dependency changes in the merge request description.
6. After a `UV_VERSION` or `TOOL_RELEASES` bump merges, expect the pre-tag secrets comparison to
   report lost and new hits. uv's own SBOM makes up most of
   `security/image-secrets-baseline.json`, and the tool checksums sit in
   `scripts/release_tools.py`. MR pipelines build no image, so refresh
   the baseline from the new `main` digest in a follow-up MR, as `docs/RELEASING.md` describes.
   Do not refresh it wholesale without reading the hits.
