"""Robot Dev Team Project
File: tests/test_agents.py
Description: Pytest coverage for agent orchestration behaviours.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import asyncio
import contextlib
import json
import os
import sys

import pytest

from app.core.config import settings
from app.services import agents
from app.services.routes import AgentTask
from app.services.trigger_queue import TriggerQueue, TriggerWorkItem


@pytest.mark.asyncio
async def test_dispatch_agents_logs_output(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")
    monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "claude-pat-test")
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return "/work/projects-ro/group/project-alpha"

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)

    async def fake_auth(agent, env, working_dir):
        assert agent == "claude"
        assert env["GITLAB_TOKEN"] == "claude-pat-test"
        assert env["GITLAB_HOST"] == "gitlab.example.com"
        assert env["EXTRA_VAR"] == "1"
        assert env["ROBOT_AGENT_NAME"] == "claude"
        assert env["CURRENT_AGENT"] == "claude"
        assert working_dir == "/work/projects-ro/group/project-alpha"
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    async def fake_run_subprocess(command, args, prompt_text, env, working_dir, event_id, agent_task):
        assert command == "claude"
        assert args == ["run"]
        assert prompt_text == "prompt text"
        assert env["GITLAB_TOKEN"] == "claude-pat-test"
        assert env["GITLAB_HOST"] == "gitlab.example.com"
        assert env["EXTRA_VAR"] == "1"
        assert env["ROBOT_AGENT_NAME"] == "claude"
        assert env["CURRENT_AGENT"] == "claude"
        assert working_dir == "/work/projects-ro/group/project-alpha"
        assert event_id == "event-123"
        assert agent_task.agent == "claude"
        return "agent-output", "", 0, None

    monkeypatch.setattr(agents, "_run_subprocess", fake_run_subprocess)

    task = AgentTask(
        agent="claude",
        task="merge_request_review",
        prompt="merge_request_review.txt",
        options={
            "command": "claude",
            "args": ["run"],
            "env": {"EXTRA_VAR": "1"},
        },
    )

    results = await agents.dispatch_agents(
        "event-123",
        [task],
        {
            "payload": {},
            "project": "group/project-alpha",
            "access": "readonly",
            "route": "mention-claude",
            "base_event_uuid": "event-123",
        },
    )

    assert results == [
        {
            "agent": "claude",
            "task": "merge_request_review",
            "prompt": "merge_request_review.txt",
            "log_file": str(tmp_path / "event-123-project-alpha-mention-claude-claude.out.json"),
            "status": "ok",
            "returncode": 0,
            "working_dir": "/work/projects-ro/group/project-alpha",
            "auth_stdout": [],
            "auth_stderr": [],
            "auth_returncode": 0,
        }
    ]

    log_path = tmp_path / "event-123-project-alpha-mention-claude-claude.out.json"
    assert log_path.exists()
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert payload["stdout"] == ["agent-output"]
    assert payload["prompt"] == ["prompt text"]
    assert payload["stdout_trailing_newline"] is False
    assert payload["stderr_trailing_newline"] is False
    assert payload["auth"] == {"stdout": [], "stderr": [], "returncode": 0}


@pytest.mark.asyncio
async def test_dispatch_agents_handles_missing_command(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return "/work/projects/group/project"

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    async def fake_run_subprocess(command, args, prompt_text, env, working_dir, event_id, agent_task):  # pragma: no cover - replaced immediately
        raise FileNotFoundError(command)

    monkeypatch.setattr(agents, "_run_subprocess", fake_run_subprocess)

    task = AgentTask(
        agent="missing-cli",
        task="triage",
        prompt="triage.txt",
        options={
            "command": "missing-cli",
            "args": [],
        },
    )

    results = await agents.dispatch_agents(
        "event-456",
        [task],
        {
            "payload": {},
            "project": "group/project",
            "route": "triage",
            "base_event_uuid": "event-456",
        },
    )

    assert results[0]["status"] == "error"
    assert "command not found" in results[0]["error"]
    log_path = tmp_path / "event-456-project-triage-missing-cli.out.json"
    assert log_path.exists()
    assert "prompt text" in log_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_dispatch_agents_fails_when_project_not_found(tmp_path, monkeypatch):
    """Verify that agent dispatch fails fast when project path cannot be resolved."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "claude-pat-test")
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return None

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)

    task = AgentTask(
        agent="claude",
        task="review",
        prompt="review.txt",
        options={
            "command": "claude",
            "args": [],
        },
    )

    results = await agents.dispatch_agents(
        "event-789",
        [task],
        {
            "payload": {},
            "project": "group/missing-project",
            "route": "review",
            "base_event_uuid": "event-789",
        },
    )

    assert results[0]["status"] == "error"
    assert "Project path not found" in results[0]["error"]
    assert "group/missing-project" in results[0]["error"]
    log_path = tmp_path / "event-789-missing-project-review-claude.out.json"
    assert log_path.exists()


