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

- A CLI tool that accepts a prompt via **stdin** or as a command-line value and
  exits with code 0 on success.
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

If `<AGENT>_AGENT_GIT_NAME` is not set, a title-cased default is derived
automatically (`<Agent> Agent`).

`<AGENT>_AGENT_GIT_EMAIL` is effectively required: the default
(`<agent>@example.com`) is treated as a placeholder, and `glab-usr` will
refuse to authenticate rather than land commits with placeholder
authorship. This check runs on every `glab-usr` invocation -- including
read-only paths like `gitlab-connect issue view` -- so configure a real
email for every agent in `.env`.

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

By default the prompt text is piped to **stdin**.  The CLI must:

1. Read the prompt from stdin (or accept it as a command-line value -- see
   below).
2. Perform its work (read/write files, call APIs, etc.).
3. Write progress to **stdout** periodically (used by the inactivity
   watchdog).  The watchdog resets its timer only on stdout output --
   stderr is captured and logged but does **not** reset the timer.  If
   the CLI produces no stdout for `AGENT_MAX_INACTIVITY_SECONDS`
   (default: **900 seconds / 15 minutes**), the process is terminated.
   The hard wall-clock limit is `AGENT_MAX_WALL_CLOCK_SECONDS` (default:
   **7200 seconds / 2 hours**).
4. Exit with code **0** on success, non-zero on failure.

If the CLI does not read from stdin natively but accepts the prompt as a
command-line value, you do **not** need a wrapper: put the literal `${PROMPT}`
token in the route's `args` and the dispatcher will substitute the rendered
prompt into that argv slot (and skip the stdin write) at dispatch time. The
shipped `gemini` agent works this way, since Antigravity's `-p` / `--print`
takes the prompt as the flag's value:

```yaml
args: ["--model", "${GEMINI_MODEL}", "--dangerously-skip-permissions", "-p", "${PROMPT}"]
```

The `${PROMPT}` token must be its own complete argv element (for example
`"-p", "${PROMPT}"`, not `"-p=${PROMPT}"`). Note that Linux caps a single argv
element at 128 KiB, so prefer stdin when the CLI supports it; a prompt over that
limit fails fast with a clear run-log error. Argv is also visible through
process inspection while the CLI runs, so do not put secrets in prompts for
argv-mode agents.
See "The `${PROMPT}` Argument Placeholder" in `docs/ROUTES.md`.

**Do not try to route a "prompt from file" flag at `/dev/stdin` to dodge the argv
limit.** It looks like it should work, and it *does* work when you test it by
hand -- `grok --prompt-file /dev/stdin < prompt.txt` reads the prompt and exits
0. It does not work under the dispatcher. The difference is that a shell redirect
hands the CLI a *file* on fd 0, while the dispatcher hands it a *pipe* and closes
the write end as soon as the prompt is written. A CLI that re-opens fd 0 **by
path** (`/dev/stdin` -> `/proc/self/fd/0`) is asking the kernel to reopen a pipe
that no longer has a writer, which fails with ENXIO:

```
Error: Failed to read '/dev/stdin': No such device or address (os error 6)
```

This bites only the CLIs that *re-open* stdin rather than *reading* the fd they
were given, so it cannot be predicted from `--help` -- and it does not reproduce
from a terminal. If a CLI takes the prompt as a flag value, use `${PROMPT}`. Grok
is the shipped example (see the worked example below).

Only if the CLI can accept the prompt *neither* on stdin nor as an argument do
you need a shell-script wrapper to adapt the interface. (OpenCode's
`opencode run`, for example, reads the prompt from stdin and exits 0 directly,
so it needs no wrapper -- see the worked example below.)

---

## Step 3: CLI Installation (Docker)

The container entrypoint runs the scripts matching `scripts/install-*.sh` that
the active route config actually needs.  To install your agent CLI in the
Docker image:

