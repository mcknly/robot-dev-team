<!--
Robot Dev Team Project
File: docs/ROUTES.md
Description: Routing configuration reference.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Routing Configuration Reference

This document explains the structure of `config/routes.yaml`, the matching logic, and the available agent invocation options. Use it as the canonical reference when updating routing rules for the webhook automation service.

## File Overview
- The file contains a single top-level key, `routes`, mapped to an ordered list of rule objects.
- Rules are evaluated sequentially. The first rule whose `match` block fits the incoming event determines the agents to run.
- Each rule may trigger one or more agent task definitions under the `agents` list.
- Each rule specifies an `access` mode (`readonly` or `readwrite`) that determines which project mount the agent uses. Default is `readonly`.
- When a webhook includes several user mentions, rules that declare exactly one `mentions` value are evaluated once per username so that each mention can dispatch its own agent set.

```yaml
routes:
  - name: my-rule
    access: readonly  # or readwrite
    match:
      event: "Merge Request Hook"
      action: "open"
      author: "alice"
      labels: ["backend"]
    agents:
      - agent: "claude"
        task: "merge_request_review"
        prompt: "merge_request_review.txt"
        options:
          command: "claude"
          args: ["run"]
          env:
            EXTRA_VAR: "value"
```

## Match Block
`match` filters incoming events before any agents are scheduled. All fields are optional unless noted otherwise.

| Key      | Type            | Description |
|----------|-----------------|-------------|
| `event`  | string (required) | Name of the GitLab webhook. Use the exact header value supplied by GitLab (e.g., `"Merge Request Hook"`, `"Issue Hook"`, `"Note Hook"`). |
| `action` | string (optional) | Matches `object_attributes.action`, `object_attributes.state`, or `event_type` depending on the payload. Common examples: `"open"`, `"merge"`, `"update"`, `"comment"`. |
| `author` | string (optional) | Compares to the username derived from the payload (`payload["user"]["username"]` or `payload["user_username"]`). |
| `labels` | list[string] (optional) | Requires every listed label to appear in the event. The loader accepts either a list or a single string. |
| `mentions` | list[string] (optional) | Requires every listed username to be mentioned in the payload (comment text or structured mentions). Use GitLab usernames without the leading `@`. |
| `assignees` | list[string] (optional) | Requires every listed username to appear in the issue/MR assignees list. Case-insensitive matching. Use for triggering work when agents are assigned via GitLab's `/assign` quick action. |
| `pattern` | string (optional) | A regular expression to match against the comment body text. The pattern is compiled using Python's `re` module and matched using `search()`. Use for detecting specific commands or content in comments. Patterns anchored with `^` match from the start of the entire comment body, not individual lines. |

**Notes**
- If multiple rules match, only the first one is used for the current evaluation. Multi-mention payloads re-run the resolver per username for single-mention rules, so ordering still matters within each pass.
- Omitting a field means "no constraint." For example, a rule without `labels` matches any label set.
- The `pattern` field is matched against the comment body (`object_attributes.note`, `object_attributes.description`, or `object_attributes.body`). If no body text is available, pattern-based routes will not match.
- Pattern matching uses Python's `re.search()`, which finds a match anywhere in the body. To require a command at the start of the comment, anchor the pattern with `^` (e.g., `^\s*/assign`). Note that `^` matches the start of the entire comment body, not individual lines, so commands preceded by other text will not match anchored patterns.
- The `assignees` field extracts usernames from `payload.assignees` and `payload.changes.assignees.current`. This is the preferred method for detecting GitLab `/assign` quick actions.

## Access Mode
The `access` field controls which project mount the agent uses:

| Value | Description |
|-------|-------------|
| `readonly` | Agent runs under the read-only mount (`/work/projects-ro/<namespace>/<project>`). File writes fail with "Read-only file system" errors. This is the **default**. |
| `readwrite` | Agent runs under the read-write mount (`/work/projects/<namespace>/<project>`). File writes are permitted. Use for routes that need to modify code. |

