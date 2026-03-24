<!--
Robot Dev Team Project
File: docs/SYSTEM_DESIGN.md
Description: System architecture overview.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# GitLab → LLM Agent Webhook Automation  
**Software System Design Specification (SSDS)**  
**Version:** 1.0.0  
**Status:** Approved for Initial Implementation  
**Date:** 2025-09-20  

---

## 1. Overview

This system is a lightweight, containerized webhook listener and event processor designed to receive **GitLab webhook events** (issues, merge requests, comments, etc.), enrich them with GitLab context (via `glab`), and execute **multiple LLM agent CLI tools** (Claude Code, Google Gemini, OpenAI Codex) based on configurable routing rules.

It is designed for local or self-hosted GitLab setups. GitHub integrations will be explored in future iterations.

---

## 2. Goals & Non-Goals

### Goals
- Listen for HTTP POST requests from GitLab webhooks.
- Parse, authenticate, and deduplicate incoming events.
- Route events dynamically to **one or more agents** based on:
  - Event type (Issue, MR, Note)
  - Action (opened, closed, merged)
  - Optional filters (author, labels)
- Allow route definitions to select any combination of Claude Code, Google Gemini, and OpenAI Codex tasks.
- Enable agents to authenticate with dedicated GitLab accounts and post comments via `glab` after completing their analysis.
- Construct context-rich prompts for each agent using:
  - Webhook JSON
  - Enriched metadata from `glab` (MR diffs, comments, labels)
  - Plain-text prompt templates
- Run multiple agents serially per trigger and log their outputs.
- Package as a lightweight Docker container with:
  - Python backend (FastAPI)
  - Node + NPM-installed CLIs (Claude Code, Gemini, OpenAI Codex)
  - GitLab CLI (`glab`)
  - Configuration via `.env`, bind-mounted config files, and YAML routes.
- Agent-specific CLI configs (`~/.claude`, `~/.gemini`, `~/.codex`) bind-mounted from the host; GitLab CLI state and token files are generated inside the container at startup by `docker-entrypoint.sh`. Git credentials are configured per repository at runtime.
  - Bind-mount placeholders for project repositories tied to incoming webhooks so agents work against live sources.

### Non-Goals
- Persisting data beyond logs (no DB required).
- Guaranteeing LLM completion delivery (focus is trigger + execution).
- Enterprise-scale horizontal scaling (single-instance focus).
- Handling GitHub webhooks or GitHub CLI-driven enrichment (deferred).

---

## 3. System Architecture

### High-Level Diagram

```text
┌────────────┐      ┌─────────────┐      ┌────────────────┐      ┌────────────────┐      ┌─────────────┐
│ GitLab     │ ---> │ Webhook     │ ---> │ Event Router   │ ---> │ Trigger Queue  │ ---> │ Agent Runner│
│ Webhooks   │ POST │ Listener    │      │  (routes.yaml) │      │   (in-memory) │      │  (serial)   │
└────────────┘      └─────────────┘      └────────────────┘      └────────────────┘      └─────────────┘
                                                                                      │
                                                                                      └─────────┬─────────┐
                                                                                                │ Claude CLI Agent │
                                                                                                │ Gemini CLI Agent │
                                                                                                │ Codex CLI Agent  │
                                                                                                └──────────────────┘
```

---

## 4. Technology Stack

| Layer                          | Technology                                                |
| ------------------------------ | --------------------------------------------------------- |
| Web Server                     | FastAPI + Uvicorn                                         |
| Config Management              | `.env` via `pydantic-settings`                            |
| Async Execution                | `asyncio.subprocess`                                      |
| Trigger Scheduling             | In-memory FIFO queue (`TriggerQueue`)                     |
| Agent CLIs                     | `claude` (Node), `gemini` (Node), `codex` (Node) |
| SCM CLIs                       | `glab` (GitLab CLI)                                        |
| Helper Scripts                 | `gitlab-connect` (GitLab issue/MR helper) + `glab-usr` (auth switch) |
| Container Base                 | `python:3.12-slim` + Node.js + NPM                        |
| Logs                           | Persistent volume `./run-logs`                            |
| Prompt Templates               | Plain text via `string.Template`                          |

---

## 5. Event Flow

