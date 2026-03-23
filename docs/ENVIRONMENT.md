<!--
Robot Dev Team Project
File: docs/ENVIRONMENT.md
Description: Environment variable reference and configuration notes.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Environment Configuration

Use this guide to configure the FastAPI webhook service for local development, containerized deployments, and CI scenarios. The application loads settings from environment variables (optionally via `.env`) and derives defaults that match the Docker Compose stack.

## How to Configure

1. Copy `.env.example` to `.env`.
2. Populate required secrets (GitLab webhook token and agent PATs).
3. Adjust optional overrides to fit your workspace or deployment target.
4. Recreate or reload the service so the new values take effect.

For container runs, Compose pulls the same `.env` file and binds host credential paths through environment-variable substitutions.

## Variable Reference

### Application Settings
| Variable | Description | Required | Default |
| --- | --- | --- | --- |
| `APP_NAME` | Display name used in logs. | No | `Robot Dev Team Webhook Listener` |
| `APP_HOST` | Interface FastAPI binds to. | No | `127.0.0.1` |
| `APP_PORT` | Exposed port for HTTP traffic. | No | `8080` |
| `APP_LOG_LEVEL` | Log verbosity (see level guide below). | No | `INFO` |
| `GITLAB_WEBHOOK_SECRET` | Shared token validated against `X-Gitlab-Token`. Leave empty to disable verification (not recommended). | Yes | _(none)_ |
| `GLAB_HOST` | GitLab instance hostname used by `glab-usr` and `gitlab-connect`. | Yes | `gitlab.com` |
| `GLAB_TOKEN` | GitLab PAT for the app process. Used for enrichment (fetching issue/MR context) and auto-unassign operations. Requires `api` scope when `ENABLE_AUTO_UNASSIGN` is enabled; `read_api` is sufficient if auto-unassign is disabled. | No | _(none)_ |
| `GLAB_TIMEOUT_SECONDS` | Timeout for GitLab CLI enrichment calls. | No | `30` |
| `AGENT_MAX_WALL_CLOCK_SECONDS` | Hard upper-bound run duration for each agent CLI invocation. | No | `7200` |
| `AGENT_MAX_INACTIVITY_SECONDS` | Inactivity watchdog limit; resets whenever the agent produces stdout output. Stderr is still captured and logged but does not reset the timer. | No | `900` |
| `AGENT_TIMEOUT_GRACE_SECONDS` | Grace period (SIGTERM before SIGKILL) when terminating an agent. | No | `10` |
| `ALL_MENTIONS_AGENTS` | Comma-separated agent usernames expanded when `@all`/`@agents` is used in a comment. | No | `claude,gemini,codex` |
| `DEBUG_RELOAD_ROUTES` | Enable hot-reload for `config/routes.yaml`. | No | `false` |
| `LIVE_DASHBOARD_ENABLED` | Toggle the live dashboard endpoint (`/dashboard`). | No | `false` |
| `ROUTE_CONFIG_PATH` | Path to routing configuration. | No | `config/routes.yaml` |
| `PROMPT_DIR` | Directory containing prompt templates. | No | `prompts` |
| `RUN_LOGS_DIR` | Directory where structured agent outputs are written. | No | `run-logs` |
| `NPM_CACHE_DIR` | Location used for npm cache when installing agent CLIs. | No | `/work/.npm-cache` |
| `ENABLE_AUTO_CLONE` | Enable on-demand repository cloning when a webhook arrives for a project that doesn't exist locally. | No | `false` |
| `AUTO_CLONE_DEPTH` | Clone depth for auto-cloned repositories. `0` = full history (recommended), `1+` = shallow clone (faster). | No | `0` |
| `ENABLE_BRANCH_SWITCH` | Enable automatic branch switching before agent dispatch based on event type. | No | `false` |
| `ENABLE_SMART_BRANCH_SELECTION` | Use smart heuristics (closes_issues API, note mentions) instead of first-open-MR when resolving branches for issues. | No | `true` |
| `ENABLE_AUTO_UNASSIGN` | Automatically unassign agent after successful task completion or manual kill when triggered by agent assignment. | No | `false` |
| `ENABLE_BACKUP_NOTIFICATIONS` | Post a GitLab comment on the issue/MR when an auto-backup branch is created during branch resolution. | No | `true` |
| `MENTION_HOLD_SECONDS` | Seconds to hold mention-triggered dispatches before promoting to the queue. If an assignment webhook for the same agent+project+IID arrives within this window, the mention is suppressed. Set to `0` to disable. | No | `3.0` |