The dual-mount system enforces read-only access at the filesystem level, providing a reliable barrier against unintended file modifications regardless of agent CLI configuration or prompt instructions.

**Example:**
```yaml
routes:
  - name: issue-review
    access: readonly   # Analysis only, no file edits
    match:
      event: "Issue Hook"
      action: "open"
    agents:
      - agent: "claude"
        task: "issue_review"

  - name: issue-work
    access: readwrite  # Code modification permitted
    match:
      event: "Note Hook"
      action: "create"
      labels: ["work::claude"]
      mentions: ["claude"]
    agents:
      - agent: "claude"
        task: "issue_work"
```

## Agents List
Each item in `agents` describes how to invoke an external CLI against the rendered prompt.

| Key       | Type             | Description |
|-----------|------------------|-------------|
| `agent`   | string (required) | Logical name of the agent (e.g., `"claude"`, `"gemini"`, `"codex"`). Also used in log filenames. |
| `task`    | string (required) | Semantic task identifier. Defaults the prompt filename to `<task>.txt` if `prompt` is omitted. |
| `prompt`  | string (optional) | Template filename relative to `prompts/`. If omitted, falls back to `<task>.txt`. |
| `options` | map (optional)    | Execution overrides; see below. |

### Agent Options
The `options` map currently supports three keys:

| Key       | Type              | Description |
|-----------|-------------------|-------------|
| `command` | string (optional) | Binary to execute. Defaults to the `agent` value. |
| `args`    | list[string] (optional) | Command-line arguments passed after the command. Defaults to an empty list. |
| `env`     | map[string,string] (optional) | Additional environment variables merged into the subprocess environment. |

Additional keys are ignored by the current launcher, allowing future extensions without breaking existing rules.

#### Non-Interactive Mode Flags
All agent CLIs **must** be configured to run in non-interactive (headless) mode so they exit after processing stdin input rather than waiting for further commands. The required flags per agent:

| Agent  | Flag       | Notes |
|--------|------------|-------|
| Claude | `-p`       | Reads prompt from stdin and exits. No value needed. |
| Gemini | `-p ""`    | Activates headless mode. The empty string is appended to stdin input. |
| Codex  | `exec`     | The `exec` subcommand is inherently non-interactive. |

Omitting these flags causes the agent to enter interactive mode and wait indefinitely for user input, eventually hitting the inactivity timeout (`AGENT_MAX_INACTIVITY_SECONDS`) or wall-clock limit (`AGENT_MAX_WALL_CLOCK_SECONDS`).

#### Model Argument Variables
`routes.yaml` supports environment-backed model identifiers so operators can update them in `.env` instead of hunting down every route entry. When the loader encounters `--model` followed by a string that contains the `${VAR}` syntax (for example `${CLAUDE_MODEL}`, `${GEMINI_MODEL}`, or `${CODEX_MODEL}`), it substitutes the value from the corresponding environment variable.

- If the `${}` syntax is omitted, the literal string is preserved, which makes per-route overrides straightforward.
- If the placeholder is present but not defined in the environment, the service raises a `ValueError` during startup to surface the misconfiguration immediately.

```yaml
options:
  args: ["-p", "--model", "${CLAUDE_MODEL}", "--dangerously-skip-permissions"]
```

## Prompt Variables
Prompt templates are rendered via `string.Template` with the following substitution keys:
- `${PROJECT}` — `project.path_with_namespace`
- `${TITLE}` — `object_attributes.title`
- `${DESCRIPTION}` — `object_attributes.description`
- `${AUTHOR}` — derived user name
- `${URL}` — `object_attributes.url` or `web_url`
- `${EXTRA}` — JSON string from GitLab enrichment (`glab` queries); populated only when `GLAB_TOKEN` is configured (see `docs/ENVIRONMENT.md`)
- `${JSON}` — Pretty-printed full webhook payload