1. **GitLab** sends webhook → `POST /webhooks/gitlab`.
2. **Auth**: `X-Gitlab-Token` checked vs `GITLAB_WEBHOOK_SECRET`.
3. **Deduplication**: `X-Gitlab-Event-UUID` checked via an in-memory set.
4. **Routing & Trigger Scheduling**:
   - Event name + action matched against `config/routes.yaml` (first matching rule per evaluation).
   - Webhooks with multiple user mentions fan out into per-mention trigger jobs, while still allowing multi-mention rules to run once on the original payload.
   - Trigger jobs are placed on an in-memory FIFO queue so agent execution proceeds in a controlled order even under bursty traffic.
5. **Context Building**:
   - Basic fields extracted: project, title, description, author, URL.
   - Optional enrichment via `glab` to fetch diffs, labels, comments.
   - Results merged into `ctx["extra_context"]`.
6. **Prompt rendering:**
   - Load `/prompts/<task>.txt`.
   - Inject variables: `${PROJECT}`, `${TITLE}`, `${DESCRIPTION}`, `${EXTRA}`, `${JSON}`.
7. **Agent dispatch:**
   - The queue worker executes each trigger sequentially; within a trigger the configured agents also run serially (one at a time).
  - Each agent runs GitLab operations through the `gitlab-connect` wrapper, which sets `CURRENT_AGENT` and calls `glab-usr` to ensure the correct service account is active before invoking `glab`.
   - Capture stdout (final reply) and the thinking stream (stderr) to `/work/run-logs/<uuid>-<project>-<route>-<agent>.out.json`.
8. Respond with HTTP 200 containing aggregated execution results plus a `triggers` array describing each processed mention-specific job (route name, mentions, status, agents).

---

## 6. Configuration

### `.env` Example

```bash
# Web server
APP_HOST=0.0.0.0
APP_PORT=8888
APP_LOG_LEVEL=INFO

# GitLab Webhook
GITLAB_WEBHOOK_SECRET=replace_me
GLAB_HOST=gitlab.example.com
GLAB_TOKEN=glpat-app-token

# Agent GitLab tokens (dedicated service accounts)
# Convention: <AGENT>_AGENT_GITLAB_TOKEN for any agent name
CLAUDE_AGENT_GITLAB_TOKEN=replace_me
GEMINI_AGENT_GITLAB_TOKEN=replace_me
CODEX_AGENT_GITLAB_TOKEN=replace_me

# Agent model identifiers (referenced via ${<AGENT>_MODEL} in routes.yaml)
CLAUDE_MODEL=claude-opus-4-6
GEMINI_MODEL=gemini-3.1-pro-preview
CODEX_MODEL=gpt-5.4

# Agent credential paths (bind-mounted into the container)
CLAUDE_CONFIG_PATH=~/.claude
GEMINI_CONFIG_PATH=~/.gemini
CODEX_CONFIG_PATH=~/.codex

# Paths
ROUTE_CONFIG_PATH=config/routes.yaml
PROMPT_DIR=prompts
RUN_LOGS_DIR=run-logs

# Timeouts
GLAB_TIMEOUT_SECONDS=30
AGENT_MAX_WALL_CLOCK_SECONDS=7200
AGENT_MAX_INACTIVITY_SECONDS=900

# Feature toggles
ENABLE_AUTO_CLONE=false
ENABLE_BRANCH_SWITCH=false
ENABLE_SMART_BRANCH_SELECTION=true
ENABLE_AUTO_UNASSIGN=false
MENTION_HOLD_SECONDS=3.0
LIVE_DASHBOARD_ENABLED=false
DEBUG_RELOAD_ROUTES=false

# Log and branch pruning
LOG_PRUNING_ENABLED=true
LOG_RETENTION_DAYS=7
BRANCH_PRUNING_ENABLED=false

# Container UID/GID remapping
LOCAL_UID=1000
LOCAL_GID=1000
```

The container entrypoint uses convention-based token-file generation: any environment variable matching `*_AGENT_GITLAB_TOKEN` is mirrored into `~/.<agent>/glab-token` (mode `0600`) so `glab-usr` can authenticate even when downstream agent sandboxes scrub environment variables. Each agent's system prompt instructs it to call `glab-usr <agent-name>` before posting comments, ensuring the correct service account is active in `glab`. See `docs/ENVIRONMENT.md` for the full variable reference.

---