#### `APP_LOG_LEVEL` Guide

The log level controls how much detail appears in container stdout and, when `LIVE_DASHBOARD_ENABLED=true`, in the dashboard system-log pane. All modules use Python's standard `logging` library with the format `%(asctime)s | %(levelname)s | %(name)s | %(message)s`.

| Level | What it reveals | When to use |
| --- | --- | --- |
| `DEBUG` | Route pattern matching details, assignee extraction metadata, branch resolver remote-query fallbacks, branch pruning age analysis, glab JSON parse skip reasons, webhook header processing | Diagnosing why a webhook did not match a route, investigating branch resolution logic, or tracing glab enrichment issues |
| `INFO` | Route match results, webhook suppression events (self-unassign echo, system notes), agent lifecycle (completion status, return code, log file path), auto-unassign actions, trigger queue dispatch/suppression, project path resolution, auto-clone progress, branch pruning actions, log pruning summaries | Normal operation -- provides a clear audit trail of what the system did and why |
| `WARNING` | Branch checkout failures with fallback info, backup branch creation for uncommitted changes, agent dispatch cancellations/kills, glab CLI unavailability, project path verification issues, git command timeouts, clone URL validation failures | Conditions the system recovered from but that may need operator attention |
| `ERROR` | Agent dispatch failures (token resolution, prompt rendering, branch resolution, working directory issues), glab command failures/timeouts, webhook payload parse errors, auto-unassign failures, clone failures, timeout notification failures | Failures that prevented an operation from completing |

**Recommended settings:**
- **Production:** `INFO` (default) -- balanced signal without noise
- **Debugging webhook routing:** `DEBUG` -- shows pattern matching decisions in `app.services.routes`
- **Debugging agent dispatch:** `DEBUG` -- shows working directory resolution and branch selection in `app.services.agents` and `app.services.branch_resolver`
- **Debugging auto-unassign:** `DEBUG` -- shows assignee detection in `app.api.webhooks`

### Agent Model Selection

Model variables follow a naming convention: for any agent named `<agent>`, set `<AGENT>_MODEL` as an environment variable. The value is substituted wherever `${<AGENT>_MODEL}` appears after a `--model` flag in `routes.yaml`.

| Variable | Description | Required | Default |
| --- | --- | --- | --- |
| `CLAUDE_MODEL` | Model identifier for `${CLAUDE_MODEL}` placeholders. | Yes (when referenced in `routes.yaml`) | _(none)_ |
| `GEMINI_MODEL` | Model identifier for `${GEMINI_MODEL}` placeholders. | Yes (when referenced in `routes.yaml`) | _(none)_ |
| `CODEX_MODEL` | Model identifier for `${CODEX_MODEL}` placeholders. | Yes (when referenced in `routes.yaml`) | _(none)_ |

Custom agents follow the same convention. For example, an agent named `qwen-code` would use `QWEN_CODE_MODEL`.

> **Note:** Model variables have no application-level defaults. If a `${<AGENT>_MODEL}` placeholder is referenced in `routes.yaml` but the corresponding environment variable is not set, the application will raise `ValueError` at startup. Ensure all model variables used in your routes are defined in `.env` (see `.env.example` for reference values).

### Log Retention
| Variable | Description | Required | Default |
| --- | --- | --- | --- |
| `LOG_PRUNING_ENABLED` | Enable background pruning of historical run logs. | No | `true` |
| `LOG_RETENTION_DAYS` | Number of days to keep run logs when pruning is on. | No | `7` |
| `LOG_PRUNING_INTERVAL_HOURS` | Interval between pruning passes. | No | `24` |

