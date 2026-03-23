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

## Container Hardening

- **Base image** is pinned to `python:3.12.6-slim-bullseye` to reduce drift. The build also pins `pip`, `uv`, and the GitLab CLI version (`GLAB_VERSION` build arg).
- **Runtime user**: `docker-entrypoint.sh` drops privileges to the `appuser` account by default. When mapping host UIDs/GIDs, set the `LOCAL_UID`/`LOCAL_GID` environment variables or run the container with `--user`.
- **Minimal packages**: only required OS packages (curl, git, nodejs/npm for agent tooling, tini) remain. `apt` caches are removed after install.
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
- The container never extracts or logs LLM provider tokens; authentication is delegated entirely to the CLI binaries.
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

For coordinated vulnerability disclosure, file a [confidential issue](https://docs.gitlab.com/ee/user/project/issues/confidential_issues.html) in this project's GitLab repository.
