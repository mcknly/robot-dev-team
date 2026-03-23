"""Robot Dev Team Project
File: tests/test_trigger_queue.py
Description: Pytest coverage for TriggerQueue worker loop.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import asyncio

import pytest

from app.services.agents import AgentKilledError
from app.services.trigger_queue import TriggerQueue, TriggerWorkItem


def _make_work_item(
    event_id: str = "evt-1",
    handler=None,
):
    """Create a minimal TriggerWorkItem for testing."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    async def default_handler():
        return [{"agent": "claude", "status": "ok"}]

    return TriggerWorkItem(
        event_id=event_id,
        base_event_uuid=event_id,
        event_name="Merge Request Hook",
        action="open",
        author="tester",
        labels=[],
        mentions=[],
        route_name="test-route",
        handler=handler or default_handler,
        future=future,
    )


@pytest.mark.asyncio
async def test_trigger_queue_basic_round_trip():
    """Enqueue a single item and verify the handler is called and result returned."""
    queue = TriggerQueue(hold_seconds=0)

    item = _make_work_item()
    results = await queue.enqueue_many([item])

    assert len(results) == 1
    assert results[0]["status"] == "ok"
    assert results[0]["event_id"] == "evt-1"
    assert results[0]["agents"] == [{"agent": "claude", "status": "ok"}]


@pytest.mark.asyncio
async def test_trigger_queue_killed_status():
    """When handler raises AgentKilledError, the result status is 'killed'."""
    queue = TriggerQueue(hold_seconds=0)

    async def killed_handler():
        raise AgentKilledError("operator killed")

    item = _make_work_item(handler=killed_handler)
    results = await queue.enqueue_many([item])

    assert len(results) == 1
    assert results[0]["status"] == "killed"
    assert "operator killed" in results[0]["error"]