### Branch Pruning
| Variable | Description | Required | Default |
| --- | --- | --- | --- |
| `BRANCH_PRUNING_ENABLED` | Enable background pruning of remote branches that have been merged into the base branch. | No | `false` |
| `BRANCH_PRUNING_INTERVAL_HOURS` | Interval between pruning passes. | No | `24` |
| `BRANCH_PRUNING_DRY_RUN` | When `true`, log branches that would be pruned without deleting them. Recommended for initial rollout. | No | `true` |
| `BRANCH_PRUNING_BASE_BRANCH` | Fallback branch to compare against when determining merge status. The pruner dynamically detects each repository's default branch via `git remote show origin`; this value is used only when detection fails. | No | `main` |
| `BRANCH_PRUNING_PROTECTED_PATTERNS` | Comma-separated glob patterns for branches that must never be pruned. | No | `main,master,HEAD,backup/*` |
| `BRANCH_PRUNING_AGENT` | Agent identity used for git authentication during pruning operations. | No | `claude` |
| `BRANCH_PRUNING_MIN_AGE_HOURS` | Minimum hours since a branch was merged into the base branch before it becomes eligible for pruning. Prevents deletion of recently-merged branches that may still be referenced by running pipelines. | No | `24` |

> **Note:** Branch pruning uses `git branch -r --merged` to detect merged branches. Branches merged via squash-merge are not detected by this method (v1 limitation).

### Agent Identity and Tokens

Agent tokens and identities follow a naming convention based on the agent name. For any agent named `<agent>`, the uppercased form (with hyphens replaced by underscores) becomes `<AGENT>`. See `docs/ADDING_AN_AGENT.md` for the full onboarding guide.

| Convention | Description | Required | Default |
| --- | --- | --- | --- |
| `<AGENT>_AGENT_GITLAB_TOKEN` | GitLab PAT for the agent. Needs `api` scope. | Yes | _(none)_ |
| `<AGENT>_AGENT_GIT_NAME` | Display name for git commits/comments. | No | `<Agent> Agent` |
| `<AGENT>_AGENT_GIT_EMAIL` | Email identity for git operations. | No | `<agent>@example.com` |

The three default agents (`claude`, `gemini`, `codex`) are pre-configured in `.env.example`. Custom agents use the same convention with no code changes required.

### Host Integration

**LLM Provider Authentication**

Agent CLIs authenticate with their respective LLM providers (Anthropic, Google, OpenAI) using the host user's personal account credentials. The `*_CONFIG_PATH` variables defined below point to the host directories containing these credentials (e.g., `~/.claude`), which are bind-mounted into the container. This allows the CLIs to reuse the same authentication session, keeping billing under the user's existing subscription plan and avoiding the cost risks of standalone API-key billing.

If you prefer dedicated credentials for automation, you can override these paths to point to directories containing bot-specific authentication files.

| Variable | Description | Required | Default |
| --- | --- | --- | --- |
| `LOCAL_UID` / `LOCAL_GID` | Map the container user to the host UID/GID so mounted agent token directories remain accessible. | Recommended | `1000` (compose fallback) |
| `CLAUDE_CONFIG_PATH` | Host directory to bind-mount for Claude CLI authentication. | No | `$HOME/.claude` |
| `GEMINI_CONFIG_PATH` | Host directory to bind-mount for Gemini CLI authentication. | No | `$HOME/.gemini` |
| `CODEX_CONFIG_PATH` | Host directory to bind-mount for Codex CLI authentication. | No | `$HOME/.codex` |

The GitLab CLI configuration is generated inside the container on startup using the agent tokens, so no bind mount is required for `glab-cli`.

## Sample `.env`

