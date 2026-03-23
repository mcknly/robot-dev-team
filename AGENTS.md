<!--
Robot Dev Team Project
File: AGENTS.md
Description: Automation agent operations guide.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Automation Agent Guide

This repository is frequently operated by LLM-based agents. Use the guidelines below to stay aligned with the system design and avoid disruptive changes.

## Mission Summary
- Maintain the FastAPI webhook service that routes GitLab events to local agent CLIs.
- Ensure configuration transparency: rely on bind-mounted host configs instead of duplicating credentials.
- Preserve container reproducibility (Dockerfile, docker-entrypoint, docker-compose) while keeping developer ergonomics strong.
- Support mention-trigger splitting so multi-mention webhooks enqueue one job per mentioned user.

## Environment Expectations
- Python 3.12+ with `uv` available for dependency management.
- Node.js + npm for Gemini CLI (Claude and Codex use native installers).
- GitLab CLI (`glab`) accessible; tokens provided via environment.
- Agents run inside Docker by default, but local execution should always work.
- Windows operators should work inside WSL2 with Docker Desktop integration (see `docs/AGENT_ONBOARDING.md`).

## Setup Commands
```bash
python -m venv .venv
source .venv/bin/activate
pip install uv
uv pip install --editable .[dev]  # includes pytest/httpx when tests are added
```

Start the API with:
```bash
./launch-uvicorn-dev
```
The helper loads `.env` plus compose environment defaults before launching `uvicorn` from `.venv` with auto-reload enabled, so local runs mirror container settings.

Launch the full stack via Docker Compose:
```bash
docker compose up --build
```

## Configuration Notes
- Copy `.env.example` to `.env`; keep secrets out of version control. Reference `docs/ENVIRONMENT.md` for variable descriptions and compose override patterns.
- Keep `config/routes.yaml` rules deterministic and minimal—first match wins for each evaluation (multi-mention events run the resolver separately per mention).
- Prompt templates live in `prompts/`; prefer plain text with `${VARIABLE}` substitutions.
- Project repositories are mounted via `docker-compose.yml` (edit the volume paths to match your local directory structure). Projects should be organized as `<namespace>/<project-name>` to mirror GitLab's path structure.
- Enable `ENABLE_AUTO_CLONE=true` to automatically clone projects on first webhook trigger.
- Use `docs/GITLAB_WEBHOOKS.md` when guiding users through webhook setup or troubleshooting delivery issues.

## Testing Guidance
- Add or update pytest suites under `tests/`. Mock `glab` calls and agent subprocesses to keep tests fast and deterministic.
- Run `pytest` before committing when tests exist.
- If introducing new dependencies, update `pyproject.toml` and regenerate lockfiles if applicable (currently using editable installs via `uv`).
- **Virtual environment portability**: The `.venv` may have been created inside a Docker container, causing script shebangs to reference `/work/projects/...` paths that don't exist on the host. If `pytest` fails with "file not found" or similar path errors, recreate the venv:
  ```bash
  rm -rf .venv
  python -m venv .venv
  source .venv/bin/activate
  pip install uv
  uv pip install --editable .[dev]
  ```
  Alternatively, create a temporary venv elsewhere (e.g., `/tmp/test-venv`) to avoid modifying the repository's `.venv`.

## Coding Standards
- Follow the ASCII-only policy unless files already include Unicode.
- Add concise comments only when necessary to orient the reader around non-trivial logic.
- Keep logging structured and avoid leaking secrets (tokens, secrets in payloads).

## Git Practices
- Commit related changes together with descriptive messages.
- Never revert user-authored changes without explicit instruction.
- If the working tree appears to change unexpectedly, stop and seek guidance.

## Helpful Files
- `docs/SYSTEM_DESIGN.md` — canonical reference for architecture and container expectations.
- `docker-entrypoint.sh` — controls runtime installation of agent CLIs via `scripts/install-*.sh`.
- `gitlab-connect` — unified CLI wrapper that authenticates via `glab-usr` and provides issue/MR create, edit, comment, and view helpers.
- `glab-usr` — retains the low-level authentication switch used by `gitlab-connect`. Uses convention-based env var resolution (`<AGENT>_AGENT_GITLAB_TOKEN`, `<AGENT>_AGENT_GIT_*`) so any agent name works without code changes.
- `docs/ADDING_AN_AGENT.md` — step-by-step guide for onboarding a custom agent CLI (BYOA).
- `docs/DEPENDENCY_MANAGEMENT.md` — dependency pinning policy, update cadence, SBOM workflow, and supported toolchain versions.
- `docs/ENVIRONMENT.md` — environment variable reference and docker-compose override examples.
- `docs/AGENT_ONBOARDING.md` — Linux and Windows (WSL) onboarding checklists for agents.
- `docs/GITLAB_WEBHOOKS.md` — webhook configuration workflow and troubleshooting tips.
- `docs/GROUP_SETUP.md` — GitLab group setup with automatic webhook provisioning via File Hooks.
- `gitlab/file_hooks/add_webhooks.rb` — File Hook script for automatic webhook creation on new projects.
- `gitlab/docker-compose.gitlab.yml` — example Docker Compose for GitLab CE with File Hook support.
- `gitlab/readme-gitlab.md` — GitLab CE deployment guidance and upgrade path instructions.
- `docs/DASHBOARD_GUIDE.md` — dashboard usage and kill-switch behaviour.
- `docs/ADD_NEW_PROJECT.md` — checklist for adding a new project to an existing deployment.
- `docs/CONTRIBUTING.md` — issue workflow, branch strategy, and MR expectations.
- `docs/CHANGELOG.md` — release history snapshot for the public track.
- `scripts/header_guard.py` — quick check to ensure headers stay compliant with the MIT licensing policy.
- `fs.inotify.*` sysctls — Claude’s dev server keeps a reload watcher running; if you see `Too many open files`, raise `fs.inotify.max_user_watches` / `fs.inotify.max_user_instances` on the host (e.g., `sudo sysctl fs.inotify.max_user_watches=262144 fs.inotify.max_user_instances=512`) before restarting the container.

## Troubleshooting Tips
- Verify webhook authentication by comparing `X-Gitlab-Token` to `GITLAB_WEBHOOK_SECRET`.
- Inspect `run-logs/` for captured prompts and agent outputs when debugging failures (multi-mention events log once per mention trigger using the derived event id).
- Use `docker compose logs app` to review runtime logs in containerized deployments; queued triggers are processed sequentially by the in-memory worker.
- When `glab` commands fail, run `glab auth status` inside the container to confirm tokens.

Stay aligned with these conventions to keep the automation pipeline predictable for both humans and agents.
