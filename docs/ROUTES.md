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

## Rule-Level Keys
In addition to `match`, `access`, and `agents`, each route accepts a few top-level keys that affect dispatch.

| Key                       | Type            | Default     | Description |
|---------------------------|-----------------|-------------|-------------|
| `max_wall_clock_seconds`  | int (optional)  | (env var)   | Hard upper-bound run duration for each agent in this route. Falls back to `AGENT_MAX_WALL_CLOCK_SECONDS`. |
| `max_inactivity_seconds`  | int (optional)  | (env var)   | Watchdog timeout; resets on stdout/stderr output. Falls back to `AGENT_MAX_INACTIVITY_SECONDS`. |
| `randomize`               | bool (optional) | `false`     | Shuffle the order of the `agents` list per matched event. No effect on routes with fewer than 2 agents (warns at load time). The shuffle is applied to a copy of the list, so the underlying configuration order is preserved and the per-event dispatch order is captured in the route-match log line. |

**Notes on `randomize`**
- Only the *order* is randomized -- the set of agents that fire is unchanged.
- The flag is only meaningful on multi-agent routes (e.g. `issue-triage`, `default-merge-request`). Setting it on a single-agent route is a no-op and logs a warning at load.
- For multi-mention webhook events, the resolver runs once per mention against single-mention rules. `randomize` therefore only takes effect on the multi-agent base match -- per-mention rules dispatch the single mentioned agent.
- Non-boolean values (including YAML truthy strings like `"yes"`) are rejected at load time with a clear error.

**Two independent randomization layers**

`randomize` (above) and the global `RANDOMIZE_ALL_MENTIONS` env var operate at different levels and do not interact:

- Route-level **`randomize`** shuffles the order of the `agents` list *within* a single matched multi-agent route.
- Env-level **`RANDOMIZE_ALL_MENTIONS`** (default `true`) shuffles the dispatch order of the *split per-mention work items* produced by `@all`/`@agents` expansion. It applies **only** to alias-expanded triggers -- explicitly listing agents (e.g. `@claude @gemini @codex`) preserves the author-specified order so call sequence can be pinned intentionally. The post-shuffle order is logged at INFO for incident replay.

**Author awareness of `@all` expansion**

