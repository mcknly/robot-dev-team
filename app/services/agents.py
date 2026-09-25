"""Robot Dev Team Project
File: app/services/agents.py
Description: Agent orchestration utilities.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio
import codecs
import json
import os
import shlex
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.services.branch_resolver import resolve_branch
from app.services.context_builder import render_prompt
from app.services.dashboard import dashboard_manager
from app.services.git_runtime import GLAB_USR_TIMEOUT_SECONDS, git_auth_lock
from app.services.glab import resolve_agent_token
from app.services.project_paths import PROJECT_PATHS
from app.services.routes import AgentTask

LOGGER = get_logger(__name__)

# Routes may place this token in an agent's `args` to request that the rendered
# prompt be passed as that argv element instead of being written to the agent's
# stdin. Antigravity (`agy`) needs this: its `--print`/`-p` flag takes the
# prompt as the flag's *value* and the CLI never reads stdin, so a stdin-only
# handoff reaches it as an empty prompt. Agents whose CLI does read stdin
# (claude, codex, opencode) simply omit the token and keep the stdin path.
PROMPT_ARG_PLACEHOLDER = "${PROMPT}"

# Linux caps a single argv element at MAX_ARG_STRLEN (32 pages = 128 KiB,
# including the trailing NUL); exceeding it fails the execve with E2BIG. Guard
# the substitution so an oversized prompt produces an actionable error in the
# run log rather than an opaque OSError at spawn time.
MAX_PROMPT_ARG_BYTES = 32 * 4096 - 1


class PromptTooLargeError(RuntimeError):
    """Raised when a prompt is too large to pass as a single argv element."""


def _inject_prompt_arg(args: List[str], prompt_text: str) -> Tuple[List[str], bool]:
    """Substitute the rendered prompt into any PROMPT_ARG_PLACEHOLDER slots.

    Returns the argv to launch with and whether a substitution happened. When
    it did, the caller must not also write the prompt to stdin -- the agent
    would otherwise receive it twice.
    """

    if PROMPT_ARG_PLACEHOLDER not in args:
        return args, False

    encoded_size = len(prompt_text.encode("utf-8"))
    if encoded_size > MAX_PROMPT_ARG_BYTES:
        raise PromptTooLargeError(
            f"prompt is {encoded_size} bytes, which exceeds the {MAX_PROMPT_ARG_BYTES}-byte "
            f"limit for a single command-line argument. This agent's CLI accepts the prompt "
            f"only as an argv value (via the '{PROMPT_ARG_PLACEHOLDER}' placeholder in "
            f"routes.yaml) and cannot read it from stdin, so the prompt must be shortened "
            f"-- trim the prompt template in prompts/ or narrow the event context."
        )

    return [prompt_text if arg == PROMPT_ARG_PLACEHOLDER else arg for arg in args], True


# Antigravity (the `gemini` agent's CLI harness, binary name `agy`) stores
# its OAuth credential as a JSON file at this path when no host-style
# libsecret session is available, which is the container's natural state.
# The file is created by a one-time interactive `agy` run; see
# docs/AGENT_ONBOARDING.md for the bootstrap procedure.
ANTIGRAVITY_TOKEN_PATH = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
ANTIGRAVITY_BOOTSTRAP_HINT = (
    "Antigravity (`agy`) is not bootstrapped in this container -- the OAuth "
    "credential file is missing or unreadable at "
    "~/.gemini/antigravity-cli/antigravity-oauth-token. Run the one-time "
    "OAuth bootstrap so the token lands on the host-mounted ~/.gemini "
    "directory:\n"
    "  docker compose run --rm app agy\n"
    "The default entrypoint runs the UID/GID remap and the install scripts "
    "before exec'ing `agy` as appuser, so the token file ends up readable by "
    "normal webhook dispatch. Complete the Google OAuth flow in your browser, "
    "then re-trigger the webhook."
)


def _antigravity_preflight(token_path: Optional[Path] = None) -> Optional[str]:
    """Validate the Antigravity OAuth credential file before invoking `agy`.

    Returns ``None`` when the file looks usable, or a user-facing error
    message when it is missing, unreadable, empty, not valid JSON, or
    missing the expected ``token`` key. The credential value itself is
    never logged or returned -- only presence and parseability are checked
    so the failure mode does not leak the OAuth token through logs or
    GitLab comments.

    Invoke only for the `gemini` agent; other agents have no equivalent
    file-based credential check and must not be blocked by it.

    The default path is resolved at call time (not at function-definition
    time) so test fixtures can monkeypatch the module-level constant.
    """

    if token_path is None:
        token_path = ANTIGRAVITY_TOKEN_PATH
    if not token_path.exists():
        return ANTIGRAVITY_BOOTSTRAP_HINT
    try:
        raw = token_path.read_bytes()
    except OSError:
        return ANTIGRAVITY_BOOTSTRAP_HINT
    if not raw:
        return ANTIGRAVITY_BOOTSTRAP_HINT
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ANTIGRAVITY_BOOTSTRAP_HINT
    if not isinstance(parsed, dict) or "token" not in parsed:
        return ANTIGRAVITY_BOOTSTRAP_HINT
    return None


class AgentKilledError(Exception):
    """Raised when an agent dispatch is terminated by the operator."""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"agent dispatch cancelled: {event_id}")
        self.event_id = event_id


class AgentProcessInfo:
    """Metadata about a running agent subprocess."""

    __slots__ = ("proc", "agent", "task")

    def __init__(self, proc: asyncio.subprocess.Process, agent: str, task: str) -> None:
        self.proc = proc
        self.agent = agent
        self.task = task


class AgentRegistry:
    """Tracks running agent dispatch tasks and subprocesses."""

    def __init__(self) -> None:
        self._dispatch_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._processes: Dict[str, AgentProcessInfo] = {}
        self._killed_events: Set[str] = set()
        self._lock = asyncio.Lock()

    async def register_dispatch(self, event_id: str) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        async with self._lock:
            self._dispatch_tasks[event_id] = task
            self._killed_events.discard(event_id)

    async def unregister_dispatch(self, event_id: str) -> None:
        async with self._lock:
            self._dispatch_tasks.pop(event_id, None)
            self._processes.pop(event_id, None)
            self._killed_events.discard(event_id)

    async def register_process(self, event_id: str, proc: asyncio.subprocess.Process, agent: str, task: str) -> None:
        info = AgentProcessInfo(proc, agent, task)
        async with self._lock:
            self._processes[event_id] = info

    async def clear_process(self, event_id: str, proc: asyncio.subprocess.Process) -> None:
        async with self._lock:
            info = self._processes.get(event_id)
            if info and info.proc is proc:
                self._processes.pop(event_id, None)

    async def kill_event(self, event_id: str) -> Tuple[bool, List[Dict[str, str]]]:
        """Kill the running agent (if any) and cancel the dispatch task."""

        async with self._lock:
            dispatch_task = self._dispatch_tasks.get(event_id)
            proc_info = self._processes.get(event_id)
            if dispatch_task or proc_info:
                self._killed_events.add(event_id)

        killed_agents: List[Dict[str, str]] = []

        if proc_info and proc_info.proc.returncode is None:
            killed_agents.append({"agent": proc_info.agent, "task": proc_info.task})
            # Intentionally sends SIGKILL immediately (no SIGTERM grace period).
            # Dashboard kills are emergency-stop operations initiated by an operator,
            # so instant termination is preferred over graceful shutdown.
            try:
                os.killpg(os.getpgid(proc_info.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    proc_info.proc.kill()
                except ProcessLookupError:  # pragma: no cover - defensive
                    pass
            await proc_info.proc.wait()

        cancelled = False
        if dispatch_task and not dispatch_task.done():
            dispatch_task.cancel()
            cancelled = True

        async with self._lock:
            if cancelled:
                self._dispatch_tasks.pop(event_id, None)
            if proc_info:
                current = self._processes.get(event_id)
                if current and current.proc is proc_info.proc:
                    self._processes.pop(event_id, None)

        return cancelled, killed_agents

    async def is_marked_killed(self, event_id: str) -> bool:
        async with self._lock:
            return event_id in self._killed_events


agent_registry = AgentRegistry()


async def kill_event(event_id: str) -> Dict[str, Any]:
    """Terminate any running agent for the given event id."""

    cancelled, killed_agents = await agent_registry.kill_event(event_id)
    action_taken = cancelled or bool(killed_agents)
    return {
        "event_id": event_id,
        "action_taken": action_taken,
        "dispatch_cancelled": cancelled,
        "killed_agents": killed_agents,
    }


async def dispatch_agents(event_id: str, tasks: List[AgentTask], context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Execute configured agent tasks against the provided context."""

    if not tasks:
        return []

    Path(settings.run_logs_dir).mkdir(parents=True, exist_ok=True)
    await agent_registry.register_dispatch(event_id)
    results: List[Dict[str, Any]] = []
    try:
        # Serial dispatch is load-bearing for the panel-aware review prompts
        # (issue_review / merge_request_review): later reviewers only see
        # prior agents' comments because the earlier agent has already
        # finished and posted. Parallelizing here will break that strategy --
        # see docs/SYSTEM_DESIGN.md "Panel-aware review prompts".
        for agent_task in tasks:
            if await agent_registry.is_marked_killed(event_id):
                raise AgentKilledError(event_id)
            result = await _execute_agent(event_id, agent_task, context)
            results.append(result)
        return results
    except asyncio.CancelledError as exc:
        LOGGER.warning("Agent dispatch cancelled", extra={"event_id": event_id})
        raise AgentKilledError(event_id) from exc
    except AgentKilledError:
        LOGGER.warning("Agent dispatch killed", extra={"event_id": event_id})
        raise
    finally:
        await agent_registry.unregister_dispatch(event_id)


