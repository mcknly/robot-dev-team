<!--
Robot Dev Team Project
File: SECURITY.md
Description: Security policy for the Robot Dev Team Project.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Security Guidance

This project processes authenticated GitLab webhooks and dispatches agent automation. The following guidance summarizes the expected security posture prior to a public release and highlights the controls implemented in this audit.

## Reporting a Vulnerability

**Report privately through GitHub Private Vulnerability Reporting:**
<https://github.com/mcknly/robot-dev-team/security/advisories/new> (repository **Security** tab, then **Report a vulnerability**).

That is the only published reporting channel. No security email address is published, so please do not look for one, and **do not open a public issue or pull request for a suspected vulnerability** -- a public report discloses the finding to everyone before a fix exists. A private report stays visible only to the maintainers until an advisory is published.

Helpful to include, where you have it:

- The released version or, better, the image digest you are running (`docker inspect --format '{{index .RepoDigests 0}}' <image>`).
- Which component is affected: the webhook listener, the router, the `gitlab-connect` / `glab-usr` wrappers, an agent harness installer, the container image, or the CI/release path.
- What an attacker gains, and what access they need to start from. This project is normally deployed behind a trusted network boundary with a shared webhook secret, so reachability matters to triage.
- Reproduction steps or a proof of concept. Please redact tokens, webhook secrets, and run-log payloads.

What happens next: the report is acknowledged, and a confirmed fix is developed, reviewed, and qualified on the private canonical instance -- it runs the same test, SBOM, and vulnerability gates as any other change (see [the mirror policy](docs/MIRRORING.md) for why that work is not public). The fix ships in a normal stable release, and the advisory is published from the public repository. Users who pin by digest should expect to move to the new digest; see [the release contract](docs/RELEASING.md).

## Maintainer Reporting Path

This section does not apply to external reporters -- it describes a path only people who can already reach the canonical instance can use, and the section above is the whole of the published channel.

Maintainers and agents with access to the canonical GitLab instance should file a confidential issue there instead, which keeps the report next to the code and the pipeline evidence from the start. [The mirror policy](docs/MIRRORING.md) describes the relationship between the two instances.

## Container Hardening

- **Base image** is pinned to `python:3.14.7-slim-trixie` (Debian 13), by both readable tag and OCI index digest, to reduce drift. The build also pins `pip`, `uv`, and the GitLab CLI version (`GLAB_VERSION` build arg), and installs OS packages from a `DEBIAN_SNAPSHOT`-pinned Debian archive so rebuilds are reproducible. That pin is enforced, not merely configured: the base image's own live-mirror APT source is cleared before `apt-get update`, and the build then asserts from `apt-get indextargets` that every active Debian index is under `snapshot.debian.org`, so no package can enter the image from outside the frozen archive. **That guarantee is scoped to the shipped image and deliberately does not cover the CI toolchain** (#65): the `validate`, `compat_python_floor`, `release_contract`, and `github_release_publish` jobs run digest-pinned Python images but install `git` from the live Debian mirrors at job time. None of the four contributes a package or a layer to the shipped image, so nothing they install can reach it. That is the whole of the claim, and it is deliberately narrower than "produces nothing published": `release_contract` does declare a release artifact (`artifacts/release-context.json`, consumed by `release_publish` and `release_yank`), and on the yank path the annotated tag message it reads through `git` is uploaded as durable release metadata; `github_release_publish` uses `git` to write the public release commit and tag, whose tree is the canonical tagged tree byte for byte. `git`'s role there is validation, reading an annotation, and writing commit and tag objects over an already-fixed tree, never package contents, which is why the snapshot guarantee is about the image and not about those jobs. Each of the four logs `git --version` so the build that ran stays recoverable from the job log. See `docs/CI.md`.
- **Runtime user**: `docker-entrypoint.sh` drops privileges to the `appuser` account by default, using `setpriv` from `util-linux`. When mapping host UIDs/GIDs, set the `LOCAL_UID`/`LOCAL_GID` environment variables or run the container with `--user`. The drop is asserted in CI against the *served* process — real, effective, saved-set and filesystem ids, supplementary groups, and an empty inheritable capability set — because the image carries no `USER` directive and so `docker exec <container> id -u` reports the exec's own root rather than the application's identity. Two limits on the capability claim, both deliberate. The inheritable set is cleared, but `--no-new-privs` and a cleared bounding set are **not** applied, since they would change the capability contract inherited by every agent subprocess. And the `CapInh` assertion witnesses the post-drop capability state rather than the `--inh-caps=-all` flag itself: a uid change with no file capabilities and no ambient set empties that set anyway, so the assertion reads clean with or without the flag, and **nothing in CI would catch its removal**.
- **Minimal packages**: only required OS packages (`bash`, `bzip2`, `ca-certificates`, `curl`, `git`, `tini`, `util-linux`, `xz-utils`) are installed on top of the base image, with `--no-install-recommends`. The build also runs `apt-get upgrade` under the pinned snapshot so preinstalled base-image packages can carry security fixes the base image predates; because `upgrade` silently holds back any update needing a new dependency, the build fails rather than continuing when that happens. Agent CLIs are installed at container start via vendor-native installers, so no Node.js / npm runtime is present in the image. `apt` caches are removed after install.
- **Logs directory**: application writes to `/work/run-logs` (bind mount recommended). Fallback log directory uses the system temp directory rather than hard-coded `/tmp`.