@pytest.mark.asyncio
async def test_dispatch_agents_updates_log_path_on_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "claude-pat-test")
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return "/work/projects-ro/group/project-alpha"

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    fallback_path = tmp_path / "fallback.json"

    def fake_write_text_log(log_file, content):
        assert "prompt text" in content
        return fallback_path

    monkeypatch.setattr(agents, "_write_text_log", fake_write_text_log)

    async def fake_run_subprocess(command, args, prompt_text, env, working_dir, event_id, agent_task):
        return "", "", 0, None

    monkeypatch.setattr(agents, "_run_subprocess", fake_run_subprocess)

    task = AgentTask(agent="claude", task="merge_request_review", options={})
    results = await agents.dispatch_agents(
        "event-999",
        [task],
        {
            "payload": {},
            "project": "group/project-alpha",
            "route": "review",
            "base_event_uuid": "event-999",
        },
    )

    assert results[0]["log_file"] == str(fallback_path)


@pytest.mark.asyncio
async def test_dispatch_agents_fallback_without_base_event_uuid(tmp_path, monkeypatch):
    """Verify _log_path falls back to event_id when base_event_uuid is missing."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "claude-pat-test")
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return "/work/projects-ro/group/project-alpha"

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    async def fake_run_subprocess(command, args, prompt_text, env, working_dir, event_id, agent_task):
        return "agent-output", "", 0, None

    monkeypatch.setattr(agents, "_run_subprocess", fake_run_subprocess)

    task = AgentTask(
        agent="claude",
        task="merge_request_review",
        prompt="merge_request_review.txt",
        options={
            "command": "claude",
            "args": ["run"],
        },
    )

    # Context without base_event_uuid or route — should fall back to event_id
    results = await agents.dispatch_agents(
        "evt-fallback-42",
        [task],
        {
            "payload": {},
            "project": "group/project-alpha",
        },
    )

    # Fallback: filename uses event_id directly with project but no route segment
    expected_log = tmp_path / "evt-fallback-42-project-alpha-claude.out.json"
    assert results[0]["log_file"] == str(expected_log)
    assert expected_log.exists()


@pytest.mark.asyncio
async def test_kill_event_terminates_running_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_max_wall_clock_seconds", 120)
    monkeypatch.setattr(settings, "agent_max_inactivity_seconds", 120)
    monkeypatch.setattr(settings, "agent_timeout_grace_seconds", 2)
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    # Use tmp_path as the working directory since it exists
    work_dir = str(tmp_path / "project")
    (tmp_path / "project").mkdir()

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return work_dir

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)
    monkeypatch.setattr(agents.dashboard_manager, "_enabled", False)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    event_id = "kill-event-123"
    task = AgentTask(
        agent="python",
        task="sleep",
        options={
            "command": sys.executable,
            "args": ["-c", "import time; time.sleep(120)"],
        },
    )

    dispatch = asyncio.create_task(
        agents.dispatch_agents(
            event_id,
            [task],
            {
                "payload": {},
                "project": "group/project",
                "route": "sleep",
                "base_event_uuid": event_id,
            },
        )
    )

    try:
        for _ in range(200):
            if event_id in agents.agent_registry._processes:
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - defensive
            raise AssertionError("agent process did not register")

        result = await agents.kill_event(event_id)
        assert result["action_taken"] is True
        assert result["killed_agents"]

        with pytest.raises(agents.AgentKilledError):
            await dispatch
    finally:
        if not dispatch.done():
            dispatch.cancel()
            with contextlib.suppress(asyncio.CancelledError, agents.AgentKilledError):
                await dispatch


@pytest.mark.asyncio
async def test_trigger_queue_reports_killed_result():
    queue = TriggerQueue()

    async def handler() -> list[dict[str, str]]:
        raise agents.AgentKilledError("event-q")

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    work_item = TriggerWorkItem(
        event_id="event-q",
        base_event_uuid="base-uuid",
        event_name="merge_request",
        action="open",
        author="bot",
        labels=[],
        mentions=[],
        route_name="default",
        handler=handler,
        future=future,
    )

    results = await queue.enqueue_many([work_item])
    assert results[0]["status"] == "killed"
    assert results[0]["event_id"] == "event-q"

    if queue._worker:
        queue._worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await queue._worker


@pytest.mark.asyncio
async def test_kill_event_terminates_process_group(tmp_path, monkeypatch):
    """Verify that kill terminates the entire process tree, not just the parent."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_max_wall_clock_seconds", 120)
    monkeypatch.setattr(settings, "agent_max_inactivity_seconds", 120)
    monkeypatch.setattr(settings, "agent_timeout_grace_seconds", 2)
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    # Use tmp_path as the working directory since it exists
    work_dir = str(tmp_path / "project")
    (tmp_path / "project").mkdir()

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return work_dir

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)
    monkeypatch.setattr(agents.dashboard_manager, "_enabled", False)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    marker_file = tmp_path / "child_running.txt"
    child_script = f"""
import subprocess
import sys
import time

marker_file = "{marker_file}"
with open(marker_file, "w") as f:
    f.write(str(__import__('os').getpid()))
child = subprocess.Popen([
    sys.executable, "-c",
    "import time; time.sleep(120)"
])
with open(marker_file, "a") as f:
    f.write("," + str(child.pid))
time.sleep(120)
"""

    event_id = "kill-group-456"
    task = AgentTask(
        agent="python",
        task="spawn-child",
        options={
            "command": sys.executable,
            "args": ["-c", child_script],
        },
    )

    dispatch = asyncio.create_task(
        agents.dispatch_agents(
            event_id,
            [task],
            {
                "payload": {},
                "project": "group/project",
                "route": "spawn-child",
                "base_event_uuid": event_id,
            },
        )
    )

    try:
        for _ in range(200):
            if marker_file.exists():
                content = marker_file.read_text()
                if "," in content:
                    break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - defensive
            raise AssertionError("child process did not start")

        content = marker_file.read_text()
        parent_pid, child_pid = [int(p) for p in content.split(",")]

        result = await agents.kill_event(event_id)
        assert result["action_taken"] is True
        assert result["killed_agents"]

        await asyncio.sleep(0.1)

        def pid_exists(pid):
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:  # pragma: no cover - defensive
                return True

        assert not pid_exists(parent_pid), "parent process should be terminated"
        assert not pid_exists(child_pid), "child process should be terminated"

        with pytest.raises(agents.AgentKilledError):
            await dispatch
    finally:
        if not dispatch.done():
            dispatch.cancel()
            with contextlib.suppress(asyncio.CancelledError, agents.AgentKilledError):
                await dispatch