## Example Patterns
### Merge Request Gate with Label Conditioning
```yaml
- name: high-priority-review
  match:
    event: "Merge Request Hook"
    action: "open"
    labels: ["priority::high"]
  agents:
    - agent: "claude"
      task: "merge_request_review"
      options:
        args: ["review", "--blocking"]
```

### Multi-Agent Fan-Out
```yaml
- name: comment-fanout
  match:
    event: "Note Hook"
    action: "comment"
  agents:
    - agent: "claude"
      task: "note_analysis"
    - agent: "codex"
      task: "note_followup"
      options:
        env:
          FOLLOWUP_STRATEGY: "summarize"
```

### Mention-Triggered Routing
```yaml
- name: claude-mentioned
  match:
    event: "Note Hook"
    action: "comment"
    mentions: ["claude-bot"]
  agents:
    - agent: "claude"
      task: "note_followup"
```

### Assignee-Based Work Routing
Use the `assignees` field to trigger work when an agent is assigned to an issue or MR via GitLab's `/assign` quick action:

```yaml
# Issue assignment routes - triggered by /assign @agent
- name: assign-issue-claude
  access: readwrite
  match:
    event: "Issue Hook"
    action: "update"
    author: "authorized-user"
    assignees: ["claude"]
  agents:
    - agent: "claude"
      task: "assign_work"
      prompt: "assign_work.txt"

# MR assignment routes - same pattern for merge requests
- name: assign-mr-claude
  access: readwrite
  match:
    event: "Merge Request Hook"
    action: "update"
    author: "authorized-user"
    assignees: ["claude"]
  agents:
    - agent: "claude"
      task: "assign_work"
      prompt: "assign_work.txt"
```

**Important:** GitLab's `/assign` quick action sends an Issue/MR Hook with `action: update`, NOT a Note Hook. The quick action text is consumed server-side and never appears in webhook payloads. Use the `assignees` field to detect agent assignments.

**Assignment Route Design Guidelines:**
- Match on `Issue Hook` or `Merge Request Hook` with `action: update`
- Use `assignees: ["agent-username"]` to detect when the agent is assigned
- Keep the `author` field explicit to prevent self-trigger loops
- Place assignment routes before fallback update routes so they take precedence

### Pattern-Based Routing (Alternative)
The `pattern` field can detect specific text patterns in comments for other use cases:

```yaml
- name: run-tests-command
  match:
    event: "Note Hook"
    action: "create"
    pattern: "^/run-tests\\b"
  agents:
    - agent: "claude"
      task: "run_tests"
```

**Pattern Design Guidelines:**
- Patterns anchored with `^` require the command at the very start of the comment body. Commands preceded by other text (e.g., "please do this\n/run-tests") will NOT match anchored patterns.
- Use `\s*` after `^` to allow optional leading whitespace (e.g., `^\s*/run-tests`).
- Use `\b` word boundaries to prevent partial matches (e.g., `/run-tests` should not match `/run-tests-all`).

**Note:** Pattern matching does NOT work for GitLab quick actions like `/assign` because those commands are processed server-side and don't appear in webhook payloads.

### Fallback Rule
Place catch-all routes last to ensure specific rules fire first:
```yaml
- name: default-issues
  match:
    event: "Issue Hook"
  agents:
    - agent: "codex"
      task: "issue_triage"
```

## Operational Tips
- Keep the file under version control; changes require a service restart unless `DEBUG_RELOAD_ROUTES=true`.
- Validate YAML syntax before deploying (`python -c "import yaml, sys; yaml.safe_load(open('config/routes.yaml'))"`).
- Test new rules with sample payloads using the unit tests or the GitLab webhook replay tool.
- When referencing new prompts, add the template in `prompts/` and commit it alongside the routing change.
- Projects are resolved from the mounted `projects/` directory tree. Ensure project directories follow the `<namespace>/<project-name>` structure matching your GitLab namespaces, or enable `ENABLE_AUTO_CLONE=true` for automatic cloning.

Refer back to this guide whenever you need to extend routing logic or onboard new agent workflows.
