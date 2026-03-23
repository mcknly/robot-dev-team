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
- Use `uv` to refresh locks. Inside the running container:
  - `docker compose exec app uv lock --upgrade` resolves both runtime and `dev` extras.
  - Copy the refreshed `uv.lock` back to the host: `docker compose cp app:/work/uv.lock uv.lock`.
- Review upstream changelogs before bumping versions and stage Python updates together with lockfile changes.
- Run `pytest` (or the relevant subset) before committing dependency bumps.
- Target cadence: evaluate monthly for minor updates and apply security fixes as soon as advisories land.

## System packages (APT)
- Base image: `python:3.12.11-slim-bookworm`.
- OS packages are installed via a Debian snapshot pinned by `DEBIAN_SNAPSHOT` in the `Dockerfile`. We track a snapshot no more than one month old (current: `20260315T000000Z`) to balance deterministic rebuilds with timely security fixes.
- To update system packages:
  1. Pick the latest viable snapshot timestamp from <https://snapshot.debian.org> (verify with `curl -I` that the packages exist).
  2. Update `DEBIAN_SNAPSHOT`, rebuild with `docker compose build --no-cache`, and confirm the image installs successfully.
  3. Note the snapshot date in your commit message or MR description to show awareness of CVE coverage.
- Add or update packages in the `apt-get install` list sparingly and keep `--no-install-recommends`.

## Node.js & npm usage
- Node.js is pinned via `NODE_VERSION` in the `Dockerfile`; upgrade path mirrors the Python instructions above.
- Node archives are verified against `SHASUMS256.txt` during the build.
- npm global prefix: `${NPM_CONFIG_PREFIX:-/home/appuser/.npm-global}`.
- Node.js is currently required only for the Gemini CLI, which has no native installer yet. Once Gemini ships a native binary, Node.js can be removed from the image entirely.

### Agent CLI installs
The entrypoint uses a hybrid installation strategy so each CLI follows its vendor's recommended method:

| Agent      | Method                          | Notes                                         |
|------------|---------------------------------|-----------------------------------------------|
| Claude Code | Native installer (`curl \| bash`) | Installs to `~/.local/bin/claude`; npm package is deprecated |
| Codex CLI  | Native binary (GitHub Releases) | Standalone musl binary installed to `~/.local/bin/codex` |
| Gemini CLI | npm (`@google/gemini-cli@latest`) | No native installer available yet              |

All three pull the latest version on every container start. Auto-updates are allowed.

- For air-gapped or flaky-network environments with Gemini, pre-populate the npm cache:
  1. Run `docker compose run --rm app npm pack @google/gemini-cli@latest` while online.
  2. Place the resulting `.tgz` file in the shared cache directory (`npm-cache/`).
  3. Future boots will install from the warmed cache even when the registry is unreachable.
- Record deviations from the latest-tracking policy in this document if operations ever require pinning.

## SBOM generation
- The build installs `syft` (v1.42.2) temporarily and emits `/work/sbom/sbom.spdx.json` that captures the full container filesystem.
- Use `scripts/generate-sbom.sh` to rebuild the image and sync the artifact into `sbom/sbom.spdx.json`. The script removes its temporary container automatically.
- Commit updated SBOMs when dependency footprints change so downstream consumers can audit releases quickly. Consider wiring the script into CI so the artifact refreshes on every release build.

## Supported toolchain versions
- Python: 3.12.x (current image 3.12.11)
- uv: 0.10.10
- FastAPI stack: see pinned versions in `pyproject.toml`
- Node.js: 20.20.1
- GitLab CLI (`glab`): 1.89.0
- Docker Engine: ≥ 24.0 (required for the compose features we rely on)

## Update checklist
1. Create a feature branch and refresh `uv.lock` (and `pyproject.toml` when bumping direct pins).
2. Adjust `Dockerfile` ARG values for Python, Node, glab, or syft as needed and rebuild.
3. Regenerate the SBOM, copy it to `sbom/`, and commit the new artifact.
4. Run the automated test suite and linting.
5. Document significant dependency changes in the merge request description.
