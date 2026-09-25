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
| `APP_PORT` | Exposed port for HTTP traffic. | No | `8888` |
| `APP_LOG_LEVEL` | Log verbosity (see level guide below). | No | `INFO` |
| `GITLAB_WEBHOOK_SECRET` | Shared token validated against `X-Gitlab-Token`. Leave empty to disable verification (not recommended). | Yes | _(none)_ |
| `GLAB_HOST` | GitLab instance hostname used by `glab-usr` and `gitlab-connect`. Must match the hostname in webhook-provided clone URLs (i.e. the hostname part of `GITLAB_EXTERNAL_URL` in `gitlab/.env`). For local Docker deployments using the bundled `gitlab/docker-compose.gitlab.yml`: set to `gitlab` (the Docker service name). For production: your GitLab domain (e.g. `gitlab.example.com`). Must be a bare host (no scheme); set `GLAB_PROTOCOL` separately. The wrappers refuse to run when this resolves to the placeholder `gitlab.example.com`. | Yes | `gitlab.com` |
| `GLAB_API_HOST` | Override the API host when it differs from `GLAB_HOST` — e.g. when GitLab runs on a non-standard port and is not reachable via the shared Docker network. When using the `robot-gitlab-net` shared network this is not needed since GitLab's internal port 80 is used directly. Bare host (no scheme). | No | _(same as `GLAB_HOST`)_ |
| `GLAB_PROTOCOL` | Protocol for GitLab API and git operations: `https` (default) or `http`. Set to `http` for local deployments without TLS. Used by `glab-usr` for `glab auth login --api-protocol` and the git credential file scheme. Values outside `http`/`https` are rejected at startup. | No | `https` |
| `GLAB_TOKEN` | GitLab PAT for the app process. Used for enrichment (fetching issue/MR context) and auto-unassign operations. Requires `api` scope when `ENABLE_AUTO_UNASSIGN` is enabled; `read_api` is sufficient if auto-unassign is disabled. | No | _(none)_ |
| `GLAB_TIMEOUT_SECONDS` | Timeout for GitLab CLI enrichment calls. | No | `30` |
| `AGENT_MAX_WALL_CLOCK_SECONDS` | Hard upper-bound run duration for each agent CLI invocation. | No | `7200` |
| `AGENT_MAX_INACTIVITY_SECONDS` | Inactivity watchdog limit; resets whenever the agent produces stdout output. Stderr is still captured and logged but does not reset the timer. | No | `900` |
| `AGENT_TIMEOUT_GRACE_SECONDS` | Grace period (SIGTERM before SIGKILL) when terminating an agent. | No | `10` |
| `ALL_MENTIONS_AGENTS` | Comma-separated agent usernames expanded when `@all`/`@agents` is used in a comment. | No | `claude,gemini,codex` |
| `RANDOMIZE_ALL_MENTIONS` | Shuffle the per-mention dispatch order for split work items, but **only** when the trigger came from `@all`/`@agents` expansion. Explicitly listing agents (e.g. `@claude @gemini @codex`) preserves author-specified order. | No | `true` |
| `DEBUG_RELOAD_ROUTES` | Enable hot-reload for `config/routes.yaml`. | No | `false` |
| `LIVE_DASHBOARD_ENABLED` | Toggle the live dashboard endpoint (`/dashboard`). | No | `false` |
| `ROUTE_CONFIG_PATH` | Path to routing configuration. | No | `config/routes.yaml` |
| `PROMPT_DIR` | Directory containing prompt templates. | No | `prompts` |
| `RUN_LOGS_DIR` | Directory where structured agent outputs are written. | No | `run-logs` |
| `ENABLE_AUTO_CLONE` | Enable on-demand repository cloning when a webhook arrives for a project that doesn't exist locally. | No | `false` |
| `AUTO_CLONE_DEPTH` | Clone depth for auto-cloned repositories. `0` = full history (recommended), `1+` = shallow clone (faster). | No | `0` |
| `ENABLE_BRANCH_SWITCH` | Enable automatic branch switching before agent dispatch based on event type. | No | `false` |
| `ENABLE_SMART_BRANCH_SELECTION` | Use smart heuristics (closes_issues API, note mentions) instead of first-open-MR when resolving branches for issues. | No | `true` |
| `ENABLE_AUTO_UNASSIGN` | Automatically unassign agent after successful task completion or manual kill when triggered by agent assignment. | No | `false` |
| `ENABLE_ASSIGN_ON_ISSUE_CREATION` | Dispatch an agent's read-write `assign_work` route immediately when an issue is created with that agent already assigned, instead of first running readonly triage. The assign route takes precedence over `issue-triage` on `open`, so it is an either/or (pre-assigned agent skips triage). When `false`, creation events fall through to triage; assigning to an existing issue via `/assign` (`action: update`) is unaffected. | No | `true` |
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
| `CLAUDE_MODEL` | Model identifier for `${CLAUDE_MODEL}` placeholders, passed to `claude --model` (e.g. `claude-opus-5`). | Yes (when referenced in `routes.yaml`) | _(none)_ |
| `GEMINI_MODEL` | Model identifier for `${GEMINI_MODEL}` placeholders. Use a stable model slug from `agy models` (e.g. `gemini-3.6-flash-high`); the slug encodes the reasoning-effort tier and needs no quoting. Slugs require `agy` >= 1.1.5; the older friendly form (`"Gemini 3.6 Flash (High)"`, quoted because it contains spaces) still works on newer builds and is the only form builds before 1.1.5 accept. List the names your account can use with `docker compose exec -u appuser app agy models` -- `agy` runs inside the container, so it is normally not on the host `PATH`. **`agy` validates this value**: an unrecognized name aborts the run with exit 1 and prints the available models. Referenced model variables must be set and non-empty or startup fails. | Yes (when referenced in `routes.yaml`) | _(none)_ |
| `CODEX_MODEL` | Model identifier for `${CODEX_MODEL}` placeholders, passed to `codex --model` (e.g. `gpt-5.6-sol`). The bare `gpt-5.6` alias currently routes to Sol; prefer the explicit ID so a future alias change cannot silently move the agent to another model. | Yes (when referenced in `routes.yaml`) | _(none)_ |
| `OPENCODE_KIMI_MODEL` | Model slug for the optional `opencode-kimi` instance's `${OPENCODE_KIMI_MODEL}` placeholder (e.g. `openrouter/moonshotai/kimi-k2.6`). Only needed if the commented OpenCode routes are enabled. | No (optional agent) | _(none)_ |
| `GOOSE_GEMMA_MODEL` | Model name for the optional `goose-gemma` instance's `${GOOSE_GEMMA_MODEL}` placeholder, passed to `goose run --model`. Must name a model the mounted Goose config's active provider serves. Note a llama.cpp server ignores the requested model and serves whatever is loaded, so against a local backend this is effectively a label for the run logs. Only needed if the commented Goose routes are enabled. | No (optional agent) | _(none)_ |
| `GROK_MODEL` | Model ID for the optional `grok` agent's `${GROK_MODEL}` placeholder, passed to `grok --model` (e.g. `grok-4.5`). Run `grok models` to list the IDs your account can use. Only needed if the commented Grok routes are enabled. | No (optional agent) | _(none)_ |
| `PI_NEMOTRON_MODEL` | Model ID for the optional `pi-nemotron` instance's `${PI_NEMOTRON_MODEL}` placeholder, passed to `pi --model` (e.g. `nvidia/nemotron-3-ultra-550b-a55b`). Must name a model the mounted Pi config's active provider serves (the default is routed via OpenRouter). Only needed if the commented Pi route is enabled. | No (optional agent) | _(none)_ |

