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


async def _execute_agent(event_id: str, agent_task: AgentTask, context: Dict[str, Any]) -> Dict[str, Any]:
    prompt_name = agent_task.prompt or f"{agent_task.task}.txt"
    log_file = _log_path(event_id, agent_task.agent, context)

    dashboard_key = dashboard_manager.agent_started(event_id, agent_task.agent, agent_task.task)

    command = agent_task.options.get("command", agent_task.agent)
    args = agent_task.options.get("args", [])
    if not isinstance(args, list):
        args = [str(args)]

    env_options = agent_task.options.get("env", {})
    if not isinstance(env_options, dict):
        env_options = {}

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
        result: Dict[str, Any] = {
            "agent": agent_task.agent,
            "task": agent_task.task,
            "prompt": prompt_name,
            "log_file": str(log_file),
            "status": "error",
            "error": error_msg,
            "returncode": -1,
        }
        LOGGER.info(
            "Agent finished: agent=%s task=%s status=%s rc=%s log=%s",
            agent_task.agent,
            agent_task.task,
            "error",
            -1,
            result["log_file"],
            extra={
                "event_id": event_id,
                "agent": agent_task.agent,
                "task": agent_task.task,
                "status": "error",
                "returncode": -1,
                "log_file": result["log_file"],
            },
        )
        dashboard_manager.agent_finished(dashboard_key)
        return result

    # env_options (from route config) is spread first so that agent-specific
    # GITLAB_TOKEN and GITLAB_HOST always take precedence, even if the route
    # config env stanza sets them.
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
        result: Dict[str, Any] = {
            "agent": agent_task.agent,
            "task": agent_task.task,
            "prompt": prompt_name,
            "log_file": str(log_file),
            "status": "error",
            "error": str(exc),
            "returncode": -1,
        }
        LOGGER.info(
            "Agent finished: agent=%s task=%s status=%s rc=%s log=%s",
            agent_task.agent,
            agent_task.task,
            "error",
            -1,
            result["log_file"],
            extra={
                "event_id": event_id,
                "agent": agent_task.agent,
                "task": agent_task.task,
                "status": "error",
                "returncode": -1,
                "log_file": result["log_file"],
            },
        )
        dashboard_manager.agent_finished(dashboard_key)
        return result

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
        result: Dict[str, Any] = {
            "agent": agent_task.agent,
            "task": agent_task.task,
            "prompt": prompt_name,
            "log_file": str(log_file),
            "status": "error",
            "error": error_msg,
            "returncode": -1,
        }
        LOGGER.info(
            "Agent finished: agent=%s task=%s status=%s rc=%s log=%s",
            agent_task.agent,
            agent_task.task,
            "error",
            -1,
            result["log_file"],
            extra={
                "event_id": event_id,
                "agent": agent_task.agent,
                "task": agent_task.task,
                "status": "error",
                "returncode": -1,
                "log_file": result["log_file"],
            },
        )
        dashboard_manager.agent_finished(dashboard_key)
        return result

    result: Dict[str, Any] = {
        "agent": agent_task.agent,
        "task": agent_task.task,
        "prompt": prompt_name,
        "log_file": str(log_file),
    }
    if working_dir:
        result["working_dir"] = working_dir

    # Resolve and checkout appropriate branch before agent dispatch
    # Branch resolution requires read-write access for git operations (fetch, checkout, reset)
    # even if the agent itself will run with read-only access
    current_branch: Optional[str] = None
    if working_dir and settings.enable_branch_switch:
        # Get read-write path for branch operations
        branch_working_dir = PROJECT_PATHS.resolve(project_path, access="readwrite")
        if branch_working_dir is None:
            LOGGER.warning(
                "Cannot resolve read-write path for branch switching, skipping",
                extra={"project_path": project_path},
            )
        else:
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
                    extra={
                        "event_id": event_id,
                        "error": branch_result.error,
                    },
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
                LOGGER.info(
                    "Agent finished: agent=%s task=%s status=%s rc=%s log=%s",
                    agent_task.agent,
                    agent_task.task,
                    "error",
                    -1,
                    result["log_file"],
                    extra={
                        "event_id": event_id,
                        "agent": agent_task.agent,
                        "task": agent_task.task,
                        "status": "error",
                        "returncode": -1,
                        "log_file": result["log_file"],
                    },
                )
                dashboard_manager.agent_finished(dashboard_key)
                return result

    # If branch switching is disabled or didn't determine a branch, query git directly
    if current_branch is None and working_dir:
        current_branch = await _get_current_branch(working_dir)

    # Populate current_branch in context for prompt substitution
    context["current_branch"] = current_branch or ""

    # Render prompt after branch resolution so ${CURRENT_BRANCH} reflects actual repo state
    prompt_text = render_prompt(prompt_name, context)
    if dashboard_manager.enabled:
        for line in _stream_lines(prompt_text):
            dashboard_manager.publish_prompt(event_id, agent_task.agent, agent_task.task, line)

    auth_stdout = ""
    auth_stderr = ""
    auth_returncode: Optional[int] = None

    try:
        auth_stdout, auth_stderr, auth_returncode = await _authenticate_agent(agent_task.agent, env, working_dir)
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
            result.update(
                {
                    "status": "error",
                    "error": "glab-usr authentication failed",
                    "returncode": auth_returncode if auth_returncode is not None else -1,
                    "auth_stdout": _split_lines(auth_stdout),
                    "auth_stderr": _split_lines(auth_stderr),
                    "auth_returncode": auth_returncode,
                }
            )
            rc = auth_returncode if auth_returncode is not None else -1
            LOGGER.info(
                "Agent finished: agent=%s task=%s status=%s rc=%s log=%s",
                agent_task.agent,
                agent_task.task,
                "error",
                rc,
                result["log_file"],
                extra={
                    "event_id": event_id,
                    "agent": agent_task.agent,
                    "task": agent_task.task,
                    "status": "error",
                    "returncode": rc,
                    "log_file": result["log_file"],
                },
            )
            return result
        stdout, stderr, returncode, timed_out = await _run_subprocess(
            command,
            args,
            prompt_text,
            env,
            working_dir,
            event_id,
            agent_task,
        )
    except FileNotFoundError as exc:
        missing_command = exc.filename or command
        LOGGER.error("Subprocess command not found", extra={"command": missing_command})
        error_payload = {
            "prompt": _split_lines(prompt_text),
            "auth": {
                "stdout": _split_lines(auth_stdout),
                "stderr": _split_lines(auth_stderr),
                "returncode": auth_returncode,
            },
            "error": f"command not found: {missing_command}",
        }
        log_file = _write_text_log(log_file, json.dumps(error_payload, indent=2, ensure_ascii=False))
        result["log_file"] = str(log_file)
        result.update(
            {
                "status": "error",
                "error": f"command not found: {missing_command}",
                "returncode": -1,
                "auth_stdout": _split_lines(auth_stdout),
                "auth_stderr": _split_lines(auth_stderr),
                "auth_returncode": auth_returncode,
            }
        )
        LOGGER.info(
            "Agent finished: agent=%s task=%s status=%s rc=%s log=%s",
            agent_task.agent,
            agent_task.task,
            "error",
            -1,
            result["log_file"],
            extra={
                "event_id": event_id,
                "agent": agent_task.agent,
                "task": agent_task.task,
                "status": "error",
                "returncode": -1,
                "log_file": result["log_file"],
            },
        )
        return result
    finally:
        dashboard_manager.agent_finished(dashboard_key)

    if await agent_registry.is_marked_killed(event_id):
        raise AgentKilledError(event_id)

    output_payload = {
        "stdout": _split_lines(stdout),
        "stderr": _split_lines(stderr),
        "prompt": _split_lines(prompt_text),
        "stdout_trailing_newline": stdout.endswith("\n") if stdout else False,
        "stderr_trailing_newline": stderr.endswith("\n") if stderr else False,
        "auth": {
            "stdout": _split_lines(auth_stdout),
            "stderr": _split_lines(auth_stderr),
            "returncode": auth_returncode,
        },
    }
    log_file = _write_text_log(log_file, json.dumps(output_payload, indent=2, ensure_ascii=False))
    result["log_file"] = str(log_file)

    status = "ok" if returncode == 0 else "error"
    result.update(
        {
            "status": status,
            "returncode": returncode,
            "auth_stdout": _split_lines(auth_stdout),
            "auth_stderr": _split_lines(auth_stderr),
            "auth_returncode": auth_returncode,
        }
    )
    if timed_out:
        result["timed_out"] = timed_out
    if returncode != 0:
        result["error"] = stderr
    LOGGER.info(
        "Agent finished: agent=%s task=%s status=%s rc=%s log=%s",
        agent_task.agent,
        agent_task.task,
        status,
        returncode,
        result["log_file"],
        extra={
            "event_id": event_id,
            "agent": agent_task.agent,
            "task": agent_task.task,
            "status": status,
            "returncode": returncode,
            "log_file": result["log_file"],
        },
    )
    return result


async def _run_subprocess(
    command: str,
    args: List[str],
    prompt_text: str,
    env: Dict[str, str],
    working_dir: Optional[str],
    event_id: str,
    agent_task: AgentTask,
) -> tuple[str, str, int, Optional[str]]:
    proc = await _launch_subprocess(command, args, env, working_dir)
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
        return stdout_text, stderr_text, proc.returncode


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
