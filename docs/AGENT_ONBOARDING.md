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
- Access tokens for the Claude, Gemini, and Codex GitLab accounts
- `glab` CLI installed on the host

> All three default agent CLIs (Claude Code, Antigravity for Gemini, Codex)
> use native installers, so Node.js / npm are not required on the host or in
> the container. The one exception is the optional Pi harness (an npm package):
> inside Docker `scripts/install-pi.sh` bootstraps a pinned user-local Node when
> a `pi-*` route is enabled, but a local (non-Docker) operator who enables Pi
> must supply Node >=22.19.0 and `pi` themselves.

## How Agent CLI Authentication Works

The agent CLIs (Claude Code, Antigravity CLI for Gemini, Codex CLI) authenticate to their respective LLM providers using the host user's personal account credentials. When you authenticate each CLI during setup, the credentials stored in `~/.claude`, `~/.gemini`, and `~/.codex` are bind-mounted into the Docker container at runtime. This means:

- Agent operations are billed to your existing subscription plan, not to a separate API account.
- Your plan's rate limits and spending controls apply automatically.
- No separate API keys need to be generated or managed for LLM access.

GitLab authentication is separate and uses per-agent Personal Access Tokens (PATs) configured in `.env`.

### Antigravity (Gemini agent) OAuth bootstrap

The `gemini` agent runs the Antigravity CLI (`agy`), which stores its OAuth credential in two possible places depending on the environment:

- **Host with a desktop session** -- `agy` writes the token into the Linux Secret Service (libsecret / GNOME Keyring). Convenient for interactive host use, but the keyring is **not** inherited into the Docker container.
- **Headless environment** -- `agy` falls back to a plain JSON file at `~/.gemini/antigravity-cli/antigravity-oauth-token` (mode 0600). The existing `~/.gemini` bind mount in `docker-compose.yml` carries that file straight into the container at `/home/appuser/.gemini/antigravity-cli/`.

The container is always "headless" from `agy`'s perspective, so it always wants the file form. On a new deployment, complete a one-time bootstrap so the file exists:

```bash
docker compose run --rm app agy
```

This relies on the default entrypoint (`docker-entrypoint.sh`) so the UID/GID remap and the agent installers (`scripts/install-*.sh`) run before `agy` starts. The CLI exec's as `appuser` post-remap, which means the OAuth credential file Antigravity writes is owned by the same UID that normal webhook dispatch uses (typically `LOCAL_UID` / `LOCAL_GID` from your `.env`, defaulting to 1000). Do **not** override `--entrypoint` here -- it bypasses the remap and produces a root-owned credential file that the unprivileged webhook process cannot read.

`agy` will print a Google OAuth URL. Open it on a browser, sign into the Google account you want the `gemini` agent to use, and complete consent. After the OAuth callback resolves, `agy` writes `~/.gemini/antigravity-cli/antigravity-oauth-token` (visible on both the container and the host via the bind mount). You can then `Ctrl-D` or `/exit` out of `agy`; every subsequent `docker compose up` inherits the file automatically.

If your host already has `agy` authenticated via libsecret, the bootstrap above is still safe to run -- it creates the file alongside the libsecret entry without touching it. The Gemini preflight in `app/services/agents.py` checks for the file before invoking `agy` and fast-fails with the same bootstrap command if it is missing, unreadable, or not valid JSON, so you'll see a clear message in run logs instead of a 30-second OAuth callback timeout.