Custom agents follow the same convention. For example, an agent named `qwen-code` would use `QWEN_CODE_MODEL`. Grok Build is the simple case: one `grok` binary backs one logical agent, so the agent slug, the GitLab username, and the env prefix are all plain `grok` (`GROK_MODEL`, `GROK_AGENT_GITLAB_TOKEN`). Because OpenCode and Goose are provider-agnostic, their logical agents are named per model -- `opencode-kimi` uses `OPENCODE_KIMI_MODEL` and `goose-gemma` uses `GOOSE_GEMMA_MODEL`; a second instance (`opencode-gpt`, `goose-qwen`) would use `OPENCODE_GPT_MODEL` / `GOOSE_QWEN_MODEL` -- all backed by the one shared `opencode` / `goose` binary.

> **Note:** the agent slug and the GitLab username are independent. Routes match the *username* from the webhook payload (`kimi`, `gemma`), while the `agent:` slug selects the credentials the run dispatches under (`opencode-kimi`, `goose-gemma`). The GitLab account therefore does not need the harness prefix.

> **Note:** Model variables have no application-level defaults. If a `${<AGENT>_MODEL}` placeholder is referenced in `routes.yaml` but the corresponding environment variable is not set or resolves to an empty string, the application will raise `ValueError` at startup. Ensure all model variables used in your routes are defined in `.env` (see `.env.example` for reference values).

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
| `<AGENT>_AGENT_GIT_EMAIL` | Email identity for git operations. Effectively required: the `<agent>@example.com` default is treated as a placeholder, and `glab-usr` refuses to authenticate rather than write commits with that authorship. The check fires on every invocation including read-only paths. | Yes | _(none; placeholder `<agent>@example.com` is rejected)_ |

