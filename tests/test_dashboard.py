"""Robot Dev Team Project
File: tests/test_dashboard.py
Description: Pytest coverage for dashboard endpoints.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import asyncio

import pytest
import pytest_asyncio

from app.services.dashboard import dashboard_manager


@pytest_asyncio.fixture
async def dashboard_queue(monkeypatch):
    """Enable dashboard, reset state, subscribe, and yield the queue."""
    original_enabled = dashboard_manager.enabled
    if not original_enabled:
        monkeypatch.setattr(dashboard_manager, "_enabled", True)
    monkeypatch.setattr(dashboard_manager, "_active_agents", {})
    dashboard_manager.set_loop(asyncio.get_running_loop())
    queue = await dashboard_manager.subscribe()
    yield queue
    dashboard_manager.unsubscribe(queue)


@pytest.mark.asyncio
async def test_dashboard_manager_stream(dashboard_queue):
    queue = dashboard_queue
    try:
        key = dashboard_manager.agent_started("event-1", "codex", "review")
        status_message = await asyncio.wait_for(queue.get(), timeout=0.2)
        assert status_message["type"] == "agent_status"
        assert status_message["active_agents"]

        dashboard_manager.publish_stdout("event-1", "codex", "review", "line-1\n")
        stdout_message = await asyncio.wait_for(queue.get(), timeout=0.2)
        assert stdout_message["stream"] == "stdout"
        assert stdout_message["line"] == "line-1\n"
    finally:
        dashboard_manager.agent_finished(key)


@pytest.mark.asyncio
async def test_dashboard_agent_finished_publishes_status(dashboard_queue):
    """agent_finished removes the agent and publishes an updated status message."""
    queue = dashboard_queue
    key = dashboard_manager.agent_started("event-2", "claude", "fix")
    # Drain the agent_started message
    await asyncio.wait_for(queue.get(), timeout=0.2)

    dashboard_manager.agent_finished(key)
    finished_msg = await asyncio.wait_for(queue.get(), timeout=0.2)
    assert finished_msg["type"] == "agent_status"
    assert finished_msg["active_agents"] == []


@pytest.mark.asyncio
async def test_dashboard_unsubscribe_stops_messages(dashboard_queue):
    """After unsubscribe, no further messages are delivered to the queue."""
    queue = dashboard_queue
    dashboard_manager.unsubscribe(queue)

    # Publish after unsubscribe -- queue should remain empty
    dashboard_manager.agent_started("event-3", "gemini", "review")
    assert queue.empty()


@pytest.mark.asyncio
async def test_dashboard_publish_system_message(dashboard_queue):
    """publish_system delivers a system-level stream message to subscribers."""
    queue = dashboard_queue
    dashboard_manager.publish_system("test message", "INFO", "test_logger")
    msg = await asyncio.wait_for(queue.get(), timeout=0.2)
    assert msg["type"] == "stream"
    assert msg["stream"] == "system"
    assert msg["line"] == "test message"
    assert msg["level"] == "INFO"
    assert msg["logger"] == "test_logger"