```ini
APP_HOST=0.0.0.0
APP_PORT=8080
APP_LOG_LEVEL=INFO
GITLAB_WEBHOOK_SECRET=replace-me
GLAB_HOST=gitlab.example.com
GLAB_TOKEN=glpat-app
LIVE_DASHBOARD_ENABLED=true
AGENT_MAX_WALL_CLOCK_SECONDS=7200
AGENT_MAX_INACTIVITY_SECONDS=900
ALL_MENTIONS_AGENTS=claude,gemini,codex

CLAUDE_AGENT_GITLAB_TOKEN=glpat-xxx
CLAUDE_AGENT_GIT_NAME="Claude Agent"
CLAUDE_AGENT_GIT_EMAIL=claude@example.com
GEMINI_AGENT_GITLAB_TOKEN=glpat-yyy
GEMINI_AGENT_GIT_NAME="Gemini Agent"
GEMINI_AGENT_GIT_EMAIL=gemini@example.com
CODEX_AGENT_GITLAB_TOKEN=glpat-zzz
CODEX_AGENT_GIT_NAME="Codex Agent"
CODEX_AGENT_GIT_EMAIL=codex@example.com

LOCAL_UID=1000
LOCAL_GID=1000
```

## Docker Compose Configuration

The `docker-compose.yml` mounts repository assets that ship with this project, agent CLI credential directories, and your projects directory.

### Project Directory Structure

Edit the project volume mount paths in `docker-compose.yml` (the `./projects:/work/projects` lines) to point to your parent directory containing all project repositories. Projects should be organized to mirror GitLab's namespace structure.

> **Note:** Projects are resolved automatically from the mounted `projects/` directory tree using the `<namespace>/<project-name>` structure. No separate project mapping file is needed. With `ENABLE_AUTO_CLONE=true`, missing projects are cloned on first webhook trigger.

```
projects/
  group/
    project-name/     # GitLab: group/project-name
    another-project/  # GitLab: group/another-project
  other-group/
    some-project/     # GitLab: other-group/some-project
```

The container mounts the projects directory twice:
- `/work/projects` — read-write mount for work routes
- `/work/projects-ro` — read-only mount for analysis/review routes

Routes use the `access` field (`readonly` or `readwrite`) to determine which mount the agent receives as its working directory. Default is `readonly`.

### On-Demand Cloning

Enable automatic repository cloning by setting `ENABLE_AUTO_CLONE=true`. When a webhook arrives for a project that doesn't exist locally, the system will:
1. Authenticate using the agent's GitLab credentials (`*_AGENT_GITLAB_TOKEN`)
2. Clone the repository to `projects/<namespace>/<project-name>`
3. Set the cloned directory as the agent's working directory

Clone depth is controlled by `AUTO_CLONE_DEPTH`:
- `0` (default) — full history, recommended for agents that need `git log` or `git blame`
- `1+` — shallow clone, faster initial clone but limited history

### Automatic Branch Switching

Enable automatic branch resolution by setting `ENABLE_BRANCH_SWITCH=true`. Before dispatching an agent, the system will checkout the appropriate branch based on the event type:

| Event Type | Branch Selection |
|------------|------------------|
| MR event (open, update, comment) | MR's `source_branch` |
| Issue with linked open MR | Linked MR's `source_branch` (smart selection when enabled) |
| New issue (no linked MR) | Default branch (queried from remote) |
| Other events | Default branch |

**Smart branch selection** (`ENABLE_SMART_BRANCH_SELECTION=true`, the default) adds intelligence to how the system selects a branch when multiple MRs are linked to an issue. GitLab automatically links MRs to issues whenever an MR is mentioned in the issue title, description, or comments, which can cause false positives when the original "first linked open MR" heuristic is used.

When smart selection is enabled, the system:
1. Fetches all open MRs linked to the issue
2. For each candidate, queries the GitLab `closes_issues` API to determine if the MR explicitly closes the issue (via `Closes #N` / `Resolves #N` in the MR description)
3. Ranks candidates using a priority tuple: note mention (`!<iid>` in comment) > explicitly closes issue > most recently updated > highest MR iid
4. If any MRs explicitly close the issue, only those (plus any note-mentioned MRs) are considered
5. If no MRs explicitly close the issue, all open MRs are ranked by recency

This prevents unintended branch switches when MRs are merely mentioned in issue discussions. When only a single open MR is linked, the `closes_issues` API call is skipped for efficiency.

Set `ENABLE_SMART_BRANCH_SELECTION=false` to revert to the original "first linked open MR" behavior.