The three default agents (`claude`, `gemini`, `codex`) are pre-configured in `.env.example`. Custom agents use the same convention with no code changes required. The optional `opencode-kimi` instance ships commented out in `.env.example`; enabling it means uncommenting its `OPENCODE_KIMI_AGENT_GITLAB_TOKEN` / `OPENCODE_KIMI_AGENT_GIT_NAME` / `OPENCODE_KIMI_AGENT_GIT_EMAIL` block (note the model tag precedes `_AGENT` so the token convention resolves the `opencode-kimi` name). The optional `grok` agent ships commented out the same way, using the plain `GROK_AGENT_*` forms. The optional `pi-nemotron` instance likewise ships commented out, using `PI_NEMOTRON_AGENT_*` (model tag before `_AGENT`, same as `opencode-kimi`); its GitLab username is the plain `nemotron`.

### Startup Preflight (strict)

The container validates this configuration before it starts, and **refuses to boot** if a route could never work. The invariant is:

> Every agent named by an enabled route must have a GitLab token **and** a git identity.

Concretely, for each agent in `config/routes.yaml` (or your `ROUTE_CONFIG_PATH` override):

- `<AGENT>_AGENT_GITLAB_TOKEN` must be non-empty, **or** a token file must exist at `~/.<agent>/glab-token`; and
- `<AGENT>_AGENT_GIT_EMAIL` must be a real address (the `@example.com` placeholder is rejected, matching `glab-usr`).

Both are hard requirements inside `glab-usr`, so a route missing either can never dispatch. Failing at startup replaces the old behaviour, where the same misconfiguration surfaced much later and far less clearly -- mid-dispatch, after a webhook had already fired.

The rule is deliberately **asymmetric**. The reverse case is only a warning:

| Situation | Result |
| --- | --- |
| Route dispatches an agent with no token or no git email | **Fatal** -- container will not start |
| `BRANCH_PRUNING_AGENT` lacks credentials while `BRANCH_PRUNING_ENABLED=true` | **Fatal** |
| `ROUTE_CONFIG_PATH` points at a file that does not exist | **Fatal** -- a typo must not look like an empty config |
| The active config yields **no dispatchable agent tasks** (empty file, or every agent entry missing its `agent:` key) | **Fatal** -- validation would be vacuous |
| A route gives an agent an empty `command:` | **Fatal** -- dispatch would exec nothing |
| A required harness binary is **not on PATH after the installs run** | **Fatal** -- see below |
| Agent has a token but no route | Warning -- supported (staged rollout; pruning identities) |
| `ALL_MENTIONS_AGENTS` names an agent no mention route matches | Warning -- `@all` silently drops it |
| A route runs a command no install script provides | Warning -- assumed already on PATH (BYOA), then verified |

There is **no escape hatch**: a bad config must be fixed, not suppressed. If a shipped route names an agent you do not run, delete that route (see "Removing an Agent" in `docs/ADDING_AN_AGENT.md`) rather than leaving it dead.

The same preflight decides **which harnesses get installed**: only the binaries that a routed, credentialed agent actually needs. It then checks those binaries are on PATH once the installers finish -- credentials are validated before the installs, binaries after -- so a failed download or a mis-declared `# provides:` line stops the container instead of failing later on every dispatch. (The download *failing* is only a warning; the harness being *absent at the end* is fatal.)

Run the config half by hand at any time --