### Recommended Runtime Flags

When deploying, prefer:

```bash
docker run \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp \
  --user $(id -u):$(id -g) \
  robot-dev-team-app
```

Adjust `tmpfs` and writable volumes as needed for prompts/config/logs.

## Dependency & Supply-Chain Hygiene

- Python dependencies are pinned in `pyproject.toml`.
- `pip install` is version locked; `uv` installs pinned dev tooling.
- Add `pip-audit` or `safety` to CI (tracked in issue #14) to continuously scan for vulnerabilities.
- Generate an SBOM (e.g., `syft packages docker:robot-dev-team-app`) for release artifacts.

## Secrets & Credentials

- Webhook requests must provide the `X-Gitlab-Token` shared secret; set `GITLAB_WEBHOOK_SECRET` via environment.
- Agent PATs are read from `CLAUDE/GEMINI/CODEX_AGENT_GITLAB_TOKEN` environment variables and are not logged. Ensure these are injected via secret stores (Docker secrets, Kubernetes secrets, etc.).
- Git credential rotation is handled by `glab-usr`; tokens are written to the container-local credential store and reconfigured on each authentication.
- Never commit `.env` files or run-log payloads containing sensitive data.

### LLM Provider Credentials

Agent CLIs authenticate to LLM providers using the host user's personal account credentials, which are bind-mounted from `~/.claude`, `~/.gemini`, and `~/.codex` into the container. These directories are mounted read-write because the entrypoint also writes `glab-token` files into them for GitLab CLI authentication.

- Restrict host directory permissions (e.g., `chmod 700 ~/.claude ~/.gemini ~/.codex`) to prevent unauthorized access.
- The container never extracts or logs LLM provider tokens; authentication is delegated entirely to the CLI binaries. The Gemini preflight in `app/services/agents.py` only checks file presence and JSON parseability for `~/.gemini/antigravity-cli/antigravity-oauth-token` -- it never reads or logs the token value.
- The Antigravity OAuth token lives in `~/.gemini/antigravity-cli/antigravity-oauth-token` on the host (mode 0600 by `agy`). Because that file is inside a read-write bind mount, the container can rewrite it during token refresh. This is intentional -- refreshed credentials need to persist on the host so future container starts inherit them -- but it does mean a compromised container could overwrite the host-side credential. The bind mount is no more privileged than the existing `~/.claude` / `~/.codex` mounts in this respect.
- If using a shared or multi-user host, consider pointing `*_CONFIG_PATH` variables to dedicated directories with restricted ownership rather than mounting personal home directories.

## Network & TLS

- The application listens on `127.0.0.1` by default. In container environments, uvicorn binds to `0.0.0.0` via the command arguments. Prefer terminating TLS at a trusted reverse proxy (nginx, Traefik) and forwarding to the container over an internal network.
- Enforce HTTPS externally, enable HSTS, and configure mutual TLS where possible for webhook ingestion.
- Apply rate limiting and IP allowlists in the reverse proxy to mitigate brute-force or replay attempts.

## Logging & Monitoring

- Review logs for potential secret leakage before enabling central aggregation.
- Instrument Falco/Trivy runtime scanning and OWASP ZAP DAST (tracked in issue #17).
- Enable webhook replay detection/deduplication (existing `_DEDUP` service covers UUIDs) and monitor for repeated failures.

## Deployment Checklist

1. Configure `GITLAB_WEBHOOK_SECRET` and agent tokens via a secret manager.
2. Front the service with TLS termination, rate limiting, and IP filtering.
3. Run `bandit -r app`, `pip-audit`, and `trivy image` during CI.
4. Regenerate SBOMs for each release artifact.
5. Verify container runs with dropped privileges and minimal writable paths.
6. Review `run-logs/` directory and rotate tokens on incident.

For coordinated vulnerability disclosure, see [Reporting a Vulnerability](#reporting-a-vulnerability) above.