**Uncommitted changes handling:**
If the working tree has uncommitted changes when a branch switch is required, the system will:
1. Create a backup branch: `backup/<agent>/<original-branch>-<timestamp>`
2. Commit all changes with message: `[auto-backup] Uncommitted changes from <branch>`
3. Push the backup branch to origin
4. Log a warning with the backup branch name
5. Proceed with the branch switch

If the backup fails and the working tree is dirty, the agent dispatch will fail to prevent accidental data loss.

**Backup notifications:**
When `ENABLE_BACKUP_NOTIFICATIONS=true` (the default), a comment is automatically posted on the triggering issue or merge request whenever a backup branch is created. The comment includes:
- The reason for the backup (uncommitted changes or local commits ahead of origin)
- The backup branch name
- Recovery instructions (`git fetch` / `git checkout` commands)

The comment is posted under the agent's own identity using its PAT. Notification failures are logged but never block agent dispatch. Set `ENABLE_BACKUP_NOTIFICATIONS=false` to disable.

### Automatic Agent Unassignment

Enable automatic agent unassignment by setting `ENABLE_AUTO_UNASSIGN=true`. When an agent completes a task successfully after being assigned to an issue or merge request, it will automatically be unassigned.

**Behavior:**
- Triggers on any assignment that matches a route (both `/assign @agent` quick action and manual UI assignment)
- Prefers `changes.assignees` from the webhook payload when available; falls back to top-level `assignees` for compatibility with GitLab CE payloads that omit the changes block
- Unassigns on successful completion (agent exits with code 0) and on manual kill via the dashboard
- Failed tasks (non-zero exit, timeout, crash) leave the assignment intact for follow-up
- Unassignment removes only the specific agent using `glab issue update --assignee '-agent'` or `glab mr update --assignee '-agent'`, preserving any other assignees

**Kill-path behavior:**
When an agent is force-killed via the dashboard kill switch, two actions are taken regardless of the `ENABLE_AUTO_UNASSIGN` setting:
1. A **termination comment** is posted on the GitLab issue/MR under the killed agent's identity, providing transparency for the team
2. If `ENABLE_AUTO_UNASSIGN=true`, the killed agent is **automatically unassigned** from the issue/MR

The termination comment uses the agent's own PAT (`*_AGENT_GITLAB_TOKEN`) so it appears under the agent's GitLab identity. If the agent-specific token is not available, it falls back to the app-level `GLAB_TOKEN`.

**Benefits:**
- Allows the GitLab quick action popup to show the agent again for subsequent assignments
- Cleaner issue/MR assignee lists after tasks complete
- Agent remains assigned when tasks fail, signaling need for human intervention

### Mention Hold Deduplication

When a user mentions `@agent` in a comment **and** assigns the agent via the GitLab UI sidebar simultaneously, GitLab fires two independent webhooks (Note Hook for the mention, Issue/MR Hook for the assignment). Without deduplication, the agent responds twice: once via the read-only mention route and once via the read-write assignment route.

The mention hold buffer (`MENTION_HOLD_SECONDS`) solves this by briefly delaying mention-triggered dispatches. If an assignment webhook for the same `(project, IID, agent)` key arrives within the hold window, the mention dispatch is suppressed and only the assignment route fires.

**Behavior:**
- Mention-triggered work items are held for `MENTION_HOLD_SECONDS` (default 3s) before being promoted to the dispatch queue
- If an assignment webhook arrives during the hold window, the held mention is cancelled and its HTTP response reports `status: suppressed`
- The existing `/assign @agent` text-based suppression (`_filter_assigned_mentions`) remains as a first-pass filter
- Only mentions of known agent usernames (from `ALL_MENTIONS_AGENTS`) are held; mentions of non-agent users bypass the hold buffer
- Set `MENTION_HOLD_SECONDS=0` to disable the hold buffer entirely (legacy behavior)

**Token scope:** The `GLAB_TOKEN` must have `api` scope when auto-unassign is enabled, since it needs permission to update issue/MR assignees. This is the same scope required by the File Hook's `GITLAB_ADMIN_TOKEN` (see `docs/GROUP_SETUP.md`). If you are running both the Robot Dev Team service and the GitLab File Hook, you can reuse the same `api`-scoped PAT for both `GLAB_TOKEN` and `GITLAB_ADMIN_TOKEN`, provided the token owner has Maintainer (or higher) access to the relevant projects. Using a single PAT simplifies credential management, though separate tokens are preferable if you want finer-grained audit trails or different token expiration policies.