def _log_agent_finished(
    event_id: str,
    agent_task: AgentTask,
    status: str,
    returncode: int,
    log_file: str,
) -> None:
    """Emit the structured 'Agent finished' record shared by every exit path."""
    LOGGER.info(
        "Agent finished: agent=%s task=%s status=%s rc=%s log=%s",
        agent_task.agent,
        agent_task.task,
        status,
        returncode,
        log_file,
        extra={
            "event_id": event_id,
            "agent": agent_task.agent,
            "task": agent_task.task,
            "status": status,
            "returncode": returncode,
            "log_file": log_file,
        },
    )


def _finalize_error(
    event_id: str,
    agent_task: AgentTask,
    prompt_name: str,
    dashboard_key: str,
    log_file: Path,
    error: str,
    returncode: int = -1,
) -> Dict[str, Any]:
    """Build the result for a pre-dispatch failure and close out the run.

    Used by the early-exit validation paths (preflight, token resolution,
    project resolution) that fail before the agent subprocess is launched.
    Preserves the observable lifecycle: a fully-populated error ``result``,
    the structured finish log, and exactly-once dashboard completion.
    """
    result: Dict[str, Any] = {
        "agent": agent_task.agent,
        "task": agent_task.task,
        "prompt": prompt_name,
        "log_file": str(log_file),
        "status": "error",
        "error": error,
        "returncode": returncode,
    }
    _log_agent_finished(event_id, agent_task, "error", returncode, result["log_file"])
    dashboard_manager.agent_finished(dashboard_key)
    return result