### `config/routes.yaml`

```yaml
routes:
  - name: issue-triage
    access: readonly
    match:
      event: "Issue Hook"
      action: "open"
      labels: ["LLM"]
    agents:
      - agent: claude
        task: triage
      - agent: gemini
        task: reply_suggestion

  - name: mr-review
    access: readonly
    match:
      event: "Merge Request Hook"
      action: "open"
      author: "your-username"
    agents:
      - agent: claude
        task: merge_request_review
        options:
          args: ["-p", "--model", "${CLAUDE_MODEL}"]

  - name: claude-mentioned
    access: readonly
    match:
      event: "Note Hook"
      action: "comment"
      mentions: ["claude"]
    agents:
      - agent: claude
        task: note_followup

  - name: assign-issue-claude
    access: readwrite
    match:
      event: "Issue Hook"
      action: "update"
      assignees: ["claude"]
    agents:
      - agent: claude
        task: assign_work
```

Each `agents` list can mix and match any CLI. The server executes agents serially within each trigger, so adding or removing an agent is a purely declarative change within `routes.yaml`. Routes use an `access` field (`readonly` or `readwrite`) to control which project mount the agent receives. See `docs/ROUTES.md` for the full schema reference.

Routes that specify exactly one `mentions` value are evaluated independently for each user mention when a webhook references multiple usernames. Rules that omit `mentions` or list several names continue to run in the standard first-match order against the original payload.

### GitLab helper scripts

- `gitlab-connect` resides at `/usr/local/bin/gitlab-connect` and provides a single entrypoint for issue/MR workflows (create, edit, comment, view). It reads `CURRENT_AGENT` (set by the webhook router) or `--agent` and automatically authenticates before delegating to `glab`.
- Authentication is handled by `/usr/local/bin/glab-usr`, which accepts any logical agent name and uses convention-based env var resolution (`<AGENT>_AGENT_GITLAB_TOKEN`, `<AGENT>_AGENT_GIT_*`) to run `glab auth login` with the correct personal access token. Any agent name works without code changes. When the requested account is already active the script becomes a no-op to avoid redundant logins.

### Prompt templates (example)

`prompts/mr_summary.txt`:
```text
You are a code review summarizer.

Project: ${PROJECT}
MR Title: ${TITLE}
Author: ${AUTHOR}
URL: ${URL}

Description:
${DESCRIPTION}

### Additional Context
${EXTRA}

Use the context above to summarize changes, list risky files, and generate a reviewer checklist.
Raw JSON for reference:
${JSON}
```

---

## 7. Containerization

### `Dockerfile`

The image is based on `python:3.12-slim` with Node.js, npm, Git, and the GitLab CLI (`glab`) installed at build time. Key build-time steps:

- Copies `gitlab-connect` and `glab-usr` helper scripts to `/usr/local/bin/`.
- Installs Python dependencies via `uv` from `pyproject.toml`.
- Creates a non-root `appuser` (UID 10001) that is remapped at runtime.
- Uses `tini` as PID 1 and delegates to `docker-entrypoint.sh`.

For local testing outside containers, the repository provides `./launch-uvicorn-dev`, which sources `.env` plus the compose environment defaults before starting `uvicorn` from `.venv` with reload enabled.

---

### `docker-entrypoint.sh`

The entrypoint performs the following steps at container start:

1. **UID/GID remapping** — When running as root (the initial Docker user), remaps the `appuser` account to match `LOCAL_UID`/`LOCAL_GID` from the environment, then re-executes itself via `gosu` as the unprivileged user. This ensures bind-mounted credential directories remain accessible.
2. **Convention-based token-file generation** — Scans environment variables for any `*_AGENT_GITLAB_TOKEN` pattern and writes the value to `~/.<agent>/glab-token` (mode `0600`). The agent directory name is the lowercase, hyphen-separated form of the prefix (e.g., `QWEN_CODE_AGENT_GITLAB_TOKEN` writes to `~/.qwen-code/glab-token`). This supports arbitrary agent names without code changes.
3. **npm cache setup** — Validates the configured cache directory is writable, falling back to `/tmp/npm-cache` if not.
4. **Agent CLI installation** — Discovers and runs all `scripts/install-*.sh` scripts. To add a new agent CLI, drop an installer script into `scripts/` (see `docs/ADDING_AN_AGENT.md`).
5. **Application launch** — Executes the CMD (`uvicorn app.main:app ...`).