`@all`/`@agents` expansion is author-aware: a comment authored by a known agent (a username in `ALL_MENTIONS_AGENTS`) does **not** expand the alias, preventing an agent from fanning out to the whole roster (including itself). Mentions are also parsed with markdown code spans stripped, so a literal `` `@all` `` written inside backticks while *discussing* the alias does not trigger dispatch. Code-span stripping covers fenced blocks (both ```` ``` ```` and `~~~`), paired-backtick inline spans of any length (e.g. `` `@all` `` or `` ``@all`` ``), blockquoted lines, and indented code blocks. Blockquote coverage includes `>` prefixes with up to three leading spaces, nested `> >` quotes, lazy continuation lines (a bare mention line immediately following a `>` line with no blank separator, which GitLab still renders as quoted), and GitLab multiline `>>> ... >>>` quotes (an unclosed `>>>` region strips to end-of-text). Indented code covers four-space (or up-to-three-space + tab, per CommonMark tab stops) indentation; every qualifying indented line is stripped rather than trying to tell a code block apart from a paragraph or list continuation, since a per-line blank/not-blank check cannot model block containment and under-strips indented code after a heading or blockquote. The stripping is a deliberately **conservative, over-strip-biased subset** rather than a full GitLab-Flavored-Markdown parser: under-stripping a quoted `@all` re-fans it out to the whole roster (the incident this guards against), whereas over-stripping merely drops a recoverable live mention, so ambiguous container shapes strip -- including indented list-continuation mentions, which are dropped as the safe trade-off. Mention parsing also requires a word boundary before `@`, so embedded forms like `support@all` (email local-part) and `https://x/@all` (URL path) do not parse as live mentions. Route-level `author:` filtering remains the final authorization check.

## Match Block
`match` filters incoming events before any agents are scheduled. All fields are optional unless noted otherwise.

| Key      | Type            | Description |
|----------|-----------------|-------------|
| `event`  | string (required) | Name of the GitLab webhook. Use the exact header value supplied by GitLab (e.g., `"Merge Request Hook"`, `"Issue Hook"`, `"Note Hook"`). |
| `action` | string or list[string] (optional) | Matches `object_attributes.action`, `object_attributes.state`, or `event_type` depending on the payload. Common examples: `"open"`, `"merge"`, `"update"`, `"comment"`. Accepts either a single action or a list; the list form matches when the event action equals **any** listed value (e.g. `action: ["open", "update"]` fires on both issue creation and updates). An empty list or an omitted field means "no constraint" (matches any action). Empty strings -- scalar `""` or empty list entries -- are rejected at load time as likely typos. |
| `author` | string or list[string] (optional) | Compares to the username derived from the payload (`payload["user"]["username"]` or `payload["user_username"]`). Accepts either a single username or a list of usernames; the rule matches when the event author equals (single form) or appears in (list form) the configured value. Matching is case-insensitive in both forms (entries are lowercased at load time, so a configured `"Cavin"` matches an event from `cavin`). An empty list is treated as "no constraint" (same as omitting the field). Empty strings -- scalar `""` or empty list entries -- are rejected at load time because they almost always indicate a config typo; omit the field instead to allow any author. |
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
| Gemini | `--model ${GEMINI_MODEL} --dangerously-skip-permissions -p ${PROMPT}` | Antigravity CLI (binary: `agy`, >= 1.1.5 for the model slugs shipped in `.env.example`; >= 1.1.1 otherwise). **`agy` never reads stdin**: `-p` / `--print` takes the prompt as the flag's *value*, so the gemini routes pass the `${PROMPT}` placeholder (see below) instead of relying on the stdin handoff the other agents use. `--dangerously-skip-permissions` auto-approves all tool calls (verbatim Claude flag name, which `agy` also accepts); `--yolo` / `--skip-trust` from the old Gemini CLI do not exist on `agy`. `--model` takes a stable model slug from `agy models` (e.g. `gemini-3.6-flash-high`), which encodes the reasoning-effort tier and survives as a single argv element without quoting. Slugs need `agy` >= 1.1.5; the older friendly form (`Gemini 3.6 Flash (High)`, whose spaces and parentheses are part of the identifier) is still accepted for backward compatibility. List the valid names with `docker compose exec -u appuser app agy models`; an unrecognized value aborts the run with exit 1 rather than falling back. |
| Codex  | `exec`     | The `exec` subcommand is inherently non-interactive. |
| Grok  | `--model ${GROK_MODEL} --no-auto-update --always-approve -p ${PROMPT}` | Grok Build (binary: `grok`), optional and disabled by default. `--no-auto-update` is **not** a version pin (`scripts/install-grok.sh` fetches latest every boot) -- it stops the running agent from updating itself into the bind-mounted `~/.grok`, where it would leave a ~150 MB binary and repoint the symlink the operator's *host* grok resolves through. **`grok` never reads stdin**: `-p` / `--single` takes the prompt as the flag's *value*, so the grok routes pass the `${PROMPT}` placeholder, like `agy`. Its `--prompt-file` flag accepts `/dev/stdin` and looks like a way back onto the stdin path -- it works from a shell but fails under the dispatcher with `ENXIO`, because grok re-opens fd 0 by path and the dispatcher's pipe has no writer left (see `docs/ADDING_AN_AGENT.md`). `grok agent stdio` is a JSON-RPC server for the SDKs, not a prompt-on-stdin mode. `--always-approve` auto-approves tool calls. In the default `plain` output format grok prints its reply only at the end of the run, so the routes raise `max_inactivity_seconds` to 1800; `--output-format streaming-json` emits continuous token events instead. Auth comes from the mounted `~/.grok`, not an env key. |
| OpenCode | `run --format json --auto` | Logical agent named per model (e.g. `opencode-kimi`) over the shared `opencode` binary. `run` reads the prompt from stdin and exits 0; `--format json` streams a clean, ANSI-free event log to stdout (feeds the inactivity watchdog); `--auto` auto-approves tool calls (OpenCode's equivalent of `--yolo`). Use the **same flags for read-only and read-write** routes -- read-only is enforced by the mount, not by OpenCode's restrictive `plan` agent, since even review routes need to write temp files and call `glab`. Provider auth comes from the mounted host config, not an env key. |

Omitting these flags causes the agent to enter interactive mode and wait indefinitely for user input, eventually hitting the inactivity timeout (`AGENT_MAX_INACTIVITY_SECONDS`) or wall-clock limit (`AGENT_MAX_WALL_CLOCK_SECONDS`).

#### Model Argument Variables
`routes.yaml` supports environment-backed model identifiers so operators can update them in `.env` instead of hunting down every route entry. When the loader encounters `--model` followed by a string that contains the `${VAR}` syntax (for example `${CLAUDE_MODEL}`, `${CODEX_MODEL}`, or `${OPENCODE_KIMI_MODEL}`), it substitutes the value from the corresponding environment variable. Only variables whose names end in `_MODEL` are eligible, so provider-specific instances such as `${OPENCODE_KIMI_MODEL}` (slug `openrouter/moonshotai/kimi-k2.6`) work without exposing secrets.

- If the `${}` syntax is omitted, the literal string is preserved, which makes per-route overrides straightforward.
- If the placeholder is present but not defined in the environment, the service raises a `ValueError` during startup to surface the misconfiguration immediately.
- If the resolved model value is empty, startup also fails. This avoids sending `--model ""` to CLIs such as `agy`, which would otherwise silently fall back to another model.

```yaml
options:
  args: ["-p", "--model", "${CLAUDE_MODEL}", "--dangerously-skip-permissions"]
```

#### The `${PROMPT}` Argument Placeholder
By default the rendered prompt is written to the agent process's **stdin**, which is what `claude`, `codex`, and `opencode` expect. Some CLIs have no stdin mode at all and accept the prompt only as a command-line value -- Antigravity (`agy`) and Grok (`grok`) are the shipped examples, where `--print` / `-p` takes the prompt as its flag value.

For those, put the literal token `${PROMPT}` in the route's `args`. At dispatch time the service replaces every `${PROMPT}` element with the rendered prompt and **skips the stdin write** for that agent, so the prompt is never delivered twice:

```yaml
options:
  command: "agy"
  args: ["--model", "${GEMINI_MODEL}", "--dangerously-skip-permissions", "-p", "${PROMPT}"]
```

- Unlike the `*_MODEL` placeholders, `${PROMPT}` is resolved by the **dispatcher** (`app/services/agents.py`), not the routes loader, because its value is per-event rather than per-process.
- Routes that omit the token are unaffected and keep the stdin handoff.
- The token must be its own complete argv element. Forms such as `-p=${PROMPT}` or `Context: ${PROMPT}` are rejected at load time; use `"-p", "${PROMPT}"` instead.
- Linux caps a single argv element at 128 KiB. A prompt larger than that cannot be passed this way, and dispatch fails fast with an actionable error in the run log rather than an opaque `E2BIG` at spawn time. Prefer stdin whenever the target CLI supports it.
- The run log and the `Agent started` log line record the **pre-substitution** argv, so the prompt body is not duplicated into the structured logs.
- Command-line arguments are not private: while the agent is running, the rendered prompt can be visible through process inspection (`ps` / `/proc/<pid>/cmdline`) to other processes with sufficient access. Do not template secrets into prompts consumed by argv-mode agents.

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

### Multi-Author Authorization
The `author` field accepts a list to authorize multiple GitLab users with a single route, instead of duplicating the rule per operator:

```yaml
- name: team-claude-mentions
  access: readonly
  match:
    event: "Note Hook"
    action: "create"
    author: ["cavin", "alice", "bob"]
    mentions: ["claude"]
  agents:
    - agent: "claude"
      task: "note_followup"
```

The single-string form (`author: "cavin"`) continues to work unchanged. Matching is case-insensitive in both forms.

### Assignee-Based Work Routing
Use the `assignees` field to trigger work when an agent is assigned to an issue or MR. This covers both GitLab's `/assign` quick action on an existing item (`action: update`) and populating the assignee at **issue-creation time** (`action: open`):

```yaml
# Issue assignment routes - triggered by /assign @agent OR by creating an
# issue with the agent already assigned. Matching both open and update lets a
# create-with-assignee event dispatch assign_work immediately.
- name: assign-issue-claude
  access: readwrite
  match:
    event: "Issue Hook"
    action: ["open", "update"]
    author: "authorized-user"
    assignees: ["claude"]
  agents:
    - agent: "claude"
      task: "assign_work"
      prompt: "assign_work.txt"

# MR assignment routes - same pattern for merge requests. MRs match only
# `update`: an MR opened with an assignee still goes through review.
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

**Assign on issue creation.** The shipped issue-assignment routes match `action: ["open", "update"]` and are placed **above** `issue-triage`, so an issue created with an agent already assigned dispatches that agent's read-write `assign_work` route immediately instead of running readonly triage first. Because `resolve_match` returns the first matching route, this is deliberately either/or -- a pre-assigned agent skips triage/review. This behavior is controlled by the `ENABLE_ASSIGN_ON_ISSUE_CREATION` environment variable (default `true`); setting it to `false` reverts issue-creation events to triage. See `docs/ENVIRONMENT.md`. Merge requests are not affected -- an MR opened with an assignee still goes through `default-merge-request` review.

**Assignment Route Design Guidelines:**
- Match on `Issue Hook` with `action: ["open", "update"]` (both creation and update) or `Merge Request Hook` with `action: update`
- Use `assignees: ["agent-username"]` to detect when the agent is assigned
- Keep the `author` field explicit to prevent self-trigger loops
- Place assignment routes before both `issue-triage` and any fallback update routes so they take precedence -- for assign-on-creation to win, they must precede `issue-triage`

> **Local overrides:** If you run a customized `routes.local.yaml` (via `ROUTE_CONFIG_PATH`), the shipped-file reorder and `action: ["open", "update"]` change do **not** reach your copy. Apply the same edits to your local routes to pick up assign-on-issue-creation.

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