async def _resolve_and_apply_branch(
    event_id: str,
    agent_task: AgentTask,
    context: Dict[str, Any],
    project_path: Optional[str],
    result: Dict[str, Any],
    log_file: Path,
    dashboard_key: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Resolve/checkout the dispatch branch, folding results into ``result``.

    Returns ``(current_branch, error_result)``. A non-None ``error_result`` means
    branch resolution failed and the caller must return it immediately; the run
    has already been finalized (log written, dashboard completion recorded).
    """
    current_branch: Optional[str] = None
    # Branch operations need the read-write path even when the agent runs read-only.
    branch_working_dir = PROJECT_PATHS.resolve(project_path, access="readwrite")
    if branch_working_dir is None:
        LOGGER.warning(
            "Cannot resolve read-write path for branch switching, skipping",
            extra={"project_path": project_path},
        )
        return current_branch, None

    branch_result = await resolve_branch(
        event=context.get("payload", {}),
        project_path=project_path or "",
        working_dir=branch_working_dir,
        agent=agent_task.agent,
    )
    if branch_result.switched:
        LOGGER.info(
            "Switched to branch '%s' before dispatch",
            branch_result.branch,
            extra={
                "event_id": event_id,
                "branch": branch_result.branch,
                "backup_branch": branch_result.backup_branch,
            },
        )
    if branch_result.backups:
        result["backups"] = [
            {"branch": b.branch, "reason": b.reason}
            for b in branch_result.backups
        ]
    if branch_result.branch:
        result["branch"] = branch_result.branch
        current_branch = branch_result.branch
    if not branch_result.success:
        LOGGER.error(
            "Branch resolution failed",
            extra={"event_id": event_id, "error": branch_result.error},
        )
        error_payload = {
            "prompt": [],
            "error": f"Branch resolution failed: {branch_result.error}",
        }
        log_file = _write_text_log(log_file, json.dumps(error_payload, indent=2, ensure_ascii=False))
        result["log_file"] = str(log_file)
        result.update(
            {
                "status": "error",
                "error": f"Branch resolution failed: {branch_result.error}",
                "returncode": -1,
            }
        )
        _log_agent_finished(event_id, agent_task, "error", -1, result["log_file"])
        dashboard_manager.agent_finished(dashboard_key)
        return current_branch, result

    return current_branch, None


@dataclass
class _RunOutcome:
    """Outcome of the authenticate-and-run phase of a dispatch.

    ``error_result`` is set when authentication or subprocess launch fails; the
    caller must return it directly. Otherwise the subprocess output fields carry
    the completed run for success finalization.
    """

    error_result: Optional[Dict[str, Any]] = None
    stdout: str = ""
    stderr: str = ""
    returncode: int = -1
    timed_out: Optional[str] = None
    auth_stdout: str = ""
    auth_stderr: str = ""
    auth_returncode: Optional[int] = None


async def _run_with_auth(
    command: str,
    args: List[str],
    prompt_text: str,
    env: Dict[str, str],
    working_dir: Optional[str],
    event_id: str,
    agent_task: AgentTask,
    result: Dict[str, Any],
    log_file: Path,
    dashboard_key: str,
) -> _RunOutcome:
    """Authenticate the agent and run its subprocess.

    Owns the try/except/finally so dashboard completion fires exactly once for
    the auth-failure, prompt-too-large, command-not-found, and success paths
    alike. On any launch/auth failure it populates ``result`` and returns it via
    ``_RunOutcome.error_result``.
    """
    outcome = _RunOutcome()
    try:
        auth_stdout, auth_stderr, auth_returncode = await _authenticate_agent(
            agent_task.agent, env, working_dir
        )
        outcome.auth_stdout = auth_stdout
        outcome.auth_stderr = auth_stderr
        outcome.auth_returncode = auth_returncode
        if auth_returncode != 0:
            LOGGER.error(
                "GitLab authentication failed",
                extra={"agent": agent_task.agent, "returncode": auth_returncode},
            )
            if dashboard_manager.enabled:
                dashboard_manager.publish_stderr(
                    event_id,
                    agent_task.agent,
                    agent_task.task,
                    "failed to authenticate with GitLab (glab-usr)",
                )
            auth_payload = {
                "auth": {
                    "stdout": _split_lines(auth_stdout),
                    "stderr": _split_lines(auth_stderr),
                    "returncode": auth_returncode,
                },
                "prompt": _split_lines(prompt_text),
            }
            log_file = _write_text_log(log_file, json.dumps(auth_payload, indent=2, ensure_ascii=False))
            result["log_file"] = str(log_file)
            rc = auth_returncode if auth_returncode is not None else -1
            result.update(
                {
                    "status": "error",
                    "error": "glab-usr authentication failed",
                    "returncode": rc,
                    "auth_stdout": _split_lines(auth_stdout),
                    "auth_stderr": _split_lines(auth_stderr),
                    "auth_returncode": auth_returncode,
                }
            )
            _log_agent_finished(event_id, agent_task, "error", rc, result["log_file"])
            outcome.error_result = result
            return outcome
        stdout, stderr, returncode, timed_out = await _run_subprocess(
            command,
            args,
            prompt_text,
            env,
            working_dir,
            event_id,
            agent_task,
        )
        outcome.stdout = stdout
        outcome.stderr = stderr
        outcome.returncode = returncode
        outcome.timed_out = timed_out
    except PromptTooLargeError as exc:
        LOGGER.error(
            "Prompt too large to pass as a command-line argument",
            extra={"event_id": event_id, "agent": agent_task.agent, "task": agent_task.task},
        )
        outcome.error_result = _finalize_run_error(
            event_id, agent_task, result, log_file, prompt_text,
            str(exc), outcome.auth_stdout, outcome.auth_stderr, outcome.auth_returncode,
        )
    except FileNotFoundError as exc:
        missing_command = exc.filename or command
        LOGGER.error("Subprocess command not found", extra={"command": missing_command})
        outcome.error_result = _finalize_run_error(
            event_id, agent_task, result, log_file, prompt_text,
            f"command not found: {missing_command}",
            outcome.auth_stdout, outcome.auth_stderr, outcome.auth_returncode,
        )
    finally:
        dashboard_manager.agent_finished(dashboard_key)

    return outcome


def _finalize_run_error(
    event_id: str,
    agent_task: AgentTask,
    result: Dict[str, Any],
    log_file: Path,
    prompt_text: str,
    error: str,
    auth_stdout: str,
    auth_stderr: str,
    auth_returncode: Optional[int],
) -> Dict[str, Any]:
    """Write the error log and finalize ``result`` for a post-auth launch failure."""
    error_payload: Dict[str, Any] = {
        "prompt": _split_lines(prompt_text),
        "auth": {
            "stdout": _split_lines(auth_stdout),
            "stderr": _split_lines(auth_stderr),
            "returncode": auth_returncode,
        },
        "error": error,
    }
    log_file = _write_text_log(log_file, json.dumps(error_payload, indent=2, ensure_ascii=False))
    result["log_file"] = str(log_file)
    result.update(
        {
            "status": "error",
            "error": error,
            "returncode": -1,
            "auth_stdout": _split_lines(auth_stdout),
            "auth_stderr": _split_lines(auth_stderr),
            "auth_returncode": auth_returncode,
        }
    )
    _log_agent_finished(event_id, agent_task, "error", -1, result["log_file"])
    return result


def _finalize_success(
    event_id: str,
    agent_task: AgentTask,
    result: Dict[str, Any],
    log_file: Path,
    prompt_text: str,
    outcome: _RunOutcome,
) -> Dict[str, Any]:
    """Write the output log and finalize ``result`` for a completed run."""
    output_payload = {
        "stdout": _split_lines(outcome.stdout),
        "stderr": _split_lines(outcome.stderr),
        "prompt": _split_lines(prompt_text),
        "stdout_trailing_newline": outcome.stdout.endswith("\n") if outcome.stdout else False,
        "stderr_trailing_newline": outcome.stderr.endswith("\n") if outcome.stderr else False,
        "auth": {
            "stdout": _split_lines(outcome.auth_stdout),
            "stderr": _split_lines(outcome.auth_stderr),
            "returncode": outcome.auth_returncode,
        },
    }
    log_file = _write_text_log(log_file, json.dumps(output_payload, indent=2, ensure_ascii=False))
    result["log_file"] = str(log_file)

    status = "ok" if outcome.returncode == 0 else "error"
    result.update(
        {
            "status": status,
            "returncode": outcome.returncode,
            "auth_stdout": _split_lines(outcome.auth_stdout),
            "auth_stderr": _split_lines(outcome.auth_stderr),
            "auth_returncode": outcome.auth_returncode,
        }
    )
    if outcome.timed_out:
        result["timed_out"] = outcome.timed_out
    if outcome.returncode != 0:
        result["error"] = outcome.stderr
    _log_agent_finished(event_id, agent_task, status, outcome.returncode, result["log_file"])
    return result


def _assemble_env(agent_task: AgentTask, agent_token: Optional[str]) -> Dict[str, str]:
    """Build the subprocess environment for an agent dispatch.

    Route-config ``env`` is spread first so the agent-specific GITLAB_TOKEN and
    GITLAB_HOST always take precedence, even if the route config sets them.
    """
    env_options = agent_task.options.get("env", {})
    if not isinstance(env_options, dict):
        env_options = {}
    env = {
        **env_options,
        "ROBOT_AGENT_NAME": agent_task.agent,
        "ROBOT_TASK_NAME": agent_task.task,
        "CURRENT_AGENT": agent_task.agent,
    }
    if agent_token:
        env["GITLAB_TOKEN"] = agent_token
    if settings.glab_host:
        env["GITLAB_HOST"] = settings.glab_host
    return env


@dataclass
class _DispatchSetup:
    """Validated inputs for an agent dispatch, produced by ``_prepare_dispatch``."""

    command: str
    args: List[str]
    env: Dict[str, str]
    working_dir: Optional[str]
    project_path: Optional[str]
    result: Dict[str, Any]


async def _prepare_dispatch(
    event_id: str,
    agent_task: AgentTask,
    context: Dict[str, Any],
    prompt_name: str,
    log_file: Path,
    dashboard_key: str,
) -> Tuple[Optional[_DispatchSetup], Optional[Dict[str, Any]]]:
    """Run the pre-dispatch validation gauntlet and build the base env/result.

    Returns ``(setup, error_result)``. A non-None ``error_result`` means a
    pre-dispatch check failed (Antigravity preflight, token resolution, or
    project resolution); the run is already finalized and the caller must return
    it. Otherwise ``setup`` carries the validated command/args/env/working_dir.
    """
    # Antigravity (`gemini` agent) requires a one-time OAuth bootstrap that
    # writes a credential file under ~/.gemini/antigravity-cli/. Without
    # that file, `agy -p` prints an OAuth URL and waits ~30s for a callback
    # before exiting, which gives operators a confusing "agent timed out"
    # signal instead of a clear "needs bootstrap" message. Fail fast here
    # with a one-line hint so the failure mode is obvious. Other agents
    # have no equivalent precondition and bypass this check entirely.
    if agent_task.agent.lower() == "gemini":
        preflight_error = _antigravity_preflight()
        if preflight_error:
            LOGGER.error(
                "Antigravity preflight failed: credential file missing or invalid",
                extra={"event_id": event_id, "agent": agent_task.agent},
            )
            error_payload = {
                "prompt": [],
                "error": preflight_error,
            }
            log_file = _write_text_log(log_file, json.dumps(error_payload, indent=2, ensure_ascii=False))
            return None, _finalize_error(
                event_id, agent_task, prompt_name, dashboard_key, log_file, preflight_error,
            )

    command = agent_task.options.get("command", agent_task.agent)
    args = agent_task.options.get("args", [])
    if not isinstance(args, list):
        args = [str(args)]

    # Resolve agent-specific GitLab token and inject into subprocess env.
    # This ensures each agent's glab/gitlab-connect calls use the correct
    # identity regardless of the shared global glab config state.
    try:
        agent_token = resolve_agent_token(agent_task.agent)
    except ValueError as exc:
        error_msg = str(exc)
        LOGGER.error(
            "Agent token resolution failed",
            extra={"event_id": event_id, "agent": agent_task.agent, "error": error_msg},
        )
        error_payload = {
            "prompt": [],
            "error": error_msg,
        }
        log_file = _write_text_log(log_file, json.dumps(error_payload, indent=2, ensure_ascii=False))
        return None, _finalize_error(
            event_id, agent_task, prompt_name, dashboard_key, log_file, error_msg,
        )

    env = _assemble_env(agent_task, agent_token)

    access = context.get("access", "readonly")
    project_path = context.get("project")
    try:
        working_dir = await PROJECT_PATHS.ensure_project_exists(
            project_path=project_path,
            access=access,
            clone_url=context.get("clone_url"),
            agent=agent_task.agent,
        )
    except RuntimeError as exc:
        LOGGER.error(
            "Failed to ensure project exists",
            extra={"event_id": event_id, "error": str(exc)},
        )
        error_payload = {
            "prompt": [],
            "error": str(exc),
        }
        log_file = _write_text_log(log_file, json.dumps(error_payload, indent=2, ensure_ascii=False))
        return None, _finalize_error(
            event_id, agent_task, prompt_name, dashboard_key, log_file, str(exc),
        )

    # Fail fast if project path could not be resolved - prevents agents from
    # running in the wrong directory (e.g., /work instead of the project mount)
    if working_dir is None:
        error_msg = f"Project path not found: {project_path}"
        LOGGER.error(
            "Project path could not be resolved",
            extra={
                "event_id": event_id,
                "project_path": project_path,
                "access": access,
            },
        )
        error_payload = {
            "prompt": [],
            "error": error_msg,
        }
        log_file = _write_text_log(log_file, json.dumps(error_payload, indent=2, ensure_ascii=False))
        return None, _finalize_error(
            event_id, agent_task, prompt_name, dashboard_key, log_file, error_msg,
        )

    result: Dict[str, Any] = {
        "agent": agent_task.agent,
        "task": agent_task.task,
        "prompt": prompt_name,
        "log_file": str(log_file),
    }
    if working_dir:
        result["working_dir"] = working_dir

    setup = _DispatchSetup(
        command=command,
        args=args,
        env=env,
        working_dir=working_dir,
        project_path=project_path,
        result=result,
    )
    return setup, None


async def _checkout_and_render_prompt(
    event_id: str,
    agent_task: AgentTask,
    context: Dict[str, Any],
    setup: _DispatchSetup,
    prompt_name: str,
    log_file: Path,
    dashboard_key: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Check out the dispatch branch and render the prompt for the run.

    Returns ``(prompt_text, error_result)``. A non-None ``error_result`` means
    branch resolution failed and the run is already finalized.
    """
    # Resolve and checkout appropriate branch before agent dispatch. Branch
    # resolution requires read-write access for git operations (fetch, checkout,
    # reset) even if the agent itself will run with read-only access.
    current_branch: Optional[str] = None
    if setup.working_dir and settings.enable_branch_switch:
        current_branch, branch_error = await _resolve_and_apply_branch(
            event_id, agent_task, context, setup.project_path,
            setup.result, log_file, dashboard_key,
        )
        if branch_error is not None:
            return None, branch_error

    # If branch switching is disabled or didn't determine a branch, query git directly
    if current_branch is None and setup.working_dir:
        current_branch = await _get_current_branch(setup.working_dir)

    # Populate current_branch in context for prompt substitution
    context["current_branch"] = current_branch or ""

    # Render prompt after branch resolution so ${CURRENT_BRANCH} reflects actual repo state
    prompt_text = render_prompt(prompt_name, context)
    if dashboard_manager.enabled:
        for line in _stream_lines(prompt_text):
            dashboard_manager.publish_prompt(event_id, agent_task.agent, agent_task.task, line)
    return prompt_text, None


async def _execute_agent(event_id: str, agent_task: AgentTask, context: Dict[str, Any]) -> Dict[str, Any]:
    prompt_name = agent_task.prompt or f"{agent_task.task}.txt"
    log_file = _log_path(event_id, agent_task.agent, context)

    dashboard_key = dashboard_manager.agent_started(event_id, agent_task.agent, agent_task.task)

    setup, setup_error = await _prepare_dispatch(
        event_id, agent_task, context, prompt_name, log_file, dashboard_key,
    )
    if setup_error is not None:
        return setup_error
    assert setup is not None  # narrowed: setup is populated when setup_error is None

    prompt_text, checkout_error = await _checkout_and_render_prompt(
        event_id, agent_task, context, setup, prompt_name, log_file, dashboard_key,
    )
    if checkout_error is not None:
        return checkout_error
    assert prompt_text is not None  # narrowed: set when checkout_error is None

    outcome = await _run_with_auth(
        setup.command, setup.args, prompt_text, setup.env, setup.working_dir,
        event_id, agent_task, setup.result, log_file, dashboard_key,
    )
    if outcome.error_result is not None:
        return outcome.error_result

    if await agent_registry.is_marked_killed(event_id):
        raise AgentKilledError(event_id)

    return _finalize_success(event_id, agent_task, setup.result, log_file, prompt_text, outcome)


async def _run_subprocess(
    command: str,
    args: List[str],
    prompt_text: str,
    env: Dict[str, str],
    working_dir: Optional[str],
    event_id: str,
    agent_task: AgentTask,
) -> tuple[str, str, int, Optional[str]]:
    launch_args, prompt_in_argv = _inject_prompt_arg(args, prompt_text)
    proc = await _launch_subprocess(command, launch_args, env, working_dir)
    # Log the pre-substitution argv so the placeholder stands in for the prompt
    # body -- the rendered prompt is already captured in the run log.
    command_line = shlex.join([command] + args)
    LOGGER.info(
        "Agent started: agent=%s task=%s pid=%s cmd=%s cwd=%s",
        agent_task.agent,
        agent_task.task,
        proc.pid,
        command_line,
        working_dir,
        extra={
            "event_id": event_id,
            "agent": agent_task.agent,
            "task": agent_task.task,
            "command": command_line,
            "pid": proc.pid,
            "working_dir": working_dir,
        },
    )
    if proc.stdin is not None:
        # When the prompt went into argv, still close stdin so CLIs that block
        # on EOF do not hang -- just don't hand them the prompt a second time.
        if not prompt_in_argv:
            proc.stdin.write(prompt_text.encode("utf-8"))
            await proc.stdin.drain()
        proc.stdin.close()

    await agent_registry.register_process(event_id, proc, agent_task.agent, agent_task.task)

    # Separate activity trackers for stdout and stderr.
    # Only stdout resets the inactivity watchdog -- stderr-only output (e.g.,
    # retry loops, stack traces) should not prevent the watchdog from firing.
    now = time.monotonic()
    stdout_activity_tracker: List[float] = [now]
    stderr_activity_tracker: List[float] = [now]

    stdout_chunks: List[str] = []
    stderr_chunks: List[str] = []
    stdout_task, stderr_task = _stream_subprocess_output(
        proc,
        stdout_chunks,
        stderr_chunks,
        event_id,
        agent_task,
        stdout_activity_tracker=stdout_activity_tracker,
        stderr_activity_tracker=stderr_activity_tracker,
    )

    # Resolve effective timeouts (per-task overrides fall back to global settings)
    wall_clock = agent_task.max_wall_clock_seconds if agent_task.max_wall_clock_seconds is not None else settings.agent_max_wall_clock_seconds
    inactivity = agent_task.max_inactivity_seconds if agent_task.max_inactivity_seconds is not None else settings.agent_max_inactivity_seconds

    try:
        timeout_reason = await _wait_for_completion(
            proc, wall_clock, inactivity,
            stdout_activity_tracker, stderr_activity_tracker,
        )
    except asyncio.CancelledError:
        await _terminate_process(proc)
        raise
    finally:
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        await agent_registry.clear_process(event_id, proc)

    stdout_text = "".join(stdout_chunks)
    stderr_text = "".join(stderr_chunks)

    if timeout_reason:
        timeout_marker = f"\n\n[System] Agent timed out ({timeout_reason})"
        if dashboard_manager.enabled:
            dashboard_manager.publish_stderr(
                event_id, agent_task.agent, agent_task.task,
                f"agent timed out ({timeout_reason})",
            )
        return stdout_text, stderr_text + timeout_marker, -1, timeout_reason

    returncode = proc.returncode
    if returncode is None:  # pragma: no cover - defensive guard
        raise RuntimeError("subprocess exited without a return code")

    return stdout_text, stderr_text, returncode, None


async def _launch_subprocess(
    command: str,
    args: List[str],
    env: Dict[str, str],
    working_dir: Optional[str],
) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        command,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **env},
        cwd=working_dir,
        start_new_session=True,
    )