@pytest.mark.asyncio
async def test_wall_clock_timeout_preserves_stderr(tmp_path, monkeypatch):
    """Wall-clock timeout preserves captured stderr and appends timeout marker."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_max_wall_clock_seconds", 2)
    monkeypatch.setattr(settings, "agent_max_inactivity_seconds", 120)
    monkeypatch.setattr(settings, "agent_timeout_grace_seconds", 1)
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    work_dir = str(tmp_path / "project")
    (tmp_path / "project").mkdir()

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return work_dir

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)
    monkeypatch.setattr(agents.dashboard_manager, "_enabled", False)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    # Script that writes to stderr and then sleeps
    script = (
        "import sys, time; "
        "sys.stderr.write('partial diagnostic output'); sys.stderr.flush(); "
        "time.sleep(120)"
    )

    event_id = "timeout-wall-clock"
    task = AgentTask(
        agent="python",
        task="timeout-test",
        options={
            "command": sys.executable,
            "args": ["-c", script],
        },
    )

    results = await agents.dispatch_agents(
        event_id,
        [task],
        {
            "payload": {},
            "project": "group/project",
            "route": "timeout-test",
            "base_event_uuid": event_id,
        },
    )

    result = results[0]
    assert result["status"] == "error"
    assert result["returncode"] == -1
    assert result["timed_out"] == "wall_clock"

    # Verify stderr is preserved (not replaced)
    log_path = tmp_path / f"{event_id}-project-timeout-test-python.out.json"
    assert log_path.exists()
    log_data = json.loads(log_path.read_text(encoding="utf-8"))
    stderr_lines = log_data["stderr"]
    stderr_joined = "\n".join(stderr_lines)
    assert "partial diagnostic output" in stderr_joined
    assert "[System] Agent timed out (wall_clock)" in stderr_joined


@pytest.mark.asyncio
async def test_inactivity_timeout(tmp_path, monkeypatch):
    """Inactivity watchdog triggers when agent produces no output."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_max_wall_clock_seconds", 120)
    monkeypatch.setattr(settings, "agent_max_inactivity_seconds", 2)
    monkeypatch.setattr(settings, "agent_timeout_grace_seconds", 1)
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    work_dir = str(tmp_path / "project")
    (tmp_path / "project").mkdir()

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return work_dir

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)
    monkeypatch.setattr(agents.dashboard_manager, "_enabled", False)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    # Script that produces no output and sleeps
    script = "import time; time.sleep(120)"

    event_id = "timeout-inactivity"
    task = AgentTask(
        agent="python",
        task="silent-test",
        options={
            "command": sys.executable,
            "args": ["-c", script],
        },
    )

    results = await agents.dispatch_agents(
        event_id,
        [task],
        {
            "payload": {},
            "project": "group/project",
            "route": "silent-test",
            "base_event_uuid": event_id,
        },
    )

    result = results[0]
    assert result["status"] == "error"
    assert result["returncode"] == -1
    assert result["timed_out"] == "inactivity"