---

### `docker-compose.yml`

The Compose file defines a single `app` service with the following key bindings:

- **Prompts and config** — `./prompts` and `./config` mounted read-only.
- **Run logs and npm cache** — `./run-logs` and `./npm-cache` mounted read-write.
- **Agent credential directories** — `~/.claude`, `~/.gemini`, `~/.codex` (configurable via `*_CONFIG_PATH` env vars) bind-mounted so CLIs reuse host authentication.
- **Project repositories** — A parent `./projects` directory is mounted twice:
  - `/work/projects` (read-write) for work routes with `access: readwrite`.
  - `/work/projects-ro` (read-only) for analysis routes with `access: readonly`.
- **UID/GID remapping** — `LOCAL_UID` / `LOCAL_GID` (from `.env`, defaulting to 1000) are passed to the entrypoint so `appuser` is remapped to the host identity.

Agent-specific configuration directories remain mounted so the container can read the `glab-token` mirrors seeded from environment variables. GitLab CLI state is generated inside the container on startup, so no host-side bind is required for `glab`.

For additional per-machine customizations, create `docker-compose.override.yml` (git-ignored). Compose merges the override automatically.

---

## 8. Security

- Validate all webhook requests against the shared secret token.
- Restrict command execution to a safe allowlist (`claude`, `gemini`, `codex`, `gitlab-connect`, `glab`, `glab-usr`, `python`).
- Enforce per-command timeout limits.
- Run the container as a non-root `appuser`.
- Mount configuration, prompt directories, and agent CLI token directories; these mounts remain writable so the entrypoint can refresh `glab-token` mirrors.
- Store GitLab personal access tokens as environment secrets per agent and scope each token to comment-only permissions wherever possible.

---

## 9. Logging & Observability

### Log Format

All application modules emit structured logs to stdout using this format:

```
%(asctime)s | %(levelname)s | %(name)s | %(message)s
```

Example output:

```
2026-03-17 14:32:45,123 | INFO | app.api.webhooks | Webhook processing complete
2026-03-17 14:32:45,456 | DEBUG | app.services.routes | Pattern matched
```