```bash
python -m app.preflight     # prints the install set; exits 1 on bad config
```

### Host Integration

**LLM Provider Authentication**

Agent CLIs authenticate with their respective LLM providers (Anthropic, Google, OpenAI, and -- via OpenCode -- any provider such as OpenRouter) using the host user's personal account credentials. The `*_CONFIG_PATH` variables defined below point to the host directories containing these credentials (e.g., `~/.claude`), which are bind-mounted into the container. This allows the CLIs to reuse the same authentication session, keeping billing under the user's existing subscription plan and avoiding the cost risks of standalone API-key billing.

If you prefer dedicated credentials for automation, you can override these paths to point to directories containing bot-specific authentication files.

| Variable | Description | Required | Default |
| --- | --- | --- | --- |
| `LOCAL_UID` / `LOCAL_GID` | Map the container user to the host UID/GID so mounted agent token directories remain accessible. | Recommended | `1000` (compose fallback) |
| `CLAUDE_CONFIG_PATH` | Host directory to bind-mount for Claude CLI authentication. | No | `$HOME/.claude` |
| `GEMINI_CONFIG_PATH` | Host directory to bind-mount for the Gemini agent (Antigravity CLI). Carries settings, conversation history, and the `antigravity-cli/antigravity-oauth-token` JSON file that `agy` falls back to whenever no host desktop libsecret session is present. The container always wants the file form; complete the one-time OAuth bootstrap from `docs/AGENT_ONBOARDING.md` so the file exists on the host before starting the stack. | No | `$HOME/.gemini` |
| `CODEX_CONFIG_PATH` | Host directory to bind-mount for Codex CLI authentication. | No | `$HOME/.codex` |
| `OPENCODE_CONFIG_PATH` | Host directory to bind-mount for OpenCode global config (`opencode.jsonc`). Only used when the optional OpenCode mounts are uncommented in `docker-compose.yml`. | No | `$HOME/.config/opencode` |
| `OPENCODE_DATA_PATH` | Host directory to bind-mount for OpenCode data, including the `auth.json` that holds provider credentials (OpenRouter, etc.). Mounting it is how the container reuses the host's `opencode auth login` session. Only used when the optional OpenCode mounts are uncommented. | No | `$HOME/.local/share/opencode` |
| `GOOSE_CONFIG_PATH` | Host directory to bind-mount for Goose config: the active provider, model, extensions, and any `secrets.yaml`. Only used when the optional Goose mount is uncommented in `docker-compose.yml`. Unlike the mounts above it is mounted **read-only**, onto a staging path -- see the note below. | No | `$HOME/.config/goose` |
| `GROK_CONFIG_PATH` | Host directory to bind-mount for Grok Build config, including the `auth.json` that holds the OAuth session. Mounting it is how the container reuses the host's `grok login`. Read-write, like the Claude/Gemini/Codex mounts. Only used when the optional Grok mount is uncommented in `docker-compose.yml`. | No | `$HOME/.grok` |
| `GROK_DEPLOYMENT_KEY` | Alternative to the `~/.grok` mount for headless hosts that cannot complete the browser OAuth flow. There is no `XAI_API_KEY` code path. | No | _(none)_ |
| `PI_CONFIG_PATH` | Host directory to bind-mount for Pi config, including the `auth.json` that holds the provider key (OpenRouter for the default model) and `settings.json`. Mounting it is how the container reuses the host's Pi setup. Read-write, like the Claude/Gemini/Codex mounts. Points at the narrow `agent` config root (not all of `~/.pi`) on purpose. Only used when the optional Pi mount is uncommented in `docker-compose.yml`. | No | `$HOME/.pi/agent` |

The GitLab CLI configuration is generated inside the container on startup using the agent tokens, so no bind mount is required for `glab-cli`.

**Goose and locally served models**

Goose's config is the only one the container does not use as-mounted. An operator whose Goose drives a model served on the host (llama.cpp, Ollama, LM Studio, vLLM) has a `base_url` on loopback -- `http://127.0.0.1:10000/v1` or similar. That is correct on the host, but inside the container `127.0.0.1` is the container itself.