Antigravity's tiered credential strategy is upstream behavior we do not control. If a future `agy` release stops honoring the file fallback, the smoke step below (and the preflight unit tests) will surface that immediately; we'd need to fold the keyring approach back in at that point.

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
4. **Bootstrap Antigravity OAuth** (only if you'll use the `gemini` agent)
   - Run `docker compose run --rm app agy` (described in *Antigravity (Gemini agent) OAuth bootstrap* above).
   - Complete the Google sign-in in your browser.
   - Verify `~/.gemini/antigravity-cli/antigravity-oauth-token` exists on the host afterwards and is owned by your local UID (not root).
5. **Start the service**
   ```bash
   ./launch-uvicorn-dev
   ```
   Auto-reload keeps the server in sync with source changes.
6. **Run smoke tests (when available)**
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
   which agy      # Antigravity CLI -- the `gemini` agent's harness
   which codex
   ```
2. Run `glab-usr <agent>` (or rely on `gitlab-connect`) to confirm authentication for each agent user.
3. When using `gitlab-connect`, agent identity can be set in several ways (checked in order): the `CURRENT_AGENT` environment variable, `ROBOT_AGENT_NAME`, or the currently active `glab` user. Explicitly setting `CURRENT_AGENT` is one way to force a specific identity:
   ```bash
   export CURRENT_AGENT=codex
   gitlab-connect issue view 1
   ```
4. Review `docs/GITLAB_WEBHOOKS.md` to ensure local GitLab projects route events correctly. For automatic webhook provisioning on new projects, see `docs/GROUP_SETUP.md`.

## Testing wrappers inside a running container

When validating changes to `glab-usr` or `gitlab-connect` (or any code path that invokes `glab`) from inside the live container, always pass `-u appuser` to `docker compose exec`:

```bash
docker compose exec -u appuser app gitlab-connect issue view 1
```

`docker compose exec` defaults to **root**, and a single root invocation of `glab` creates `/home/appuser/.config/glab-cli/` and `/home/appuser/.gitconfig` owned by root. Once that happens, every subsequent appuser-driven agent dispatch (webhooks, branch pruner, mention triggers) fails with `permission denied` until the files are chowned back. `glab-usr` will now detect this state and emit a remediation hint, but the cleanest path is to avoid it entirely.

If you do hit it, recover with:

```bash
docker compose exec -u root app chown -R appuser:appuser /home/appuser
```

(Chowning the whole home directory is the most robust form -- the file list `.config` / `.gitconfig` / `.config/glab-cli/git-credentials-<host>` / `glab-cli/config.yml` covers the known poisoning paths, but any of them may be absent on a fresh container, which would make a path-list `chown` exit non-zero.)

The container entrypoint also performs a recursive chown on every startup, so restarting the container is an alternative recovery.

## How agent credentials are scoped

Several agents share one `appuser` home directory and, in a shared checkout, one `.git/config`. Getting the *right* agent to authenticate a push therefore takes more than writing the right credential file, and the details below explain why the configuration looks the way it does.

### Where the credentials live

`glab-usr <agent>` writes the selected agent's token to one of two stores:

| Situation | Store |
| --- | --- |
| Inside a checkout whose common git directory is writable | `<common git dir>/credentials` |
| Read-only project mount, or outside any repository | `~/.config/glab-cli/git-credentials-<host>` |

The **common git directory** is what `git rev-parse --git-common-dir` reports. For an ordinary checkout that is `<repo>/.git`, so the store is `<repo>/.git/credentials` as before. It is named this way because `.git` is not always a directory:

- In a **linked worktree** (`git worktree add`), `.git` is a *file* holding a `gitdir:` pointer, and the common directory is the *parent* repository's `.git`.
- In a **submodule**, `.git` is likewise a file, and the common directory is `<parent>/.git/modules/<name>`.

Both are mode `0600`.

**One agent per repository, worktrees included.** `git config --local` run from a linked worktree writes the repository's *common* config, which every sibling worktree reads. Authenticating in one worktree therefore re-points the credential helper and the git identity for all of them: run `glab-usr codex` in `wt-b` and a push from `wt-a` authenticates as Codex. This is deliberate: the dispatcher processes triggers sequentially, and `glab-usr` also mutates home-level `glab` state (`~/.config/glab-cli/`) that no per-worktree git config could isolate, so per-worktree credential isolation would advertise a concurrency guarantee the rest of the system does not provide. Do not treat sibling worktrees as independently assignable to different agents.

The global fallback is **per host** and deliberately not `~/.git-credentials`: that path is git's shared default store, and it holds entries for other hosts (a GitHub mirror, a second GitLab). `glab-usr` writes its store as a single line, so pointing it at the shared file would destroy every unrelated credential on each dispatch. If you previously relied on `~/.git-credentials` holding the agent's GitLab entry, note that `glab-usr` no longer reads or writes that file; other hosts in it keep working normally.

**One-time cleanup on an existing deployment.** Because earlier versions wrote to `~/.git-credentials`, that file may still contain a line for your GitLab host holding a **live agent token**. Nothing reads it any more, and the scoped reset described below keeps it from answering, but it is credential material sitting at rest indefinitely. Delete just that line:

```bash
docker compose exec -u appuser app sed -i '/git\.example\.com/d' /home/appuser/.git-credentials
```

Leave the other hosts' lines alone. The line does not record which agent wrote it, so if you cannot tell, revoke the token in GitLab (**Settings -> Access Tokens** on the agent accounts) and reissue. This is left as a manual step on purpose: automatically editing a shared file this project no longer owns is the same class of action that caused the destructive fallback in the first place.

### Why the empty helper reset is required

Git credential helpers **accumulate** across configuration scopes and are queried in order: system, then global, then repository-local. Setting a repository-local `credential.helper` therefore does not displace a global one -- it appends to the list, and the global helper still answers first.

In a shared multi-agent checkout that is a live misattribution bug. If the operator's global config carries any credential helper that has an entry for the GitLab host, that entry answers every push, no matter which agent `glab-usr` just authenticated. The failure is silent and easy to miss: `glab-usr` reports success, the local credential file is correct, and commits carry the right author -- but GitLab records the push and the pipeline trigger under the *other* agent. This was observed in production on `feature/issue-25-gitlab-ci`, where Codex-authored commits produced Grok push events.

`glab-usr` fixes it by writing a **host-scoped** key whose first value is empty:

```text
credential.https://git.example.com.helper =                                  # empty = list reset
credential.https://git.example.com.helper = store --file '<store path>'
```

Git treats an empty helper value as a reset of the helper list, so everything inherited from broader scopes is discarded and the selected agent's store is the only responder. Because the key is scoped to one host, helpers for every other host -- a credential manager, a GitHub token -- keep working untouched.

Two properties of the key matter if you ever write one by hand:

- **The protocol must match the request.** A `credential.https://...` key does not apply to an `http://` credential request, so the key is built from `GLAB_PROTOCOL`.
- **The port is part of the key.** A key without a port does not match a request for `host:8080`, so `GLAB_HOST` is used verbatim, port included. (The key uses `GLAB_HOST`, not `GLAB_API_HOST`: git authenticates against the remote URL's host.)

### Consequence: no fallback for the GitLab host

The reset is deliberate, and it removes the safety net. Once it is in place, a missing or stale credential store for `GLAB_HOST` is a **hard authentication failure** rather than a quiet fallthrough to whatever other helper happens to be configured. For a multi-agent dispatcher that is the correct trade -- a failed push is recoverable, a push attributed to the wrong agent corrupts the audit history and may apply broader permissions than intended. For the same reason, a failure to write the store or configure the helper aborts `glab-usr` instead of warning and reporting success.

`glab-usr` re-applies this configuration on every dispatch and the operation is idempotent, so the helper list does not grow across runs. Switching agents in a checkout simply rewrites the store and the scoped key. `gitlab-connect` invokes `glab-usr` automatically and inherits all of this.

### Repository config outranks global, and what that means

The reset works in both directions: a *repository-scoped* key discards the *global* one, because repository config is read last. So authenticating globally does not override the helper a checkout already carries.

Two consequences in this deployment:

- **Read-only dispatches.** `docker-compose.yml` bind-mounts the same project tree twice -- read-write at `/work/projects` and read-only at `/work/projects-ro` -- so the read-only view reads the very `.git/config` a writable dispatch wrote. A read-only dispatch takes the global path, writes its own store, and is then outranked in that checkout: git resolves the *previous* agent's credential there. `glab-usr` warns when it detects this, naming the repository and the helper that will answer. It is not fatal, because the read-only path cannot push and `glab`/`gitlab-connect` authenticate from `GLAB_TOKEN` rather than the git helper -- failing would break every read-only dispatch over an identity they never use.
- **Anything that authenticates outside a repository and then operates inside one.** The branch pruner does exactly this (one `glab-usr` call per pass, then `fetch` and `push --delete` per project). It must override the helper **on the git command line**, which is the only scope above repository config:

  ```bash
  git -c 'credential.https://git.example.com.helper=' \
      -c "credential.https://git.example.com.helper=store --file '<store>'" \
      push origin --delete <branch>
  ```

  Re-authenticating with the repository as the working directory would also work, and is wrong: it rewrites `.git/config` and `.git/credentials` in a live checkout. `git_auth_lock` is released before an agent's CLI starts, so a background service can legitimately take that lock and repoint credentials underneath an agent that is running for minutes -- reintroducing the misattribution on the *active* dispatch path. The `-c` form touches no disk state.

  To keep the key construction in one place, `glab-usr` publishes what it configured on stderr:

  ```text
  [glab-usr] CREDENTIAL-CONFIG scope=global key=credential.https://git.example.com.helper helper=store --file '<store>'
  ```

  Callers should parse that line rather than rebuild the key, because a mismatched key fails *silently* -- it simply does not match the request, and the checkout's own helper answers as before. `app/services/branch_pruning.py` consumes it and refuses to prune if it is missing. The line contains a path, never token material, and goes to stderr so stdout stays unchanged for existing callers.

### Two scoping limits worth knowing

- **The global reset is host-wide and persistent.** On the fallback path the reset is written to `~/.gitconfig`, so it applies to `GLAB_HOST` in *every* checkout under that home, not only the one being dispatched -- and it stays there. Inside the container that is intended and harmless. On a workstation, where `glab-usr` can also be run by hand, a single invocation permanently removes your own credential helper for that host across all your checkouts of it. Restore it with `git config --global --unset-all 'credential.<protocol>://<host>.helper'`.
- **The store is per host, not per agent.** Both scopes hold one entry for `GLAB_HOST` at a time, so correctness depends on dispatches being serialized -- which they are: the trigger queue runs a single FIFO worker. Running `docker compose exec -u appuser app glab-usr <other-agent>` by hand while an agent is mid-dispatch rewrites the store underneath it and reintroduces the misattribution for the remainder of that run. Avoid manual `glab-usr` calls against a busy container; use `glab auth status` to inspect instead.

## First-Run Validation

- Send a test webhook from GitLab (`Test` button) and confirm:
  - Webhook is accepted (`200 OK`).
  - A trigger appears in `run-logs/`.
  - Live dashboard (if enabled) shows the event stream.
- Inspect `config/routes.yaml` to ensure incoming events map to the expected agents. The shipped file uses `your-username` as a placeholder in the `author` match fields. For local use, copy it to `config/routes.local.yaml`, replace `your-username` with your GitLab username (or a list of usernames such as `["alice", "bob"]` to authorize multiple operators on the same route), and set `ROUTE_CONFIG_PATH=config/routes.local.yaml` in your `.env`.
- Document any environment-specific adjustments for future agents in the project memory file (e.g., `CLAUDE.md` / `AGENTS.md`).