The four fields are:
- **asctime** -- timestamp in `YYYY-MM-DD HH:MM:SS,mmm` format from the Python logging formatter
- **levelname** -- standard Python level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`)
- **name** -- the Python module path (see key logger names below)
- **message** -- human-readable event description, often followed by `extra={}` context on the same line

### Key Logger Names

| Logger | Module | What it logs |
| --- | --- | --- |
| `app.api.webhooks` | Webhook endpoint | Header processing, route matching results, suppression of echo webhooks, auto-unassign agent detection |
| `app.services.routes` | Route engine | Pattern match decisions during rule evaluation |
| `app.services.agents` | Agent runner | Working directory resolution, agent lifecycle (start, completion, timeout, kill), token resolution errors |
| `app.services.branch_resolver` | Branch resolution | Remote default-branch queries, MR lookup for issues, smart branch selection ranking, checkout failures and backup creation |
| `app.services.branch_pruning` | Branch pruning | Pruning cycle start/stop, per-branch age analysis, dry-run vs live actions, git command failures |
| `app.services.glab` | GitLab CLI calls | glab enrichment output parsing, command failures and timeouts |
| `app.services.context_builder` | Context enrichment | Issue data fetch failures during enrichment |
| `app.services.trigger_queue` | Trigger queue | Dispatch events, mention hold suppression, promotion of held items |
| `app.services.project_paths` | Project resolution | Path resolution, auto-clone progress, git repository validation |
| `app.services.log_pruning` | Log pruning | Pruning cycle start/stop, retention policy enforcement, deletion summaries |

### Log Level Behavior

`APP_LOG_LEVEL` (default `INFO`) sets the root logger threshold. All loggers listed above inherit this level. See `docs/ENVIRONMENT.md` for a detailed guide on what each level reveals and recommended settings for common debugging scenarios.

### Dashboard Log Mirroring

When `LIVE_DASHBOARD_ENABLED=true`, the application adds a `DashboardLogHandler` to the root logger at startup. This handler mirrors every log record that passes the `APP_LOG_LEVEL` threshold to connected WebSocket clients on the `/dashboard` endpoint. A small number of dashboard-originated events (such as kill-switch notifications) are published directly via `dashboard_manager.publish_system()` and appear regardless of `APP_LOG_LEVEL`.

Dashboard system-log payloads include three fields: `message` (the formatted log text), `level` (e.g. `INFO`), and `logger` (the module name). The full console format string (`asctime | levelname | name | message`) is **not** replicated -- operators viewing container logs versus the dashboard should expect the same events but different presentation.

Agent stdout, stderr (thinking stream), and prompt content are streamed to the dashboard through a separate mechanism and are **not** controlled by `APP_LOG_LEVEL`. Lowering the log level increases the volume of system-log entries on the dashboard but does not affect agent output streaming.

### `DEBUG_RELOAD_ROUTES` Relationship

`DEBUG_RELOAD_ROUTES` is an independent feature toggle that enables hot-reload for `config/routes.yaml`. It is **not** activated by setting `APP_LOG_LEVEL=DEBUG`. The two settings are orthogonal:

- `DEBUG_RELOAD_ROUTES=true` watches the routes file for changes and reloads rules without a server restart, useful during development.
- `APP_LOG_LEVEL=DEBUG` increases log verbosity across all modules, making route-reload events easier to trace when both are enabled together.

### Troubleshooting with Logs

| Scenario | Recommended level | What to look for |
| --- | --- | --- |
| Webhook not triggering any route | `DEBUG` | `app.services.routes` -- pattern match decisions showing why no rule matched |
| Agent not dispatching after route match | `DEBUG` | `app.services.agents` -- working directory resolution, token errors, prompt rendering failures |
| Wrong branch checked out | `DEBUG` | `app.services.branch_resolver` -- remote query results, MR candidate ranking, checkout fallback reasons |
| Auto-unassign not firing | `DEBUG` | `app.api.webhooks` -- assignee detection; `app.services.agents` -- completion status and return code |
| glab enrichment missing data | `DEBUG` | `app.services.glab` -- JSON parse skip reasons, command output details |
| Trigger queue delays | `INFO` | `app.services.trigger_queue` -- dispatch timing, hold/suppression events |

### Run Logs

Each agent execution is captured as a structured JSON file under the configured `RUN_LOGS_DIR` (default `/work/run-logs/`):

```
<event_uuid>-<project>-<route>-<agent>.out.json
```

These files contain the rendered prompt, stdout, stderr (thinking stream), return code, and branch context. Run logs are independent of the Python logging system and are always written regardless of `APP_LOG_LEVEL`.

If the configured log directory is unwritable, the agent runner falls back to a temporary directory (`<system-tmpdir>/run-logs/`). A warning is emitted to the application log when this occurs. Note that the automatic pruning service (`LOG_PRUNING_ENABLED` / `LOG_RETENTION_DAYS`) only manages files in the configured `RUN_LOGS_DIR` -- fallback files must be cleaned up manually.

### Health Check

`GET /health` returns `{"status": "ok"}` for container liveness probes.

---

## 10. Testing

- Unit tests validate webhook parsing, routing rules, and prompt rendering.
- Integration tests mock `glab` outputs and agent subprocesses to verify the enrichment pipeline, agent dispatch, branch resolution, log pruning, and dashboard behavior.
- Tests are run with `pytest` under `tests/`. No end-to-end webhook tests exist yet; coverage focuses on unit and integration levels with mocked external calls.

---

## 11. First Release Scope

- ✅ Webhook server with token auth & deduplication
- ✅ YAML-based routing with conditions
- ✅ Prompt rendering with Markdown extra context
- ✅ Multi-agent execution with serial asyncio dispatch
- ✅ Node-based agent CLIs installed at runtime
- ✅ GitLab CLI enrichment support
- ✅ Dockerfile + Docker Compose for reproducible deployment
- ✅ Logging and simple health check
- ✅ `gitlab-connect` helper for switching dedicated agent accounts and handling issue/MR workflows

---

## 12. Future Enhancements

- UI or CLI tool to view logs and event history.
- Rate limiting and backoff for large event storms.
- Direct Git push hooks to enrich with diff hunks inline.
- Integration with other LLM providers or on-prem models.
- GitHub webhook handling and GitHub CLI-driven enrichment.

---

**End of Document**