1. Create `scripts/install-<agent>.sh` (e.g., `scripts/install-qwen-code.sh`).
2. Declare the binary it installs with a `# provides:` line (see
   [The install gate](#the-install-gate-provides) below). **Without this line
   the script never runs.**
3. Make it executable: `chmod +x scripts/install-<agent>.sh`.

Example:

```bash
#!/usr/bin/env bash

# provides: qwen-code

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

> **Note**: `scripts/` is baked into the image, not bind-mounted, so a new or
> edited install script needs `docker compose up --build` -- a plain restart
> will not pick it up.

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

## Worked Example: OpenCode (a provider-agnostic, multi-instance harness)

OpenCode ships pre-wired in the example configs but **disabled by default** --
every piece below is present yet commented out, so nothing runs until an
operator opts in. It also illustrates two patterns worth reusing:

- **One binary, many logical agents.** A single `opencode` binary can drive any
  provider/model, so each logical agent is named after its model -- `opencode-kimi`
  (OpenRouter's Kimi K2.6), and you could add `opencode-gpt`, `opencode-gemini`,
  etc. All share **one** `scripts/install-opencode.sh`. In routes, `command` is
  the shared binary while `agent` is the per-model name:

  ```yaml
  - agent: "opencode-kimi"
    task: "issue_review"
    prompt: "issue_review.txt"
    options:
      command: "opencode"
      args: ["run", "--model", "${OPENCODE_KIMI_MODEL}", "--format", "json", "--auto"]
  ```

  The env prefix keeps the model tag **before** `_AGENT`
  (`OPENCODE_KIMI_AGENT_GITLAB_TOKEN`, `OPENCODE_KIMI_MODEL`) so the naming
  convention resolves the agent name `opencode-kimi`.

- **Same flags for read-only and read-write.** `--auto` auto-approves tool calls
  (OpenCode's equivalent of `--yolo`); read-only routes rely on the read-only
  project mount + system prompt for containment, not on a restrictive agent mode,
  because even review routes still write temp files and call `glab`.

- **Provider auth via mounted host config.** Rather than an API key in `.env`,
  the commented `docker-compose.yml` mounts the host's `~/.config/opencode` and
  `~/.local/share/opencode` (which holds `auth.json`). Run `opencode auth login`
  on the host once; if it works on the host, it works in the container -- with
  whatever provider (OpenRouter, etc.) you configured.

To enable: uncomment the `opencode-kimi` block in `.env.example`, its routes in
`config/routes.yaml`, and the two mounts in `docker-compose.yml`; set a real
token/email and `OPENCODE_KIMI_MODEL`; optionally add `opencode-kimi` to
`ALL_MENTIONS_AGENTS`.

> **Note:** while OpenCode is disabled, `scripts/install-opencode.sh` is
> skipped -- the preflight installs the `opencode` binary only once some
> `opencode-*` agent is both routed and credentialed. Enabling the routes above
> is what pulls the harness in.

---

## Worked Example: Goose (a harness driving a locally served model)

Goose also ships pre-wired and **disabled by default**. It is the reference
example for a harness pointed at a model served on the host -- llama.cpp,
Ollama, LM Studio, vLLM -- rather than a cloud provider.

The premise is the same as OpenCode's: **configure Goose on the host, confirm it
works there, and bind-mount its config.** Provider, model, credentials, and
extension *configuration* all live in `~/.config/goose` and carry into the
container. Nothing in this repo restates them -- there is deliberately no example
`goose.yaml` to drift out of sync with the config that actually works.

> **The mount carries an extension's config, not its executable.** A builtin
> (`type: platform`) extension such as `developer` works in the container
> unchanged. A `type: stdio` extension launched via `npx` -- the most common MCP
> extension form -- works on the host and **cannot** work here: the base image
> ships no Node.js (it was dropped when Gemini's harness became `agy`; the optional
> Pi harness bootstraps a user-local Node only when enabled, but exposes only
> `node` on the shared `PATH` -- its `npm`/`npx` stay in Pi's versioned Node dir --
> so it never puts a general `npx` on Goose's `PATH`). `goose_config`
> warns at boot when an enabled stdio extension names an executable that is not
> on the container's `PATH`, so this surfaces as one startup line rather than a
> failure deep inside extension startup at first dispatch. To use such an
> extension, bake its runtime into the `Dockerfile`.

- **One binary, many agents -- and the GitLab username stays clean.** Like
  OpenCode, a single `goose` binary backs every logical `goose-*` agent, so each
  is named after its model (`goose-gemma`, `goose-qwen`, ...) and resolves its own
  `GOOSE_GEMMA_AGENT_*` / `GOOSE_GEMMA_MODEL` vars.

  The GitLab account does **not** carry the harness prefix. Routes match on the
  *username* in the webhook payload (`gemma`), while the `agent:` slug only
  selects which credentials the run dispatches under (`goose-gemma`) -- the same
  split as `opencode-kimi` triggering on `@kimi`. Collapsing the two would mean
  either mentioning a user GitLab does not have, or an agent whose `*_AGENT_*`
  vars do not resolve.

- **Prompt on stdin, and no `--provider` flag.** The route is:

  ```yaml
  # match.mentions / match.assignees key off the GitLab username: ["gemma"]
  - agent: "goose-gemma"
    task: "issue_review"
    prompt: "issue_review.txt"
    options:
      command: "goose"
      args: ["run", "--no-session", "--model", "${GOOSE_GEMMA_MODEL}", "-i", "-"]
  ```

  `-i -` is what reads the prompt from stdin; `--text` takes a literal string
  and would send Goose the prompt `-`. `--no-session` keeps webhook runs from
  accumulating session state. There is **no `--provider` flag**: the loader only
  substitutes `${VAR}` in the argument immediately after `--model`, so a
  `--provider "${GOOSE_PROVIDER}"` pair would reach the CLI as that literal
  string. The provider comes from the mounted config's `active_provider`.

- **A loopback endpoint is rewritten for you.** This is the one thing that does
  not survive a bind mount. A host Goose talking to a local model has an endpoint
  like `http://127.0.0.1:10000/v1` -- correct on the host, but inside the
  container `127.0.0.1` *is the container*. So the mount lands on a **read-only
  staging path** (`~/.config/goose-host`) and the entrypoint runs
  `python -m app.goose_config`, which copies the config to `~/.config/goose`
  with loopback hosts (`127.0.0.0/8`, `localhost`, `::1`, `0.0.0.0`) redirected
  at `host.docker.internal` -- already wired up by `extra_hosts` in
  `docker-compose.yml`, so no Docker network needs joining. Only the host is
  swapped; scheme, port, and path are preserved, and remote hosts are untouched.

  Both spellings Goose accepts are covered: a full URL (a custom provider's
  `base_url`) **and** a bare `*_HOST` value with no scheme, which Goose also
  takes -- `OLLAMA_HOST: localhost:11434` is rewritten just like
  `http://localhost:11434` would be.

  The copy is why the mount is read-only: rewriting in place would break Goose
  *on the host*, where `127.0.0.1` is right. The trade-off is that a change to
  the host config needs a container restart to take effect.

  Reach for this same pattern for any harness whose host config hardcodes a
  local endpoint. Note it also means the server must be reachable from the host
  gateway -- a model server bound to `127.0.0.1` on the host will refuse the
  container's connection even after the rewrite; bind it to `0.0.0.0`.

- **No API key plumbing.** A local server usually needs none (Goose's
  `requires_auth: false`). If yours does, put it in `~/.config/goose/secrets.yaml`
  on the host -- it is copied into the container (verbatim, apart from the same
  loopback rewrite, since a self-hosted endpoint may carry credentials) and Goose
  reads it there. No `GOOSE_DISABLE_KEYRING` is needed: verified against goose
  1.41.0 in this container, where a key present only in `secrets.yaml` -- no env
  var, no keyring daemon, no dbus -- is picked up and sent.

- **Turn streaming OFF for a llama.cpp provider.** Set `"supports_streaming":
  false` in the custom provider JSON on the host. llama.cpp's server does not
  emit valid OpenAI SSE when the response contains a **tool call**: the stream
  breaks at the tool-call boundary, and Goose compounds it by deserializing the
  server's mid-stream error event as a normal chunk, dying with an opaque
  `Stream decode error: error decoding response body` that discards the server's
  actual message ([goose#8021](https://github.com/aaif-goose/goose/issues/8021),
  [llama.cpp#12601](https://github.com/ggml-org/llama.cpp/discussions/12601)).

  The symptom is nasty precisely because it looks like anything *but* what it is:
  the agent runs for minutes, the GPU is busy, llama-server logs no error and
  releases its slot normally, the watchdog never fires -- and no comment is ever
  posted. It is also intermittent, because only turns that emit a tool call trip
  it: a short "confirm you got this" reply succeeds while a real task fails.

  Non-streaming means stdout arrives once per **turn** instead of token-by-token.
  Since the watchdog resets only on stdout, `max_inactivity_seconds` must now
  cover a whole turn of local inference (not just a gap between tokens) -- 3600
  is a sane starting point.

- **`${GOOSE_GEMMA_MODEL}` may be only a label.** A llama.cpp server ignores the
  model name in the request and serves whatever is loaded, so against that
  backend the value names the run in the logs rather than selecting a model --
  which also means a `goose-gemma` agent is only really running Gemma if that is
  the model currently deployed on the server.

- **Expect long silences.** A local model can go quiet on both streams during a
  long prefill, and only *stdout* resets the inactivity watchdog. The commented
  Goose routes ship with `max_inactivity_seconds: 1800` for that reason.

To enable: uncomment the `GOOSE_*` block in `.env.example`, the Goose routes in
`config/routes.yaml`, and the mount in `docker-compose.yml`; create a `goose`
GitLab user and set a real token/email.

---

## Worked Example: Grok Build (a CLI whose headless mode looks argv-only)

Grok Build (`grok`, from xAI) ships commented out in `config/routes.yaml`,
`.env.example`, and `docker-compose.yml` (issue #18). It is the *simple* shape of
a custom agent -- one binary, one logical agent, one cloud provider -- so the only
interesting parts are the two places its CLI does not behave the way the docs
suggest.

- **The prompt goes in argv, and the obvious escape hatch is a trap.** Grok's
  headless surface is `-p / --single <PROMPT>`, which takes the prompt as the
  flag's value, so the routes use `${PROMPT}` exactly like `agy`. Grok also has a
  `--prompt-file` flag that accepts `/dev/stdin`, which *appears* to put the
  prompt back on the unbounded stdin path -- and it does work from a shell. Under
  the dispatcher it fails every time with `ENXIO`, because grok re-opens fd 0 by
  path and the dispatcher's pipe has no writer left by then (see the CLI-contract
  section above). `grok agent stdio` is not the answer either -- that is a
  JSON-RPC server for the Agent SDKs, not a prompt-on-stdin mode. The practical
  consequence of argv transport: prompts are capped at 128 KiB and are visible to
  process inspection while the run is live.

- **Auth is a mounted config dir, not an API key.** Credentials live in
  `~/.grok/auth.json`, written by `grok login` on the host; the mount is
  read-write, like the Claude/Gemini/Codex mounts. There is no `XAI_API_KEY` code
  path. A headless host with no browser can set `GROK_DEPLOYMENT_KEY` instead.

- **The routes pass `--no-auto-update`, and that is not a version pin.** The
  installer still fetches the current stable build on every container start. The
  flag stops the *running agent* from updating itself, which matters only because
  the config dir is bind-mounted: grok's updater stages into `~/.grok/downloads`
  and repoints `~/.grok/bin/{grok,agent}`, so a container-side update would leave
  a ~150 MB binary in the operator's home and flip the symlink their **host**
  `grok` resolves through. This is the general lesson for any harness whose
  updater writes inside a mounted config dir -- check where it stages before you
  let it run unattended.

- **The install script fetches the artifact, not xAI's `install.sh`.** Piping the
  vendor script into a container would symlink a bare `agent` command onto `PATH`
  (ambiguous in a repo full of agents), append a block to `~/.bashrc`, and stage
  downloads under the bind-mounted `~/.grok`. `scripts/install-grok.sh` resolves
  the version from `https://x.ai/cli/stable` and fetches the raw binary directly.

- **Watch the watchdog.** In the default `plain` output format Grok prints its
  reply in one go at the end of the run, so a long stretch of tool calls produces
  no stdout to reset the inactivity timer. Every commented Grok route ships with
  `max_inactivity_seconds: 1800` -- including the two *shared* review routes
  (`issue-triage`, `default-merge-request`), where the override is a route-level
  key that must be uncommented alongside the agent entry. It applies to every
  agent on that route, which is harmless: it only widens the silence they are
  allowed before being killed. If 1800s is not enough, `--output-format
  streaming-json` emits continuous token events instead, at the cost of
  JSON-shaped run-logs.

To enable: uncomment the `GROK_*` block in `.env.example`, the Grok routes in
`config/routes.yaml`, and the mount in `docker-compose.yml`; run `grok login` on
the host; create a `grok` GitLab user and set a real token/email.

---

## Worked Example: Pi (a harness that reintroduces Node -- behind the gate)

Pi (`pi`, package `@earendil-works/pi-coding-agent`) ships commented out in
`config/routes.yaml`, `.env.example`, and `docker-compose.yml` (issue #32). It is
provider-agnostic like OpenCode/Goose -- one `pi` binary backs every logical
`pi-*` agent, named per model (`pi-nemotron` here). Two things make it worth a
worked example: it brings Node back to the image, and it is the one harness
shipped with a *single* example route rather than the full family.

- **Node is bootstrapped in the install script, not baked into the image.** Pi is
  an npm package that needs Node `>=22.19.0`, but the base image deliberately ships
  no Node (dropped when Gemini's harness became `agy`). Baking Node into the
  `Dockerfile` would make every operator -- including those who never touch Pi --
  pay the image-size cost and would silently reverse that decision. Instead
  `scripts/install-pi.sh` downloads a **pinned, SHA256-verified** Node 22 tarball
  into a user-local `~/.local/node-v<ver>` and symlinks **only `node`** onto the
  shared `~/.local/bin` PATH, then installs Pi with `npm install -g
  --ignore-scripts --prefix ~/.local @earendil-works/pi-coding-agent` (npm invoked
  via a command-scoped PATH so `npm`/`npx` never land on the shared PATH -- see the
  Goose interaction note below). Because the whole thing sits behind the preflight
  `# provides: pi` gate, a commented-out Pi route costs nothing: no Node, no npm, no
  download. `--ignore-scripts` matches upstream's own headless install and sidesteps
  native compilation, so the image needs no `build-essential`. The two version
  policies are deliberately different: **Node is pinned** (not latest-tracked like
  the agent CLIs, because a full language runtime warrants it) and cached across
  boots, while **Pi tracks latest** -- `npm install` runs every boot, so a restart
  repairs or updates the install rather than freezing it at the first-boot version
  (the Goose/grok installers re-fetch each start the same way). See
  `docs/DEPENDENCY_MANAGEMENT.md`.

- **Exposing only `node` keeps `npx` away from Goose.** `~/.local/bin` is on the
  application-wide `PATH`, so symlinking `npm`/`npx` there would also give an
  enabled Goose harness a runnable `npx` -- letting it execute host-configured
  npx-based stdio extensions and silencing `goose_config`'s "not on PATH" warning.
  Pi's shim only needs `node`, so that is the only binary the install script puts on
  the shared PATH; Pi's own `npm` stays in the versioned Node dir. A guard test
  (`test_install_pi_does_not_leak_npm_or_npx_onto_shared_path`) pins this.

- **The prompt goes on stdin -- no `${PROMPT}` token.** Pi print mode (`-p`) reads
  piped stdin and merges it into the initial prompt, so the route omits the
  `${PROMPT}` argv placeholder and the dispatcher pipes the rendered prompt to the
  child (the default transport; see the CLI-contract section above). This also
  keeps the prompt off the 128 KiB argv ceiling. The route args are
  `-p --no-session --no-approve --model ${PI_NEMOTRON_MODEL}`.

- **`--no-session` and `--no-approve` are security/hygiene flags, not cosmetics.**
  `--no-session` stops each webhook run from persisting a session under the
  bind-mounted `~/.pi/agent/sessions/`. `--no-approve` stops an untrusted
  repository or branch from opting *itself* into executable Pi extensions
  (project-local `.pi` packages can run arbitrary code; a mounted host setting of
  `defaultProjectTrust: always` would otherwise let it).

- **Auth is a mounted config dir, and the provider is whatever the host configured
  -- OpenRouter by default.** Credentials live in `~/.pi/agent/auth.json`, and the
  default model `nvidia/nemotron-3-ultra-550b-a55b` is served **via OpenRouter**
  (the host `auth.json` holds an `openrouter` key, and `settings.json` sets
  `defaultProvider: openrouter`), not NVIDIA NIM directly -- so there is no
  `NVIDIA_API_KEY` to set. The mount is read-write, like the Claude/Gemini/Codex
  mounts, and points at the **narrow `agent` config root** (`~/.pi/agent`), not all
  of `~/.pi`. RDT's preflight validates the *GitLab* credentials, not Pi's provider
  auth, so confirm `pi` works on the host before enabling. The Pi `.env.example`
  block sets `PI_SKIP_VERSION_CHECK=1` / `PI_TELEMETRY=0` by default for quieter,
  non-mutating starts.

- **No Grok-style self-update hazard.** Pi does a startup version check but only
  `pi update` changes the installation, which lives under container-local
  `~/.local`, not the host-mounted config -- so no `--no-auto-update`-style flag is
  needed. The only writes into the RW `~/.pi/agent` mount are the version-check
  marker and `models-store.json`; `PI_SKIP_VERSION_CHECK=1` (set by default in the
  Pi env block) suppresses the marker write.

- **Watch the watchdog.** A 550B model can prefill silently, and Pi only resets the
  inactivity timer on stdout, so the mention route ships with
  `max_inactivity_seconds: 1800`.

- **Scope: one example route on purpose.** Unlike OpenCode/Goose/Grok (which ship
  commented across the assign/review/mention families), Pi ships a single
  `mention-pi-nemotron` example. Clone the OpenCode/Goose blocks if you want Pi on
  the assign/review routes too.

To enable: uncomment the `PI_NEMOTRON_*` block in `.env.example`, the
`mention-pi-nemotron` route in `config/routes.yaml`, and the mount in
`docker-compose.yml`; configure and log in to Pi on the host; create a `nemotron`
GitLab user and set a real token/email. The agent slug (`pi-nemotron`) resolves
the `PI_NEMOTRON_*` env vars; the GitLab username stays the plain `nemotron`.

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

3. **Remove model/token env vars**: If no remaining route references
   `${<AGENT>_MODEL}`, the model variable is no longer required. Once no route
   invokes the agent, `<AGENT>_AGENT_GITLAB_TOKEN` and the git identity
   variables can be removed too.

   Removing the route is what stops the harness from being installed. Leaving
   the token behind is harmless -- the preflight only warns that a credentialed
   agent has no route -- but the binary is skipped either way.

4. **Remove Docker volume mounts** (optional): The config-directory bind mounts
   in `docker-compose.yml` (e.g., `~/.codex:/home/appuser/.codex`) are
   unconditional. Removing unused mounts avoids potential startup issues if the
   host directory does not exist and gives a leaner container configuration.

5. **Remove install script** (optional): No longer necessary. The preflight
   skips any install script whose binary no enabled route asks for, so an
   unused `scripts/install-<agent>.sh` costs nothing at startup. Delete it only
   if you want it gone from the repo.

> **Summary**: Agents are opt-in at the routing layer, and the harness install
> now follows the routes: deleting an agent's routes is a genuine opt-out, and
> its binary stops being downloaded. Only the Docker Compose volume mounts
> remain opt-out.

## The install gate: `# provides:`

At startup, `docker-entrypoint.sh` runs `python -m app.preflight` instead of
blindly executing every `scripts/install-*.sh`. The preflight installs a
harness only when the active route config actually needs it:

> Run install script `S` **iff** some agent entry in an enabled route runs a
> command that `S` provides, **and** that agent has a GitLab token and a git
> identity.

Because the script's filename is not the binary's name -- `install-gemini.sh`
installs the Antigravity CLI, whose binary is `agy` -- each installer declares
what it puts on PATH:

```bash
# provides: agy
```

Rules for the declaration:

- Put it in the first 15 lines of the script (convention: just below the
  license header).
- List the binary exactly as routes invoke it in `options.command`.
- Space-separate every executable the installer supplies, including aliases or
  required companion binaries: `# provides: codex codex-code-mode-host`.
- An install script with no `# provides:` line is **never run**, and the
  preflight warns about it.

This is what makes the shared-binary case work: one `opencode` binary backs
every logical `opencode-*` agent, so the gate keys off the binary, not the
agent name. OpenCode installs when *any* `opencode-*` agent is both routed and
credentialed, and is skipped when none are.

If a route runs a command that no install script provides -- a BYOA binary you
baked into a custom image, say -- the preflight warns rather than failing, on
the assumption that it is already on PATH. That assumption is then checked: see
below.

### Binaries are verified after the installs

Credentials are checked *before* the install loop; binaries are checked *after*
it. Once an installer is selected, every executable in its `# provides:` line
joins the post-install check, including companions that routes do not invoke
directly. The entrypoint **fails the container if one is missing**. Otherwise the
container could start with an incomplete harness and fail only after a dispatch
tries to use it -- the exact late failure this design exists to prevent.

This catches a failed download, a `# provides:` line that names the wrong
binary, and a BYOA binary that was never baked into the image. A binary that is
already present passes regardless of who installed it, so BYOA keeps working.

Note the asymmetry with the install step itself: a download *failing* stays a
soft warning, because a flaky CDN is not a misconfiguration. It is the end
state -- "the harness is not on PATH now that we are done" -- that is fatal.

> **Hot reload will not install a harness.** With `DEBUG_RELOAD_ROUTES=true`,
> route edits are picked up at runtime, but installs happen once, in the
> entrypoint. Adding an agent to a route in a running container gives you a
> route whose binary was never installed. **Restart the container** after
> adding an agent.

## Startup is strict about credentials

The preflight **fails the container** when an enabled route dispatches an agent
that has no GitLab token, or no git identity. Both are hard requirements inside
`glab-usr`, so such a route could never succeed; failing at startup beats
failing mid-dispatch after a webhook has already fired.

To satisfy the check, every agent named by a route needs:

- `<AGENT>_AGENT_GITLAB_TOKEN` (or a token file at `~/.<agent>/glab-token`), and
- `<AGENT>_AGENT_GIT_EMAIL` set to a real address -- the `@example.com`
  placeholder is rejected.

The reverse is only a warning: a token with no route is fine (staged rollouts,
and `BRANCH_PRUNING_AGENT`, which needs a glab identity but no route and no
harness binary). See `docs/ENVIRONMENT.md` for the full rule.

Run the same check by hand at any time:

```bash
python -m app.preflight            # prints the install set; exits 1 on bad config
```