@pytest.mark.asyncio
async def test_stderr_only_triggers_inactivity_timeout(tmp_path, monkeypatch):
    """Continuous stderr with zero stdout must trigger the inactivity watchdog.

    Reproduces the failure mode from issue #87: an agent CLI in an infinite
    retry loop produces only stderr (stack traces, error bodies). Previously
    this reset the activity tracker and the agent hung until the 2-hour
    wall-clock limit. With the fix, only stdout resets the watchdog.
    """
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_max_wall_clock_seconds", 120)
    monkeypatch.setattr(settings, "agent_max_inactivity_seconds", 2)
    monkeypatch.setattr(settings, "agent_timeout_grace_seconds", 1)
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    work_dir = str(tmp_path / "project")
    (tmp_path / "project").mkdir()

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return work_dir

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)
    monkeypatch.setattr(agents.dashboard_manager, "_enabled", False)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    # Script that continuously writes to stderr every 0.5s but never writes to stdout
    script = (
        "import sys, time; "
        "start = time.time(); "
        "[sys.stderr.write('retry error\\n') or sys.stderr.flush() or time.sleep(0.5) "
        "for _ in iter(lambda: time.time() - start < 120, False)]"
    )

    event_id = "stderr-only-retry"
    task = AgentTask(
        agent="python",
        task="retry-test",
        options={
            "command": sys.executable,
            "args": ["-c", script],
        },
    )

    results = await agents.dispatch_agents(
        event_id,
        [task],
        {
            "payload": {},
            "project": "group/project",
            "route": "retry-test",
            "base_event_uuid": event_id,
        },
    )

    result = results[0]
    assert result["status"] == "error"
    assert result["returncode"] == -1
    assert result["timed_out"] == "inactivity"


