<!--
Robot Dev Team Project
File: docs/ADDING_AN_AGENT.md
Description: Step-by-step guide for onboarding a custom agent CLI (BYOA).
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Adding a Custom Agent

This guide covers onboarding a new agent CLI into the Robot Dev Team framework.
The system uses a **naming convention** to derive environment variables, token
files, and identity settings from the agent name, so adding a new agent requires
**zero Python or shell code changes**.

## Prerequisites

- A CLI tool that accepts a prompt via **stdin** and exits with code 0 on
  success.
- A dedicated GitLab user account for the agent (for authentication and
  assignment workflows).
- A GitLab Personal Access Token (PAT) with `api` scope for the agent account.

## Agent Naming Constraints

Agent names must use **lowercase letters, digits, and hyphens** only
(e.g., `qwen-code`, `my-agent-2`). Underscores in agent names are **not
supported** -- the system normalises names by converting underscores to
hyphens internally (for token file paths, directory names, etc.), which can
cause mismatches if the original name contains underscores.

Good: `qwen-code`, `deepseek`, `my-agent`
Bad: `qwen_code`, `My_Agent`

---

## Step 1: Environment Variables

All variables follow a naming convention based on the agent name.  For an agent
named `<agent>`, the uppercased form with hyphens replaced by underscores
becomes `<AGENT>`.

| Variable | Purpose | Example for `qwen-code` |
|---|---|---|
| `<AGENT>_AGENT_GITLAB_TOKEN` | GitLab PAT for the agent account | `QWEN_CODE_AGENT_GITLAB_TOKEN` |
| `<AGENT>_AGENT_GIT_NAME` | Git commit author name | `QWEN_CODE_AGENT_GIT_NAME` |
| `<AGENT>_AGENT_GIT_EMAIL` | Git commit author email | `QWEN_CODE_AGENT_GIT_EMAIL` |
| `<AGENT>_MODEL` | Model identifier for `${<AGENT>_MODEL}` in routes | `QWEN_CODE_MODEL` |

Add these to your `.env` file (see `.env.example` for reference).

If `<AGENT>_AGENT_GIT_NAME` or `<AGENT>_AGENT_GIT_EMAIL` are not set, defaults
are derived automatically (`<Agent> Agent` / `<agent>@example.com`).

---

## Step 2: Route Configuration

Add entries for the new agent in `config/routes.yaml` (or your local override
file).  Each route specifies the CLI command and its arguments:

```yaml
routes:
  - name: mention-qwen-code
    access: readonly
    match:
      event: "Note Hook"
      action: "create"
      mentions: ["qwen-code"]
    agents:
      - agent: "qwen-code"
        task: "note_followup"
        prompt: "note_followup.txt"
        options:
          command: "qwen-code"
          args: ["--model", "${QWEN_CODE_MODEL}", "-p"]
```

### Model placeholder resolution

The `${<AGENT>_MODEL}` syntax in the `args` list is resolved at load time from
environment variables.  Set `QWEN_CODE_MODEL=qwen-coder-latest` in `.env` and
the placeholder will be replaced automatically.

### CLI contract

The framework invokes the agent CLI as:

```
<command> [args...] < prompt.txt
```

The prompt text is piped to **stdin**.  The CLI must:

1. Read the prompt from stdin.
2. Perform its work (read/write files, call APIs, etc.).
3. Write progress to **stdout** periodically (used by the inactivity
   watchdog).  The watchdog resets its timer only on stdout output --
   stderr is captured and logged but does **not** reset the timer.  If
   the CLI produces no stdout for `AGENT_MAX_INACTIVITY_SECONDS`
   (default: **900 seconds / 15 minutes**), the process is terminated.
   The hard wall-clock limit is `AGENT_MAX_WALL_CLOCK_SECONDS` (default:
   **7200 seconds / 2 hours**).
4. Exit with code **0** on success, non-zero on failure.

If the CLI does not read from stdin natively, wrap it in a shell script that
adapts the interface.

---

## Step 3: CLI Installation (Docker)

The container entrypoint runs all scripts matching `scripts/install-*.sh` at
startup.  To install your agent CLI in the Docker image:

1. Create `scripts/install-<agent>.sh` (e.g., `scripts/install-qwen-code.sh`).
2. Make it executable: `chmod +x scripts/install-<agent>.sh`.

Example:

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "[install-qwen-code] Installing Qwen Code CLI..."
if ! pip install qwen-code-cli; then
  echo "[install-qwen-code] WARN: failed to install; continuing without it" >&2
  exit 0
fi
if command -v qwen-code >/dev/null; then
  echo "[install-qwen-code] Installed: $(qwen-code --version 2>/dev/null || echo unknown)"