async def _authenticate_agent(
    agent: str,
    env: Dict[str, str],
    working_dir: Optional[str],
) -> tuple[str, str, int]:
    # Acquire the shared git auth lock to prevent credential races with
    # the background BranchPruner (which also calls glab-usr).
    async with git_auth_lock:
        # Note: start_new_session is not needed here because glab-usr is a
        # short-lived blocking call that completes quickly and does not spawn
        # child processes.
        proc = await asyncio.create_subprocess_exec(
            "glab-usr",
            agent,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **env},
            cwd=working_dir,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=GLAB_USR_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            LOGGER.warning(
                "glab-usr timed out after %.0fs for agent '%s'",
                GLAB_USR_TIMEOUT_SECONDS,
                agent,
            )
            return "", f"glab-usr timed out after {GLAB_USR_TIMEOUT_SECONDS:.0f}s", -1
        stdout_text = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr_text = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        # communicate() has completed, so returncode is set; fall back to -1
        # to satisfy the int return contract if it is somehow still None.
        returncode = proc.returncode if proc.returncode is not None else -1
        return stdout_text, stderr_text, returncode


def _stream_subprocess_output(
    proc: asyncio.subprocess.Process,
    stdout_chunks: List[str],
    stderr_chunks: List[str],
    event_id: str,
    agent_task: AgentTask,
    stdout_activity_tracker: Optional[List[float]] = None,
    stderr_activity_tracker: Optional[List[float]] = None,
) -> tuple[asyncio.Task[Any], asyncio.Task[Any]]:
    if proc.stdout is None or proc.stderr is None:  # pragma: no cover - defensive guard
        raise RuntimeError("subprocess pipes are not available")

    stdout_task = asyncio.create_task(
        _consume_stream(
            proc.stdout,
            stdout_chunks,
            lambda payload: _publish_stdout(event_id, agent_task, payload),
            activity_tracker=stdout_activity_tracker,
        )
    )
    stderr_task = asyncio.create_task(
        _consume_stream(
            proc.stderr,
            stderr_chunks,
            lambda payload: _publish_stderr(event_id, agent_task, payload),
            activity_tracker=stderr_activity_tracker,
        )
    )
    return stdout_task, stderr_task