So `GOOSE_CONFIG_PATH` is mounted read-only at `~/.config/goose-host`, and on startup the entrypoint runs `python -m app.goose_config`, which copies it to `~/.config/goose` with loopback hosts (`127.0.0.0/8`, `localhost`, `::1`, `0.0.0.0`) redirected at `host.docker.internal`. Both spellings Goose accepts are handled: a full URL (a custom provider's `base_url`) and a bare `*_HOST` value with no scheme (`OLLAMA_HOST: localhost:11434`), which Goose also takes. Only the host is swapped -- scheme, port, path, and any remote host are preserved. The copy exists so the container never rewrites the host's own config, which must keep pointing at loopback to run Goose on the host.

Consequences worth knowing:

- Editing the host Goose config requires a container restart to take effect.
- The model server must accept connections from the Docker host gateway. A server bound to `127.0.0.1` on the host will refuse the container even after the rewrite -- bind it to `0.0.0.0`.
- No `GOOSE_PROVIDER` / `OPENAI_HOST` / API-key variables are needed: everything but the endpoint is taken from the mounted config as-is. A provider that *does* need a key should carry it in `~/.config/goose/secrets.yaml` on the host; it is copied into the container and Goose reads it there, with no `GOOSE_DISABLE_KEYRING` required (verified against goose 1.41.0: a key present only in `secrets.yaml`, with no keyring daemon or dbus in the container, is picked up and sent).
- **A llama.cpp provider must set `"supports_streaming": false`.** llama.cpp does not emit valid OpenAI SSE when a response contains a tool call, and Goose masks the resulting server error as an opaque `Stream decode error` ([goose#8021](https://github.com/aaif-goose/goose/issues/8021)). The run burns minutes of GPU, llama-server logs nothing wrong, the watchdog never fires, and no comment is posted. Non-streaming shifts stdout to once per turn, so `max_inactivity_seconds` must cover a full turn of local inference (3600 is a sane start).
- The mount carries an extension's **config, not its executable**. Builtin (`type: platform`) extensions such as `developer` work unchanged; a `type: stdio` extension launched with `npx` cannot, because the base image ships no Node.js. (The optional Pi harness bootstraps a user-local Node only when enabled, but exposes **only `node`** on the shared `PATH` -- its `npm`/`npx` stay inside Pi's versioned Node dir -- so an enabled Pi never hands Goose a runnable `npx`.) `goose_config` warns at boot when an enabled stdio extension names an executable that is not on the container's `PATH`.
- Mount the host config **only** at the staging path. Adding a second, conventional mount onto `~/.config/goose` would point the materialization's rebuild at the real host config; `goose_config` refuses to run if its target is a mount point rather than deleting it.

## Sample `.env`

```ini
APP_HOST=0.0.0.0
APP_PORT=8888
APP_LOG_LEVEL=INFO
GITLAB_WEBHOOK_SECRET=replace-me
GLAB_HOST=gitlab.example.com
GLAB_TOKEN=glpat-app
LIVE_DASHBOARD_ENABLED=true
AGENT_MAX_WALL_CLOCK_SECONDS=7200
AGENT_MAX_INACTIVITY_SECONDS=900
ALL_MENTIONS_AGENTS=claude,gemini,codex
RANDOMIZE_ALL_MENTIONS=true

# Note: *_AGENT_GIT_EMAIL values that end in @example.com are rejected by
# glab-usr (placeholder loud-fail). Replace the domain with your real one.
CLAUDE_AGENT_GITLAB_TOKEN=glpat-xxx
CLAUDE_AGENT_GIT_NAME="Claude Agent"
CLAUDE_AGENT_GIT_EMAIL=claude@your-org.tld
GEMINI_AGENT_GITLAB_TOKEN=glpat-yyy
GEMINI_AGENT_GIT_NAME="Gemini Agent"
GEMINI_AGENT_GIT_EMAIL=gemini@your-org.tld
CODEX_AGENT_GITLAB_TOKEN=glpat-zzz
CODEX_AGENT_GIT_NAME="Codex Agent"
CODEX_AGENT_GIT_EMAIL=codex@your-org.tld

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

### Assign on Issue Creation

Enabled by default (`ENABLE_ASSIGN_ON_ISSUE_CREATION=true`). When an issue is
**created** with an agent already populated in the assignee field, GitLab sends
an `Issue Hook` with `action: open`. The shipped `assign-issue-*` routes match
both `open` and `update` and are ordered above `issue-triage`, so the event
dispatches the agent's read-write `assign_work` route immediately instead of
running readonly triage first.

**Behavior:**
- This is deliberately **either/or**: a pre-assigned agent skips triage/review
  and goes straight to work — there is no second-opinion review pass on creation.
- Only affects issue **creation**. Assigning an agent to an existing issue via
  the `/assign` quick action (`action: update`) already dispatched `assign_work`
  and is unchanged.
- Merge requests are **not** affected: an MR opened with an assignee still goes
  through `default-merge-request` review.

**Disabling:** Set `ENABLE_ASSIGN_ON_ISSUE_CREATION=false` to revert creation
events to the previous behavior, where a create-with-assignee event falls
through to `issue-triage`. Note that with `ENABLE_AUTO_UNASSIGN=true`, a triaged
create-with-assignee issue has its pre-assigned agent unassigned once triage
completes — the reason this feature was added.

> **Local overrides:** The precedence comes from route order and the
> `action: ["open", "update"]` field in `config/routes.yaml`. Operators using a
> local override (`ROUTE_CONFIG_PATH=config/routes.local.yaml`) must apply the
> same reorder and action-list change to their copy for the feature to take
> effect, regardless of the toggle.

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

### Deployment Profiles

The right network and GitLab host configuration depends on where the two services are running relative to each other. There are three common profiles:

---

**Profile 1: Local Docker (same host, bundled `gitlab/docker-compose.gitlab.yml`)**

Both containers are on the same machine. `robot-dev-team` reaches GitLab via the Docker host using `host.docker.internal`, which resolves to the host gateway from inside any container (provided by `extra_hosts: host.docker.internal:host-gateway` in both compose files, or automatically on Docker Desktop).

The key detail: `external_url` in `GITLAB_OMNIBUS_CONFIG` controls the hostname in generated clone URLs, but setting a non-standard port there would make GitLab's nginx also listen on that port — breaking the `8929:80` mapping. This is resolved by adding `nginx['listen_port'] = 80` to the Omnibus config, which pins the internal listen port to 80 regardless of what `external_url` says (the standard GitLab pattern for reverse-proxy setups).

| Setting | Value |
|---|---|
| `GITLAB_EXTERNAL_URL` (in `gitlab/.env`) | `http://host.docker.internal:8929` |
| `GLAB_HOST` | `host.docker.internal` |
| `GLAB_API_HOST` | `host.docker.internal:8929` |
| `GLAB_PROTOCOL` | `http` |
| `gitlab/docker-compose.gitlab.yml` port mapping | `8929:80` (unchanged; browsers still use `localhost:8929`) |
| Shared Docker network | Not required |

---

**Profile 2: Production (reverse proxy, real public domain)**

GitLab sits behind a reverse proxy (nginx, Caddy, etc.) with a real domain and TLS. `robot-dev-team` can reach GitLab either via the shared Docker network (if on the same host) or via the public URL. The shared network is optional but still useful to avoid an unnecessary trip through the proxy for clone and API calls.

| Setting | Value |
|---|---|
| `GITLAB_EXTERNAL_URL` (in `gitlab/.env`) | `https://gitlab.example.com` |
| `GLAB_HOST` | `gitlab.example.com` |
| `GLAB_PROTOCOL` | `https` |
| `GLAB_API_HOST` | _(not needed)_ |
| Shared network | Optional — omit `robot-gitlab-net` from `docker-compose.yml` if preferred |

The reverse proxy terminates TLS and forwards to GitLab's internal port 80 as normal. The `robot-gitlab-net` network does not interfere with it.

---

**Profile 3: Containers on different Docker hosts**

Docker bridge networks are host-local and cannot span machines. The shared network approach does not apply here.

| Setting | Value |
|---|---|
| `GITLAB_EXTERNAL_URL` (in `gitlab/.env`) | `https://gitlab.example.com` (must be publicly routable) |
| `GLAB_HOST` | `gitlab.example.com` |
| `GLAB_PROTOCOL` | `https` |
| Shared network | **Not applicable** — remove `robot-gitlab-net` from both compose files |

`robot-dev-team` reaches GitLab via the public URL. All clone and API calls go over the network between hosts. Ensure `robot-dev-team`'s host can reach GitLab's domain and that the GitLab instance allows webhook delivery to `robot-dev-team`'s public address.

---

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