@pytest.mark.asyncio
async def test_active_agent_not_interrupted_by_inactivity(tmp_path, monkeypatch):
    """Agent producing regular output should not be killed by inactivity watchdog."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_max_wall_clock_seconds", 120)
    monkeypatch.setattr(settings, "agent_max_inactivity_seconds", 2)
    monkeypatch.setattr(settings, "agent_timeout_grace_seconds", 1)
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    work_dir = str(tmp_path / "project")
    (tmp_path / "project").mkdir()

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return work_dir

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)
    monkeypatch.setattr(agents.dashboard_manager, "_enabled", False)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    # Script that continuously produces output every 0.5s and then exits after 3s
    script = (
        "import sys, time; "
        "start = time.time(); "
        "[sys.stdout.write('tick\\n') or sys.stdout.flush() or time.sleep(0.5) "
        "for _ in iter(lambda: time.time() - start < 3, False)]"
    )

    event_id = "active-agent"
    task = AgentTask(
        agent="python",
        task="active-test",
        options={
            "command": sys.executable,
            "args": ["-c", script],
        },
    )

    results = await agents.dispatch_agents(
        event_id,
        [task],
        {
            "payload": {},
            "project": "group/project",
            "route": "active-test",
            "base_event_uuid": event_id,
        },
    )

    result = results[0]
    # Agent should complete successfully despite inactivity timeout being 2s
    # because it keeps producing output every 0.5s
    assert result["status"] == "ok"
    assert result["returncode"] == 0
    assert "timed_out" not in result


@pytest.mark.asyncio
async def test_per_route_timeout_override(tmp_path, monkeypatch):
    """Per-route timeout overrides take precedence over global settings."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    # Set global timeouts very high so they won't trigger
    monkeypatch.setattr(settings, "agent_max_wall_clock_seconds", 7200)
    monkeypatch.setattr(settings, "agent_max_inactivity_seconds", 900)
    monkeypatch.setattr(settings, "agent_timeout_grace_seconds", 1)
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    work_dir = str(tmp_path / "project")
    (tmp_path / "project").mkdir()

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return work_dir

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)
    monkeypatch.setattr(agents.dashboard_manager, "_enabled", False)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    # Script that sleeps without output
    script = "import time; time.sleep(120)"

    event_id = "route-override"
    # AgentTask with per-route override for inactivity (2s)
    task = AgentTask(
        agent="python",
        task="override-test",
        options={
            "command": sys.executable,
            "args": ["-c", script],
        },
        max_inactivity_seconds=2,
    )

    results = await agents.dispatch_agents(
        event_id,
        [task],
        {
            "payload": {},
            "project": "group/project",
            "route": "override-test",
            "base_event_uuid": event_id,
        },
    )

    result = results[0]
    assert result["status"] == "error"
    assert result["timed_out"] == "inactivity"