async def _consume_stream(
    stream: asyncio.StreamReader,
    buffer: List[str],
    publisher,
    activity_tracker: Optional[List[float]] = None,
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    while True:
        chunk = await stream.read(1024)
        if not chunk:
            break
        if activity_tracker is not None:
            activity_tracker[0] = time.monotonic()
        text = decoder.decode(chunk)
        if text:
            buffer.append(text)
            pending += text
            pending = _flush_pending(pending, publisher)
    remaining = decoder.decode(b"", final=True)
    if remaining:
        buffer.append(remaining)
        pending += remaining
    if pending:
        publisher(pending)


def _publish_stdout(event_id: str, agent_task: AgentTask, payload: str) -> None:
    if dashboard_manager.enabled:
        dashboard_manager.publish_stdout(event_id, agent_task.agent, agent_task.task, payload)


def _publish_stderr(event_id: str, agent_task: AgentTask, payload: str) -> None:
    if dashboard_manager.enabled:
        dashboard_manager.publish_stderr(event_id, agent_task.agent, agent_task.task, payload)


async def _wait_for_completion(
    proc: asyncio.subprocess.Process,
    max_wall_clock: int,
    max_inactivity: int,
    stdout_activity_tracker: List[float],
    stderr_activity_tracker: Optional[List[float]] = None,
) -> Optional[str]:
    """Wait for process completion with dual timeout limits.

    Only stdout activity resets the inactivity watchdog. Stderr output is
    tracked separately for diagnostics but does not prevent the inactivity
    timeout from firing. This prevents infinite retry loops (which produce
    only stderr) from blocking the agent queue indefinitely.

    Returns None if the process exited normally, or a string indicating
    the timeout reason (``"wall_clock"`` or ``"inactivity"``).
    """
    start = time.monotonic()
    while proc.returncode is None:
        await asyncio.sleep(1.0)
        if proc.returncode is not None:
            break

        elapsed = time.monotonic() - start
        if elapsed >= max_wall_clock:
            LOGGER.warning(
                "Agent exceeded wall-clock limit",
                extra={"limit": max_wall_clock, "elapsed": round(elapsed, 1)},
            )
            await _terminate_process(proc)
            return "wall_clock"

        now = time.monotonic()
        idle_stdout = now - stdout_activity_tracker[0]
        if idle_stdout >= max_inactivity:
            stderr_idle = (now - stderr_activity_tracker[0]) if stderr_activity_tracker else None
            stderr_still_active = stderr_idle is not None and stderr_idle < 60.0
            LOGGER.warning(
                "Agent exceeded inactivity limit (stdout-only watchdog)",
                extra={
                    "limit": max_inactivity,
                    "idle_stdout": round(idle_stdout, 1),
                    "stderr_still_active": stderr_still_active,
                },
            )
            await _terminate_process(proc)
            return "inactivity"

    return None


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate the process and its entire process group.

    Sends SIGTERM first, waits a grace period, then escalates to SIGKILL.
    Correctness does not depend on SIGTERM being respected by the CLI.
    """
    grace = settings.agent_timeout_grace_seconds

    # Attempt graceful SIGTERM first
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass

    # Wait up to grace_seconds for the process to exit
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        return
    except asyncio.TimeoutError:
        pass

    # Escalate to SIGKILL
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover - defensive
            pass
    await proc.wait()


def _log_path(event_id: str, agent: str, context: Optional[Dict[str, Any]] = None) -> Path:
    context = context or {}
    # Prefer the base UUID (without appended mention) to avoid duplication
    base_id = context.get("base_event_uuid") or event_id
    parts = [base_id]
    # Extract project name (without namespace) from the project path
    project_path = context.get("project")
    if project_path:
        project_name = project_path.rsplit("/", 1)[-1]
        safe_project = "".join(
            ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in project_name
        )
        parts.append(safe_project)
    # Add route name
    route = context.get("route")
    if route:
        safe_route = "".join(
            ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in route
        )
        parts.append(safe_route)
    # Always include agent name
    parts.append(agent.replace("/", "_"))
    filename = "-".join(parts) + ".out.json"
    return Path(settings.run_logs_dir) / filename


def _write_text_log(log_file: Path, content: str) -> Path:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_file.write_text(content, encoding="utf-8")
        return log_file
    except OSError as exc:
        import tempfile

        fallback_dir = Path(tempfile.gettempdir()) / "run-logs"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        fallback_path = fallback_dir / log_file.name
        fallback_path.write_text(content, encoding="utf-8")
        LOGGER.warning(
            "Failed to write log file; using fallback",
            extra={"original": str(log_file), "fallback": str(fallback_path)},
            exc_info=exc,
        )
        return fallback_path


def _split_lines(value: str) -> List[str]:
    if not value:
        return []
    return value.splitlines()


def _stream_lines(value: str) -> List[str]:
    if not value:
        return []
    return value.splitlines(True)


def _flush_pending(pending: str, publisher) -> str:
    while True:
        newline_index = pending.find("\n")
        if newline_index == -1:
            return pending
        segment = pending[: newline_index + 1]
        publisher(segment)
        pending = pending[newline_index + 1 :]


async def _get_current_branch(working_dir: str) -> Optional[str]:
    """Get the current branch name from the working directory."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        stdout, _ = await proc.communicate()

        if proc.returncode == 0:
            return stdout.decode("utf-8").strip()
        return None

    except Exception:
        return None
