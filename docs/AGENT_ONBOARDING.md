<!--
Robot Dev Team Project
File: docs/AGENT_ONBOARDING.md
Description: Onboarding checklist for automation agents.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Agent Onboarding Guide

This guide walks human and LLM agents through first-time setup on Linux and on Windows using WSL. It assumes you have cloned this repository and reviewed `AGENTS.md` for general practices.

## Prerequisites

- Git 2.40+ and Python 3.12+
- Docker Engine or Docker Desktop (with Compose v2)
- Node.js 18+ and npm (required for Gemini CLI; Claude and Codex use native installers)
- Access tokens for the Claude, Gemini, and Codex GitLab accounts
- `glab` CLI installed on the host

## How Agent CLI Authentication Works

The agent CLIs (Claude Code, Gemini CLI, Codex CLI) authenticate to their respective LLM providers using the host user's personal account credentials. When you authenticate each CLI during setup, the credentials stored in `~/.claude`, `~/.gemini`, and `~/.codex` are bind-mounted into the Docker container at runtime. This means:

- Agent operations are billed to your existing subscription plan, not to a separate API account.
- Your plan's rate limits and spending controls apply automatically.
- No separate API keys need to be generated or managed for LLM access.

GitLab authentication is separate and uses per-agent Personal Access Tokens (PATs) configured in `.env`.

## Linux Setup

1. **Create a virtual environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install uv
   uv pip install --editable .[dev]
   ```
2. **Configure environment**
   ```bash
   cp .env.example .env
   # populate secrets and per-host overrides
   ```
   See `docs/ENVIRONMENT.md` for the full variable reference.
3. **Authenticate GitLab CLIs**
   - Run `glab auth login` to set up personal access tokens.
   - Populate `CLAUDE_AGENT_GITLAB_TOKEN`, `GEMINI_AGENT_GITLAB_TOKEN`, and `CODEX_AGENT_GITLAB_TOKEN` in `.env`.
   - For multi-project setups, add agent accounts as **Developer** members of your GitLab Group so access is inherited automatically. See `docs/GROUP_SETUP.md`.
4. **Start the service**
   ```bash
   ./launch-uvicorn-dev
   ```
   Auto-reload keeps the server in sync with source changes.
5. **Run smoke tests (when available)**
   ```bash
   uv run pytest
   ```

## Windows Setup (WSL2)

1. **Install prerequisites**
   - Enable WSL and install an Ubuntu distribution (`wsl --install -d Ubuntu`).
   - Install Docker Desktop for Windows and enable WSL integration for the Ubuntu distribution.
   - Install Git for Windows (optional but helpful for credential helpers).
2. **Clone the repository inside WSL**
   ```bash
   wsl
   mkdir -p ~/workspace && cd ~/workspace
   git clone https://<gitlab-host>/<namespace>/robot-dev-team.git
   cd robot-dev-team
   ```
   Avoid cloning into the Windows filesystem (`/mnt/c/...`) to minimize I/O overhead.
3. **Follow the Linux setup steps inside WSL**
   - Use the same virtual environment workflow.
   - Store credentials under the WSL home directory or point mount paths to Windows locations using `/mnt/c/...`.
4. **Docker considerations**
   - Docker Desktop shares the WSL filesystem automatically. For Windows-resident credentials, ensure the drive is shared in Docker Desktop settings and update `docker-compose.override.yml` accordingly.
5. **Launching the stack**
   ```bash
   ./launch-uvicorn-dev      # local development
   # or
   docker compose up --build # containerized
   ```

## Agent CLI Checklist

1. Verify the agent CLIs are available on `PATH`:
   ```bash
   which claude
   which gemini
   which codex
   ```
2. Run `glab-usr <agent>` (or rely on `gitlab-connect`) to confirm authentication for each agent user.
3. When using `gitlab-connect`, agent identity can be set in several ways (checked in order): the `CURRENT_AGENT` environment variable, `ROBOT_AGENT_NAME`, or the currently active `glab` user. Explicitly setting `CURRENT_AGENT` is one way to force a specific identity:
   ```bash
   export CURRENT_AGENT=codex
   gitlab-connect issue view 1
   ```
4. Review `docs/GITLAB_WEBHOOKS.md` to ensure local GitLab projects route events correctly. For automatic webhook provisioning on new projects, see `docs/GROUP_SETUP.md`.

## First-Run Validation

- Send a test webhook from GitLab (`Test` button) and confirm:
  - Webhook is accepted (`200 OK`).
  - A trigger appears in `run-logs/`.
  - Live dashboard (if enabled) shows the event stream.
- Inspect `config/routes.yaml` to ensure incoming events map to the expected agents. The shipped file uses `your-username` as a placeholder in the `author` match fields. For local use, copy it to `config/routes.local.yaml`, replace `your-username` with your GitLab username, and set `ROUTE_CONFIG_PATH=config/routes.local.yaml` in your `.env`.
- Document any environment-specific adjustments for future agents in the project memory file (e.g., `CLAUDE.md` / `AGENTS.md`).