@pytest.mark.asyncio
async def test_timeout_result_includes_timed_out_key(tmp_path, monkeypatch):
    """Verify timed_out key is present in result for timeout and absent for normal completion."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "claude-pat-test")
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return "/work/projects/group/project"

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    # Simulate a timeout response
    async def fake_run_timeout(command, args, prompt_text, env, working_dir, event_id, agent_task):
        return "output", "partial stderr\n\n[System] Agent timed out (wall_clock)", -1, "wall_clock"

    monkeypatch.setattr(agents, "_run_subprocess", fake_run_timeout)

    task = AgentTask(agent="claude", task="review", options={})
    results = await agents.dispatch_agents(
        "evt-timeout",
        [task],
        {"payload": {}, "project": "group/project", "route": "test", "base_event_uuid": "evt-timeout"},
    )

    assert results[0]["timed_out"] == "wall_clock"
    assert results[0]["status"] == "error"

    # Simulate a normal response - timed_out should not be in result
    async def fake_run_ok(command, args, prompt_text, env, working_dir, event_id, agent_task):
        return "output", "", 0, None

    monkeypatch.setattr(agents, "_run_subprocess", fake_run_ok)

    results = await agents.dispatch_agents(
        "evt-ok",
        [task],
        {"payload": {}, "project": "group/project", "route": "test", "base_event_uuid": "evt-ok"},
    )

    assert "timed_out" not in results[0]
    assert results[0]["status"] == "ok"


@pytest.mark.asyncio
async def test_wall_clock_preempts_active_output(tmp_path, monkeypatch):
    """Wall-clock timeout fires even when agent is actively producing output."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_max_wall_clock_seconds", 3)
    monkeypatch.setattr(settings, "agent_max_inactivity_seconds", 120)
    monkeypatch.setattr(settings, "agent_timeout_grace_seconds", 1)
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    work_dir = str(tmp_path / "project")
    (tmp_path / "project").mkdir()

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return work_dir

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)
    monkeypatch.setattr(agents.dashboard_manager, "_enabled", False)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    # Script that continuously writes output every 0.3s (well within the 120s
    # inactivity limit) but runs far longer than the 3s wall-clock limit.
    script = (
        "import sys, time\n"
        "for _ in range(200):\n"
        "    sys.stdout.write('tick\\n')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.3)\n"
    )

    event_id = "wall-clock-preempts-active"
    task = AgentTask(
        agent="python",
        task="preempt-test",
        options={
            "command": sys.executable,
            "args": ["-c", script],
        },
    )

    results = await agents.dispatch_agents(
        event_id,
        [task],
        {
            "payload": {},
            "project": "group/project",
            "route": "preempt-test",
            "base_event_uuid": event_id,
        },
    )

    result = results[0]
    assert result["status"] == "error"
    assert result["timed_out"] == "wall_clock"


@pytest.mark.asyncio
async def test_sigterm_escalation_to_sigkill(tmp_path, monkeypatch):
    """Process that ignores SIGTERM is escalated to SIGKILL after grace period."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "agent_max_wall_clock_seconds", 2)
    monkeypatch.setattr(settings, "agent_max_inactivity_seconds", 120)
    monkeypatch.setattr(settings, "agent_timeout_grace_seconds", 2)
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    work_dir = str(tmp_path / "project")
    (tmp_path / "project").mkdir()

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return work_dir

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)
    monkeypatch.setattr(agents.dashboard_manager, "_enabled", False)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    # Script that traps SIGTERM and ignores it, continuing to sleep.
    # The watchdog should escalate to SIGKILL after the grace period.
    script = (
        "import signal, time; "
        "signal.signal(signal.SIGTERM, lambda *a: None); "
        "time.sleep(120)"
    )

    event_id = "sigterm-escalation"
    task = AgentTask(
        agent="python",
        task="trap-test",
        options={
            "command": sys.executable,
            "args": ["-c", script],
        },
    )

    results = await agents.dispatch_agents(
        event_id,
        [task],
        {
            "payload": {},
            "project": "group/project",
            "route": "trap-test",
            "base_event_uuid": event_id,
        },
    )

    result = results[0]
    assert result["status"] == "error"
    assert result["returncode"] == -1
    assert result["timed_out"] == "wall_clock"


@pytest.mark.asyncio
async def test_gitlab_token_injected_into_subprocess_env(tmp_path, monkeypatch):
    """GITLAB_TOKEN from agent-specific env var is passed to the subprocess."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")
    monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "claude-pat-inject")
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return "/work/projects/group/project"

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    captured_env = {}

    async def fake_run_subprocess(command, args, prompt_text, env, working_dir, event_id, agent_task):
        captured_env.update(env)
        return "output", "", 0, None

    monkeypatch.setattr(agents, "_run_subprocess", fake_run_subprocess)

    task = AgentTask(agent="claude", task="review", options={"command": "claude", "args": []})
    await agents.dispatch_agents(
        "evt-inject",
        [task],
        {"payload": {}, "project": "group/project", "route": "test", "base_event_uuid": "evt-inject"},
    )

    assert captured_env["GITLAB_TOKEN"] == "claude-pat-inject"
    assert captured_env["GITLAB_HOST"] == "gitlab.example.com"
    assert captured_env["CURRENT_AGENT"] == "claude"


