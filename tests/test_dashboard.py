"""Robot Dev Team Project
File: tests/test_dashboard.py
Description: Pytest coverage for dashboard endpoints.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import asyncio
import threading

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.dashboard import router as dashboard_router
from app.core.config import settings
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


@pytest.fixture
def dashboard_client(monkeypatch):
    """Mount the dashboard router on a bare app with the feature enabled."""
    monkeypatch.setattr(settings, "live_dashboard_enabled", True)
    monkeypatch.setattr(dashboard_manager, "_enabled", True)
    monkeypatch.setattr(dashboard_manager, "_active_agents", {})
    monkeypatch.setattr(dashboard_manager, "_subscribers", set())
    monkeypatch.setattr(dashboard_manager, "_loop", None)

    application = FastAPI()
    application.include_router(dashboard_router)
    return TestClient(application)


def test_dashboard_websocket_streams_and_unsubscribes(dashboard_client):
    """The /dashboard/ws endpoint streams broadcasts and cleans up on disconnect.

    This is the only surface in app/ that imports starlette directly
    (WebSocketDisconnect), so it is the one place a starlette major bump can break
    without any other test noticing.
    """
    # Register an agent up front so subscribe() enqueues an immediate status frame.
    # Receiving it proves the subscription is live, which removes the race between
    # the handshake returning and the endpoint's subscribe() call completing.
    dashboard_manager.agent_started("ws-event", "claude", "review")

    with dashboard_client.websocket_connect("/dashboard/ws") as websocket:
        first = websocket.receive_json()
        assert first["type"] == "agent_status"
        assert len(dashboard_manager._subscribers) == 1

        dashboard_manager.publish_system("ws probe", "INFO", "ws_logger")
        streamed = websocket.receive_json()
        assert streamed["type"] == "stream"
        assert streamed["stream"] == "system"
        assert streamed["line"] == "ws probe"

    # Closing the client raises WebSocketDisconnect inside the endpoint; the
    # finally: block must still run and drop the queue.
    assert dashboard_manager._subscribers == set()


def test_dashboard_websocket_rejected_when_disabled(dashboard_client, monkeypatch):
    """With the dashboard disabled the socket is closed instead of subscribing.

    Run on a bounded worker thread: if the guard ever regresses the endpoint blocks
    on queue.get() forever, and a plain receive here would hang the suite instead of
    failing it.
    """
    monkeypatch.setattr(settings, "live_dashboard_enabled", False)

    captured: dict[str, BaseException] = {}

    def connect_and_read() -> None:
        try:
            with dashboard_client.websocket_connect("/dashboard/ws") as websocket:
                websocket.receive_json()
        except BaseException as exc:  # noqa: BLE001 - re-raised as an assertion below
            captured["error"] = exc

    worker = threading.Thread(target=connect_and_read, daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "socket stayed open; the disabled guard did not close it"
    error = captured.get("error")
    assert isinstance(error, WebSocketDisconnect)
    assert error.code == 1008
    assert dashboard_manager._subscribers == set()