fi
```

The existing `scripts/install-claude.sh`, `scripts/install-codex.sh`, and
`scripts/install-gemini.sh` serve as reference implementations.

> **Note**: Install scripts run in a **subshell** (`bash "$installer"`), so
> they cannot export environment variables for the main entrypoint process.
> Place binaries in `$HOME/.local/bin` (already on `PATH`) and keep any
> required environment setup in the `.env` file instead.

> **Tip**: If you prefer not to install the CLI at startup, you can bake it into
> a custom Docker image by extending the `Dockerfile` instead.

---

## Step 4: Docker Compose (if needed)

If the agent CLI requires a configuration directory bind-mounted from the host,
add a volume entry in `docker-compose.yml`:

```yaml
volumes:
  - ${HOME}/.qwen-code:/home/appuser/.qwen-code:ro
```

---

## Step 5: GitLab Account Setup

1. Create a GitLab user for the agent (e.g., `qwen-code`).

   > **Tip (self-managed GitLab):** You do not need to set a password or sign
   > in as the agent account to generate a PAT. Instead, use admin
   > impersonation:
   > 1. Sign in as an admin and go to **Admin Area > Users > (agent user) >
   >    Impersonate**.
   > 2. Navigate to **User Settings > Access Tokens** and create a PAT with
   >    `api` scope.
   > 3. Copy the token, then click **Stop impersonating** to return to your
   >    admin session.

2. Generate a PAT with `api` scope (see tip above).
3. Set the token in `.env` as `QWEN_CODE_AGENT_GITLAB_TOKEN=glpat-...`.
4. Add the agent user to the GitLab project(s) with at least **Developer**
   role.

---

## Step 6: Update ALL_MENTIONS_AGENTS (optional)

If you want the new agent to respond to `@all` or `@agents` mentions, add it
to `ALL_MENTIONS_AGENTS` in `.env`:

```
ALL_MENTIONS_AGENTS=claude,gemini,codex,qwen-code
```

---

## Step 7: Testing

1. **Local verification**: Run the agent CLI manually with a test prompt to
   verify it accepts stdin and exits cleanly.

2. **Integration test**: Trigger a webhook event (e.g., mention the agent in a
   GitLab issue comment) and check `run-logs/` for the captured prompt and
   output.

3. **Unit tests**: If you add custom route rules, verify them with:
   ```bash
   pytest tests/test_routes.py -v
   ```

---

## Summary Checklist

| Step | File(s) | Required? |
|---|---|---|
| Environment variables | `.env` | Yes |
| Route entries | `config/routes.yaml` | Yes |
| Install script | `scripts/install-<agent>.sh` | If using Docker |
| Docker volumes | `docker-compose.yml` | If CLI needs host config |
| GitLab account | GitLab UI | Yes |
| ALL_MENTIONS_AGENTS | `.env` | Optional |

No Python code, shell script, or Dockerfile modifications are required.

---

## Removing an Agent

To omit one of the three default agents (Claude, Gemini, Codex) -- or any
previously added custom agent -- follow these steps:

1. **Remove route entries**: Delete or comment out all routes for that agent in
   `config/routes.yaml` (or your `routes.local.yaml` override). This includes
   mention routes, assign routes, and any other entries pointing at that agent.
   Once no route references the agent, it will never be invoked.

2. **Remove from `ALL_MENTIONS_AGENTS`**: If the agent appears in
   `ALL_MENTIONS_AGENTS` in `.env`, remove it. Otherwise `@all` / `@agents`
   mentions will still attempt to enqueue it even with no matching routes.

3. **Remove model/token env vars** (optional): If no remaining route references
   `${<AGENT>_MODEL}`, the model variable is no longer required. The
   `<AGENT>_AGENT_GITLAB_TOKEN` and git identity variables can also be removed
   safely once no route invokes the agent.

4. **Remove Docker volume mounts** (optional): The config-directory bind mounts
   in `docker-compose.yml` (e.g., `~/.codex:/home/appuser/.codex`) are
   unconditional. Removing unused mounts avoids potential startup issues if the
   host directory does not exist and gives a leaner container configuration.

5. **Remove install script** (optional): The entrypoint runs every
   `scripts/install-*.sh` unconditionally at container startup. The install
   scripts warn-and-continue on failure, so leaving them is harmless, but
   removing `scripts/install-<agent>.sh` avoids unnecessary install attempts
   and speeds up startup.

> **Summary**: Agents are opt-in at the routing layer. Install scripts and
> Docker Compose volume mounts are currently opt-out and must be removed
> separately if you want a fully lean startup footprint.