@pytest.mark.asyncio
async def test_empty_agent_token_fails_hard(tmp_path, monkeypatch):
    """Agent with empty token env var produces an error result."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setenv("CODEX_AGENT_GITLAB_TOKEN", "")
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    task = AgentTask(agent="codex", task="review", options={"command": "codex", "args": []})
    results = await agents.dispatch_agents(
        "evt-missing-token",
        [task],
        {"payload": {}, "project": "group/project", "route": "test", "base_event_uuid": "evt-missing-token"},
    )

    assert results[0]["status"] == "error"
    assert "Missing GitLab token" in results[0]["error"]
    assert "CODEX_AGENT_GITLAB_TOKEN" in results[0]["error"]


@pytest.mark.asyncio
async def test_unconfigured_agent_skips_token_injection(tmp_path, monkeypatch):
    """Agent without a token env var proceeds without GITLAB_TOKEN injection."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.delenv("CUSTOM_BOT_AGENT_GITLAB_TOKEN", raising=False)
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return "/work/projects/group/project"

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    captured_env = {}

    async def fake_run_subprocess(command, args, prompt_text, env, working_dir, event_id, agent_task):
        captured_env.update(env)
        return "output", "", 0, None

    monkeypatch.setattr(agents, "_run_subprocess", fake_run_subprocess)

    task = AgentTask(agent="custom-bot", task="review", options={"command": "custom-bot", "args": []})
    results = await agents.dispatch_agents(
        "evt-unknown",
        [task],
        {"payload": {}, "project": "group/project", "route": "test", "base_event_uuid": "evt-unknown"},
    )

    assert results[0]["status"] == "ok"
    assert "GITLAB_TOKEN" not in captured_env


@pytest.mark.asyncio
async def test_each_agent_gets_own_token(tmp_path, monkeypatch):
    """Sequential multi-agent dispatch injects the correct token per agent."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")
    monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "claude-pat")
    monkeypatch.setenv("GEMINI_AGENT_GITLAB_TOKEN", "gemini-pat")
    monkeypatch.setenv("CODEX_AGENT_GITLAB_TOKEN", "codex-pat")
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return "/work/projects/group/project"

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    tokens_seen = []

    async def fake_run_subprocess(command, args, prompt_text, env, working_dir, event_id, agent_task):
        tokens_seen.append((agent_task.agent, env.get("GITLAB_TOKEN")))
        return "output", "", 0, None

    monkeypatch.setattr(agents, "_run_subprocess", fake_run_subprocess)

    tasks = [
        AgentTask(agent="claude", task="review", options={"command": "claude", "args": []}),
        AgentTask(agent="gemini", task="review", options={"command": "gemini", "args": []}),
        AgentTask(agent="codex", task="review", options={"command": "codex", "args": []}),
    ]
    results = await agents.dispatch_agents(
        "evt-multi",
        tasks,
        {"payload": {}, "project": "group/project", "route": "test", "base_event_uuid": "evt-multi"},
    )

    assert all(r["status"] == "ok" for r in results)
    assert tokens_seen == [
        ("claude", "claude-pat"),
        ("gemini", "gemini-pat"),
        ("codex", "codex-pat"),
    ]


@pytest.mark.asyncio
async def test_authenticate_agent_still_called(tmp_path, monkeypatch):
    """_authenticate_agent (glab-usr) is still called even with token injection."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "claude-pat-test")
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return "/work/projects/group/project"

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)

    auth_called = []

    async def fake_auth(agent, env, working_dir):
        auth_called.append(agent)
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    async def fake_run_subprocess(command, args, prompt_text, env, working_dir, event_id, agent_task):
        return "output", "", 0, None

    monkeypatch.setattr(agents, "_run_subprocess", fake_run_subprocess)

    task = AgentTask(agent="claude", task="review", options={"command": "claude", "args": []})
    await agents.dispatch_agents(
        "evt-auth-check",
        [task],
        {"payload": {}, "project": "group/project", "route": "test", "base_event_uuid": "evt-auth-check"},
    )

    assert auth_called == ["claude"]