**Note:** The auto-unassign event will generate a new webhook from GitLab. The system filters out these "assignee removed" events to prevent re-triggering routes.

### Agent Timeout Behavior

Agent execution is governed by a **dual-limit watchdog** that protects against both runaway processes and idle/hung agents while allowing long-running tasks to complete.

**Two independent limits:**
- `AGENT_MAX_WALL_CLOCK_SECONDS` (default: 7200 / 2 hours) -- hard upper bound on total run duration, regardless of output activity.
- `AGENT_MAX_INACTIVITY_SECONDS` (default: 900 / 15 minutes) -- watchdog that resets every time the agent produces output on stdout. Stderr is still captured and logged but does not reset the inactivity timer. This prevents agents stuck in retry loops (emitting only stderr) from blocking the queue indefinitely.

**Termination sequence:**
When either limit is reached, the system sends `SIGTERM` to the process group, waits `AGENT_TIMEOUT_GRACE_SECONDS` (default: 10) for a clean exit, then escalates to `SIGKILL`. Correctness does not depend on the CLI handling `SIGTERM` gracefully.

**Timeout notifications:**
When an agent times out, a comment is posted on the originating GitLab issue or MR under the agent's own identity, indicating which limit was hit and linking to the run log file.

**Per-route overrides:**
Timeout limits can be overridden at the route level in `config/routes.yaml` using `max_wall_clock_seconds` and `max_inactivity_seconds` fields. This allows long-running routes (e.g., `assign-work`) to have higher limits than quick review routes. When not specified, routes fall back to the global environment variables.

```yaml
routes:
  - name: assign-issue-claude
    access: readwrite
    max_wall_clock_seconds: 14400  # 4 hours for complex tasks
    max_inactivity_seconds: 1800   # 30 minutes inactivity tolerance
    match:
      event: "Issue Hook"
      ...
```

**Preserved stderr:**
On timeout, captured stderr output is preserved and appended with a `[System] Agent timed out (reason)` marker instead of being replaced. This ensures diagnostic output is available for debugging.

**Prompt template variables:**
When branch switching is enabled, the following substitutions are available in prompt templates:
- `${SOURCE_BRANCH}` — MR source branch (if applicable)
- `${TARGET_BRANCH}` — MR target branch (if applicable)
- `${CURRENT_BRANCH}` — The actual checked-out branch after resolution (always populated when `working_dir` exists)

### Starting the Stack

```bash
docker compose up --build
```

The `app` service uses `restart: unless-stopped`, so the container will automatically restart after a system reboot or Docker daemon restart. It will **not** restart after an explicit `docker compose stop` or `docker compose down`. Ensure the Docker daemon itself is enabled at boot (`sudo systemctl enable docker`) for fully unattended recovery.

### Manual Overrides

For additional customizations, create `docker-compose.override.yml`. Compose merges the override with the base file automatically. The override file is ignored by git so you can safely customize it per machine.

Ensure the host directories are readable by the mapped UID/GID. If you are using Docker Desktop on Windows, confirm that the drive is shared with Docker Desktop so the mounts succeed.

## Platform Notes

- **Linux** — Docker Desktop or the native Docker Engine both work. Set `LOCAL_UID`/`LOCAL_GID` to `id -u` / `id -g` (compose defaults to `1000`); mismatches cause permission problems when the container writes to bind mounts.
- **Windows via WSL** — Install WSL2 with an Ubuntu distribution, then install Docker Desktop and enable WSL integration for that distribution. Place the repository inside the Linux filesystem (e.g., `/home/<user>/robot-dev-team`) to avoid slow path traversal. Share your Windows home directory with Docker Desktop if agent CLIs store credentials there, or place credentials within the WSL home and update the mount paths.
- **macOS** — The stack runs under Docker Desktop. Verify that CLI credential directories are shared via the Filesharing options.

Refer back to `.env.example` when new configuration options are added.