@pytest.mark.asyncio
async def test_route_env_cannot_override_agent_token(tmp_path, monkeypatch):
    """Route config env stanza cannot override the injected GITLAB_TOKEN."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")
    monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "correct-agent-pat")
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return "/work/projects/group/project"

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)

    async def fake_auth(agent, env, working_dir):
        return "", "", 0

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    captured_env = {}

    async def fake_run_subprocess(command, args, prompt_text, env, working_dir, event_id, agent_task):
        captured_env.update(env)
        return "output", "", 0, None

    monkeypatch.setattr(agents, "_run_subprocess", fake_run_subprocess)

    # Route config includes a GITLAB_TOKEN that should be overridden
    task = AgentTask(
        agent="claude",
        task="review",
        options={
            "command": "claude",
            "args": [],
            "env": {"GITLAB_TOKEN": "route-override-token", "GITLAB_HOST": "route-host.example.com"},
        },
    )
    await agents.dispatch_agents(
        "evt-override",
        [task],
        {"payload": {}, "project": "group/project", "route": "test", "base_event_uuid": "evt-override"},
    )

    # Agent token takes precedence over route env
    assert captured_env["GITLAB_TOKEN"] == "correct-agent-pat"
    # GITLAB_HOST from settings also takes precedence over route env
    assert captured_env["GITLAB_HOST"] == "gitlab.example.com"


@pytest.mark.asyncio
async def test_auth_failure_still_aborts_dispatch(tmp_path, monkeypatch):
    """Dispatch aborts when _authenticate_agent fails, even with token injection."""
    monkeypatch.setattr(settings, "run_logs_dir", str(tmp_path))
    monkeypatch.setattr(settings, "enable_branch_switch", False)
    monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "claude-pat-test")
    monkeypatch.setattr(agents, "render_prompt", lambda _name, _context: "prompt text")

    async def fake_ensure_project_exists(project_path, access="readonly", clone_url=None, agent=None):
        return "/work/projects/group/project"

    monkeypatch.setattr(agents.PROJECT_PATHS, "ensure_project_exists", fake_ensure_project_exists)

    async def fake_auth(agent, env, working_dir):
        return "", "auth error", 1

    monkeypatch.setattr(agents, "_authenticate_agent", fake_auth)

    task = AgentTask(agent="claude", task="review", options={"command": "claude", "args": []})
    results = await agents.dispatch_agents(
        "evt-auth-fail",
        [task],
        {"payload": {}, "project": "group/project", "route": "test", "base_event_uuid": "evt-auth-fail"},
    )

    assert results[0]["status"] == "error"
    assert "authentication failed" in results[0]["error"]


@pytest.mark.asyncio
async def test_authenticate_agent_timeout_returns_error():
    """A hanging glab-usr call is killed and returns a non-zero status."""
    async def _hanging_communicate():
        await asyncio.sleep(10)
        return (b"", b"")

    mock_proc = asyncio.create_subprocess_exec.__class__.__new__(
        asyncio.create_subprocess_exec.__class__,
    ) if False else type("FakeProc", (), {})()
    mock_proc.communicate = _hanging_communicate
    mock_proc.kill = lambda: None

    async def _wait():
        return 0
    mock_proc.wait = _wait

    from unittest.mock import patch, AsyncMock
    from app.services.git_runtime import git_auth_lock

    # Ensure the lock is free before the test
    assert not git_auth_lock.locked()

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)), \
         patch("app.services.agents.GLAB_USR_TIMEOUT_SECONDS", 0.05):
        stdout, stderr, rc = await agents._authenticate_agent("claude", {}, None)

    assert rc == -1
    assert "timed out" in stderr
    # Lock must be released after timeout
    assert not git_auth_lock.locked()


@pytest.mark.asyncio
async def test_authenticate_agent_normal_completes():
    """A fast glab-usr call completes normally with the timeout in place."""
    from unittest.mock import patch, AsyncMock

    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"authenticated\n", b"")
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        stdout, stderr, rc = await agents._authenticate_agent("claude", {}, None)

    assert rc == 0
    assert stdout == "authenticated\n"
