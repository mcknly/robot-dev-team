"""Robot Dev Team Project
File: tests/test_mention_hold.py
Description: Tests for mention hold buffer deduplication (issue #69).
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import webhooks
from app.core.config import settings
from app.main import app
from app.services.routes import AgentTask, RouteMatch
from app.services.trigger_queue import MentionHoldBuffer, TriggerQueue, TriggerWorkItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class DummyDeduplicator:
    async def mark(self, _key):
        return True


def _make_future(loop=None):
    if loop is None:
        loop = asyncio.get_event_loop()
    return loop.create_future()


async def _noop_handler():
    return [{"agent": "claude", "status": "ok"}]


def _make_item(loop, **overrides):
    defaults = {
        "event_id": "evt-1",
        "base_event_uuid": "base-1",
        "event_name": "Note Hook",
        "action": "create",
        "author": "user",
        "labels": [],
        "mentions": ["claude"],
        "route_name": "mention-claude",
        "handler": _noop_handler,
        "future": _make_future(loop),
        "project_path": "group/project",
        "iid": 42,
        "is_mention_trigger": True,
        "is_assignment_trigger": False,
        "hold_agents": ["claude"],
    }
    defaults.update(overrides)
    return TriggerWorkItem(**defaults)


# ---------------------------------------------------------------------------
# MentionHoldBuffer unit tests
# ---------------------------------------------------------------------------

class TestMentionHoldBuffer:
    """Unit tests for MentionHoldBuffer."""

    @pytest.mark.asyncio
    async def test_mention_promoted_after_hold_expires(self):
        """A held mention is promoted to the queue after the hold timer expires."""
        buf = MentionHoldBuffer(hold_seconds=0.1)
        loop = asyncio.get_event_loop()
        item = _make_item(loop)
        promoted = []

        buf.hold(("group/project", 42, "claude"), item, lambda i: promoted.append(i))

        assert buf.has_pending(("group/project", 42, "claude"))
        await asyncio.sleep(0.2)
        assert len(promoted) == 1
        assert promoted[0] is item
        assert not buf.has_pending(("group/project", 42, "claude"))

    @pytest.mark.asyncio
    async def test_assignment_cancels_held_mention(self):
        """An assignment cancels a pending held mention."""
        buf = MentionHoldBuffer(hold_seconds=5.0)
        loop = asyncio.get_event_loop()
        item = _make_item(loop)
        promoted = []

        key = ("group/project", 42, "claude")
        buf.hold(key, item, lambda i: promoted.append(i))
        assert buf.has_pending(key)

        cancelled = buf.cancel(key)
        assert cancelled
        assert not buf.has_pending(key)
        assert item.future.done()
        result = item.future.result()
        assert result["status"] == "suppressed"
        assert result["reason"] == "assignment-coalesced"

        # Timer should be cancelled, nothing promoted
        await asyncio.sleep(0.1)
        assert len(promoted) == 0

    @pytest.mark.asyncio
    async def test_cancel_returns_false_for_unknown_key(self):
        """Cancelling a non-existent key returns False."""
        buf = MentionHoldBuffer(hold_seconds=5.0)
        assert not buf.cancel(("group/project", 99, "claude"))

    @pytest.mark.asyncio
    async def test_cancel_for_item_suppresses_matching_agents(self):
        """cancel_for_item suppresses held mentions matching the assignment item's agents."""
        buf = MentionHoldBuffer(hold_seconds=5.0)
        loop = asyncio.get_event_loop()
        mention_item = _make_item(loop)
        promoted = []

        buf.hold(("group/project", 42, "claude"), mention_item, lambda i: promoted.append(i))

        assign_item = _make_item(
            loop,
            event_id="evt-assign",
            is_mention_trigger=False,
            is_assignment_trigger=True,
            hold_agents=["claude"],
        )
        suppressed = buf.cancel_for_item(assign_item)
        assert len(suppressed) == 1
        assert suppressed[0] == ("group/project", 42, "claude")

    @pytest.mark.asyncio
    async def test_different_agents_tracked_independently(self):
        """Mentions for different agents are held independently."""
        buf = MentionHoldBuffer(hold_seconds=5.0)
        loop = asyncio.get_event_loop()

        item_claude = _make_item(loop, hold_agents=["claude"])
        item_gemini = _make_item(loop, event_id="evt-2", mentions=["gemini"], hold_agents=["gemini"], future=_make_future(loop))

        buf.hold(("group/project", 42, "claude"), item_claude, lambda i: None)
        buf.hold(("group/project", 42, "gemini"), item_gemini, lambda i: None)

        assert buf.has_pending(("group/project", 42, "claude"))
        assert buf.has_pending(("group/project", 42, "gemini"))

        # Cancel only claude
        buf.cancel(("group/project", 42, "claude"))
        assert not buf.has_pending(("group/project", 42, "claude"))
        assert buf.has_pending(("group/project", 42, "gemini"))

    @pytest.mark.asyncio
    async def test_different_projects_tracked_independently(self):
        """Same agent on different projects are tracked independently."""
        buf = MentionHoldBuffer(hold_seconds=5.0)
        loop = asyncio.get_event_loop()

        item_a = _make_item(loop, project_path="group/proj-a")
        item_b = _make_item(loop, event_id="evt-2", project_path="group/proj-b", future=_make_future(loop))

        buf.hold(("group/proj-a", 42, "claude"), item_a, lambda i: None)
        buf.hold(("group/proj-b", 42, "claude"), item_b, lambda i: None)

        buf.cancel(("group/proj-a", 42, "claude"))
        assert not buf.has_pending(("group/proj-a", 42, "claude"))
        assert buf.has_pending(("group/proj-b", 42, "claude"))

    @pytest.mark.asyncio
    async def test_replace_existing_held_mention(self):
        """Holding the same key twice replaces the first and cancels its timer."""
        buf = MentionHoldBuffer(hold_seconds=5.0)
        loop = asyncio.get_event_loop()

        item1 = _make_item(loop, event_id="evt-1")
        item2 = _make_item(loop, event_id="evt-2", future=_make_future(loop))

        key = ("group/project", 42, "claude")
        buf.hold(key, item1, lambda i: None)
        buf.hold(key, item2, lambda i: None)

        assert buf.has_pending(key)
        # Cancel should resolve item2's future, not item1's
        buf.cancel(key)
        assert item2.future.done()
        assert item2.future.result()["status"] == "suppressed"

    @pytest.mark.asyncio
    async def test_replace_resolves_old_future(self):
        """Replacing a held mention resolves the old item's future with 'mention-replaced'."""
        buf = MentionHoldBuffer(hold_seconds=5.0)
        loop = asyncio.get_event_loop()

        item1 = _make_item(loop, event_id="evt-old")
        item2 = _make_item(loop, event_id="evt-new", future=_make_future(loop))

        key = ("group/project", 42, "claude")
        buf.hold(key, item1, lambda i: None)

        # item1 should not be resolved yet
        assert not item1.future.done()

        # Replacing with item2 should resolve item1's future
        buf.hold(key, item2, lambda i: None)
        assert item1.future.done()
        result = item1.future.result()
        assert result["status"] == "suppressed"
        assert result["reason"] == "mention-replaced"
        assert result["event_id"] == "evt-old"

        # item2 should still be pending
        assert not item2.future.done()
        assert buf.has_pending(key)

    @pytest.mark.asyncio
    async def test_cancel_for_item_without_metadata_returns_empty(self):
        """cancel_for_item returns empty list when item has no project_path or iid."""
        buf = MentionHoldBuffer(hold_seconds=5.0)
        loop = asyncio.get_event_loop()
        item = _make_item(loop, project_path=None, iid=None)
        assert buf.cancel_for_item(item) == []

    # -------------------------------------------------------------------
    # Recent-assignment memory tests (issue #75)
    # -------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_recent_assignment_suppresses_mention_immediately(self):
        """A mention arriving after a recent assignment is suppressed immediately."""
        buf = MentionHoldBuffer(hold_seconds=5.0)
        loop = asyncio.get_event_loop()
        key = ("group/project", 42, "claude")

        # Record an assignment
        buf.record_assignment(key)
        assert buf.has_recent_assignment(key)

        # Now hold a mention for the same key -- should be suppressed immediately
        item = _make_item(loop)
        promoted = []
        buf.hold(key, item, lambda i: promoted.append(i))

        assert item.future.done()
        result = item.future.result()
        assert result["status"] == "suppressed"
        assert result["reason"] == "assignment-recent"
        # Item should NOT be in the pending buffer
        assert not buf.has_pending(key)
        # Nothing should be promoted
        await asyncio.sleep(0.1)
        assert len(promoted) == 0

    @pytest.mark.asyncio
    async def test_recent_assignment_expires_after_hold_window(self):
        """A recent assignment record expires after hold_seconds, allowing mentions through."""
        buf = MentionHoldBuffer(hold_seconds=0.1)
        loop = asyncio.get_event_loop()
        key = ("group/project", 42, "claude")

        buf.record_assignment(key)
        assert buf.has_recent_assignment(key)

        # Wait for the record to expire
        await asyncio.sleep(0.2)
        assert not buf.has_recent_assignment(key)

        # A mention after expiry should be held normally
        item = _make_item(loop)
        promoted = []
        buf.hold(key, item, lambda i: promoted.append(i))
        assert buf.has_pending(key)
        assert not item.future.done()

    @pytest.mark.asyncio
    async def test_recent_assignment_resets_on_repeat(self):
        """Recording the same assignment key twice resets the expiry timer."""
        buf = MentionHoldBuffer(hold_seconds=0.2)
        loop = asyncio.get_event_loop()
        key = ("group/project", 42, "claude")

        buf.record_assignment(key)
        await asyncio.sleep(0.1)

        # Re-record before expiry -- should reset the timer
        buf.record_assignment(key)
        await asyncio.sleep(0.15)

        # Should still be active (0.1 + 0.15 = 0.25 > 0.2 from first, but
        # only 0.15 from second recording which is < 0.2)
        assert buf.has_recent_assignment(key)

        # Wait for second timer to expire
        await asyncio.sleep(0.1)
        assert not buf.has_recent_assignment(key)

    @pytest.mark.asyncio
    async def test_cancel_for_item_records_assignment(self):
        """cancel_for_item records assignment keys for future mention suppression."""
        buf = MentionHoldBuffer(hold_seconds=5.0)
        loop = asyncio.get_event_loop()

        assign_item = _make_item(
            loop,
            event_id="evt-assign",
            is_mention_trigger=False,
            is_assignment_trigger=True,
            hold_agents=["claude"],
        )
        buf.cancel_for_item(assign_item)

        # The assignment should now be recorded
        assert buf.has_recent_assignment(("group/project", 42, "claude"))


# ---------------------------------------------------------------------------
# TriggerQueue mention-hold integration tests
# ---------------------------------------------------------------------------

class TestTriggerQueueMentionHold:
    """Tests for TriggerQueue with mention hold buffer integration."""

    @pytest.mark.asyncio
    async def test_mention_only_dispatches_after_hold(self):
        """A mention-only trigger dispatches after the hold window expires."""
        queue = TriggerQueue(hold_seconds=0.1)
        loop = asyncio.get_event_loop()
        item = _make_item(loop)

        results = await queue.enqueue_many([item])

        assert len(results) == 1
        assert results[0]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_hold_disabled_dispatches_immediately(self):
        """When hold_seconds=0, mentions are dispatched immediately."""
        queue = TriggerQueue(hold_seconds=0)
        loop = asyncio.get_event_loop()
        item = _make_item(loop)

        results = await queue.enqueue_many([item])

        assert len(results) == 1
        assert results[0]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_mention_without_metadata_dispatches_immediately(self):
        """A mention trigger without project_path/iid bypasses the hold buffer."""
        queue = TriggerQueue(hold_seconds=5.0)
        loop = asyncio.get_event_loop()
        item = _make_item(loop, project_path=None, iid=None, hold_agents=[])

        results = await queue.enqueue_many([item])

        assert len(results) == 1
        assert results[0]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_assignment_suppresses_held_mention(self):
        """An assignment arriving during the hold window suppresses the mention."""
        queue = TriggerQueue(hold_seconds=10.0)
        loop = asyncio.get_event_loop()

        mention_item = _make_item(loop, event_id="evt-mention")

        # Enqueue mention in background (it will be held)
        mention_task = asyncio.create_task(queue.enqueue_many([mention_item]))

        # Brief wait to ensure the mention is held
        await asyncio.sleep(0.05)

        # Now submit the assignment
        assign_item = _make_item(
            loop,
            event_id="evt-assign",
            event_name="Issue Hook",
            is_mention_trigger=False,
            is_assignment_trigger=True,
            hold_agents=["claude"],
            future=_make_future(loop),
        )
        assign_results = await queue.enqueue_many([assign_item])

        # The assignment should have been dispatched
        assert len(assign_results) == 1
        assert assign_results[0]["status"] == "ok"

        # The mention should have been suppressed
        mention_results = await mention_task
        assert len(mention_results) == 1
        assert mention_results[0]["status"] == "suppressed"
        assert mention_results[0]["reason"] == "assignment-coalesced"

    @pytest.mark.asyncio
    async def test_multi_agent_mention_dispatches_once(self):
        """A single item held under multiple agent keys is dispatched only once."""
        handler_calls = []

        async def counting_handler():
            handler_calls.append(1)
            return [{"agent": "claude", "status": "ok"}]

        queue = TriggerQueue(hold_seconds=0.1)
        loop = asyncio.get_event_loop()
        item = _make_item(
            loop,
            mentions=["claude", "gemini"],
            hold_agents=["claude", "gemini"],
            handler=counting_handler,
        )

        results = await queue.enqueue_many([item])

        assert len(results) == 1
        assert results[0]["status"] == "ok"
        # The handler should have been invoked exactly once despite two hold keys
        assert len(handler_calls) == 1

    @pytest.mark.asyncio
    async def test_multi_agent_suppression_cancels_one_agent_only(self):
        """Suppressing one agent key does not prevent promotion via the other key."""
        queue = TriggerQueue(hold_seconds=0.2)
        loop = asyncio.get_event_loop()
        item = _make_item(
            loop,
            mentions=["claude", "gemini"],
            hold_agents=["claude", "gemini"],
        )

        # Enqueue in background (will be held)
        mention_task = asyncio.create_task(queue.enqueue_many([item]))

        await asyncio.sleep(0.05)

        # Cancel only claude's key; gemini's timer should still promote the item
        cancelled = queue.hold_buffer.cancel(("group/project", 42, "claude"))
        # cancel() resolves the future with suppressed, so the item's future is done
        # However the item should NOT be promoted again when gemini's timer fires
        assert cancelled

        results = await mention_task
        assert len(results) == 1
        # Future was resolved by cancel() with suppressed status
        assert results[0]["status"] == "suppressed"

    @pytest.mark.asyncio
    async def test_non_mention_items_bypass_hold(self):
        """Non-mention trigger items are enqueued directly without hold."""
        queue = TriggerQueue(hold_seconds=5.0)
        loop = asyncio.get_event_loop()
        item = _make_item(
            loop,
            is_mention_trigger=False,
            is_assignment_trigger=False,
        )

        results = await queue.enqueue_many([item])
        assert len(results) == 1
        assert results[0]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_assignment_before_mention_suppresses_immediately(self):
        """An assignment enqueued before a mention suppresses the mention immediately (issue #75)."""
        queue = TriggerQueue(hold_seconds=10.0)
        loop = asyncio.get_event_loop()

        # Step 1: Enqueue the assignment first
        assign_item = _make_item(
            loop,
            event_id="evt-assign",
            event_name="Issue Hook",
            is_mention_trigger=False,
            is_assignment_trigger=True,
            hold_agents=["claude"],
        )
        assign_task = asyncio.create_task(queue.enqueue_many([assign_item]))

        # Brief wait for the assignment to be processed
        await asyncio.sleep(0.05)

        # Step 2: Enqueue the mention (should be suppressed immediately)
        mention_item = _make_item(
            loop,
            event_id="evt-mention",
            future=_make_future(loop),
        )
        mention_results = await queue.enqueue_many([mention_item])

        assert len(mention_results) == 1
        assert mention_results[0]["status"] == "suppressed"
        assert mention_results[0]["reason"] == "assignment-recent"

        # The assignment should have completed normally
        assign_results = await assign_task
        assert len(assign_results) == 1
        assert assign_results[0]["status"] == "ok"


# ---------------------------------------------------------------------------
# Webhook-level integration tests
# ---------------------------------------------------------------------------

def _setup_mention_hold_patches(monkeypatch, hold_seconds=0.1):
    """Shared setup for mention-hold integration tests."""
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "top-secret")
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(settings, "mention_hold_seconds", hold_seconds)
    monkeypatch.setattr(webhooks, "_DEDUP", DummyDeduplicator())

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "group/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    async def fake_dispatch(event_uuid, tasks, context):
        return [
            {"agent": task.agent, "status": "ok", "event_id": event_uuid}
            for task in tasks
        ]

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch)


@pytest.mark.asyncio
async def test_mention_work_item_has_hold_metadata(monkeypatch):
    """Work items created from Note Hook mentions have correct hold metadata."""
    _setup_mention_hold_patches(monkeypatch, hold_seconds=0)

    # Use a real queue with hold disabled for synchronous testing
    queue = TriggerQueue(hold_seconds=0)
    monkeypatch.setattr(webhooks, "_TRIGGER_QUEUE", queue)

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if event_name == "Note Hook" and mentions and "claude" in mentions:
            rule = SimpleNamespace(name="mention-claude", mentions=["claude"], assignees=[], access="readonly")
            if rule_predicate and not rule_predicate(rule):
                return None
            agents = [AgentTask(agent="claude", task="review", prompt="review.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    monkeypatch.setattr(webhooks, "_ROUTES", type('R', (), {
        'resolve_match': staticmethod(resolver),
    })())

    # Capture the work items
    captured_items = []
    original_enqueue = queue.enqueue_many

    async def capturing_enqueue(items):
        captured_items.extend(items)
        return await original_enqueue(items)

    monkeypatch.setattr(queue, "enqueue_many", capturing_enqueue)

    payload = {
        "object_kind": "note",
        "object_attributes": {"action": "create", "note": "@claude please review"},
        "merge_request": {"iid": 10},
        "project": {"path_with_namespace": "group/project"},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-hold-meta",
            },
        )

    assert response.status_code == 200
    assert len(captured_items) == 1
    item = captured_items[0]
    assert item.is_mention_trigger is True
    assert item.is_assignment_trigger is False
    assert item.project_path == "group/project"
    assert item.iid == 10
    assert item.hold_agents == ["claude"]


@pytest.mark.asyncio
async def test_assignment_work_item_has_hold_metadata(monkeypatch):
    """Work items created from assignment events have correct hold metadata."""
    _setup_mention_hold_patches(monkeypatch, hold_seconds=0)

    queue = TriggerQueue(hold_seconds=0)
    monkeypatch.setattr(webhooks, "_TRIGGER_QUEUE", queue)

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            if rule_predicate and not rule_predicate(rule):
                return None
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    monkeypatch.setattr(webhooks, "_ROUTES", type('R', (), {
        'resolve_match': staticmethod(resolver),
    })())

    captured_items = []
    original_enqueue = queue.enqueue_many

    async def capturing_enqueue(items):
        captured_items.extend(items)
        return await original_enqueue(items)

    monkeypatch.setattr(queue, "enqueue_many", capturing_enqueue)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "project": {"path_with_namespace": "group/project"},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-assign-meta",
            },
        )

    assert response.status_code == 200
    assert len(captured_items) == 1
    item = captured_items[0]
    assert item.is_mention_trigger is False
    assert item.is_assignment_trigger is True
    assert item.project_path == "group/project"
    assert item.iid == 42
    assert item.hold_agents == ["claude"]


@pytest.mark.asyncio
async def test_mention_hold_end_to_end_suppression(monkeypatch):
    """End-to-end test: mention is held, then assignment arrives and suppresses it."""
    _setup_mention_hold_patches(monkeypatch, hold_seconds=10.0)

    queue = TriggerQueue(hold_seconds=10.0)
    monkeypatch.setattr(webhooks, "_TRIGGER_QUEUE", queue)

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if event_name == "Note Hook" and mentions and "claude" in mentions:
            rule = SimpleNamespace(name="mention-claude", mentions=["claude"], assignees=[], access="readonly")
            if rule_predicate and not rule_predicate(rule):
                return None
            agents = [AgentTask(agent="claude", task="review", prompt="review.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            if rule_predicate and not rule_predicate(rule):
                return None
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    monkeypatch.setattr(webhooks, "_ROUTES", type('R', (), {
        'resolve_match': staticmethod(resolver),
    })())

    # Step 1: Mention webhook arrives
    mention_payload = {
        "object_kind": "note",
        "object_attributes": {"action": "create", "note": "@claude I am assigning you again"},
        "merge_request": {"iid": 37},
        "project": {"path_with_namespace": "group/project"},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Send mention webhook (will be held)
        mention_response_task = asyncio.create_task(
            client.post(
                "/webhooks/gitlab",
                json=mention_payload,
                headers={
                    "X-Gitlab-Token": "top-secret",
                    "X-Gitlab-Event": "Note Hook",
                    "X-Gitlab-Event-UUID": "uuid-mention-e2e",
                },
            )
        )

        # Brief delay for the mention to be processed and held
        await asyncio.sleep(0.1)

        # Step 2: Assignment webhook arrives
        assign_payload = {
            "object_kind": "merge_request",
            "object_attributes": {"action": "update", "iid": 37},
            "project": {"path_with_namespace": "group/project"},
            "assignees": [{"username": "claude"}],
            "changes": {
                "assignees": {
                    "previous": [],
                    "current": [{"username": "claude"}],
                }
            },
        }

        assign_response = await client.post(
            "/webhooks/gitlab",
            json=assign_payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-assign-e2e",
            },
        )

        # The assignment should succeed
        assert assign_response.status_code == 200
        assign_data = assign_response.json()
        assert assign_data["status"] == "ok"

        # The mention should have been suppressed
        mention_response = await mention_response_task
        assert mention_response.status_code == 200
        mention_data = mention_response.json()
        # The suppressed mention will appear in the triggers
        suppressed = [t for t in mention_data.get("triggers", []) if t.get("status") == "suppressed"]
        assert len(suppressed) == 1
        assert suppressed[0]["reason"] == "assignment-coalesced"


@pytest.mark.asyncio
async def test_assignment_before_mention_e2e_suppression(monkeypatch):
    """End-to-end test: assignment arrives first, then mention is suppressed immediately (issue #75)."""
    _setup_mention_hold_patches(monkeypatch, hold_seconds=10.0)

    queue = TriggerQueue(hold_seconds=10.0)
    monkeypatch.setattr(webhooks, "_TRIGGER_QUEUE", queue)

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if event_name == "Note Hook" and mentions and "claude" in mentions:
            rule = SimpleNamespace(name="mention-claude", mentions=["claude"], assignees=[], access="readonly")
            if rule_predicate and not rule_predicate(rule):
                return None
            agents = [AgentTask(agent="claude", task="review", prompt="review.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            if rule_predicate and not rule_predicate(rule):
                return None
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    monkeypatch.setattr(webhooks, "_ROUTES", type('R', (), {
        'resolve_match': staticmethod(resolver),
    })())

    # Step 1: Assignment webhook arrives first
    assign_payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "update", "iid": 43},
        "project": {"path_with_namespace": "group/project"},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Send assignment webhook first
        assign_response_task = asyncio.create_task(
            client.post(
                "/webhooks/gitlab",
                json=assign_payload,
                headers={
                    "X-Gitlab-Token": "top-secret",
                    "X-Gitlab-Event": "Merge Request Hook",
                    "X-Gitlab-Event-UUID": "uuid-assign-first",
                },
            )
        )

        # Brief delay to ensure assignment is processed
        await asyncio.sleep(0.1)

        # Step 2: Mention webhook arrives shortly after
        mention_payload = {
            "object_kind": "note",
            "object_attributes": {"action": "create", "note": "@claude please look at this"},
            "merge_request": {"iid": 43},
            "project": {"path_with_namespace": "group/project"},
        }

        mention_response = await client.post(
            "/webhooks/gitlab",
            json=mention_payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-mention-after",
            },
        )

        # The mention should have been suppressed immediately
        assert mention_response.status_code == 200
        mention_data = mention_response.json()
        suppressed = [t for t in mention_data.get("triggers", []) if t.get("status") == "suppressed"]
        assert len(suppressed) == 1
        assert suppressed[0]["reason"] == "assignment-recent"

        # The assignment should succeed
        assign_response = await assign_response_task
        assert assign_response.status_code == 200
        assign_data = assign_response.json()
        assert assign_data["status"] == "ok"


@pytest.mark.asyncio
async def test_mention_hold_non_agent_mention_not_held(monkeypatch):
    """Mentions of non-agent users are not held (they bypass the hold buffer)."""
    _setup_mention_hold_patches(monkeypatch, hold_seconds=0)

    queue = TriggerQueue(hold_seconds=0)
    monkeypatch.setattr(webhooks, "_TRIGGER_QUEUE", queue)

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if event_name == "Note Hook" and mentions and "human-user" in mentions:
            rule = SimpleNamespace(name="mention-human", mentions=["human-user"], assignees=[], access="readonly")
            if rule_predicate and not rule_predicate(rule):
                return None
            agents = [AgentTask(agent="claude", task="review", prompt="review.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    monkeypatch.setattr(webhooks, "_ROUTES", type('R', (), {
        'resolve_match': staticmethod(resolver),
    })())

    captured_items = []
    original_enqueue = queue.enqueue_many

    async def capturing_enqueue(items):
        captured_items.extend(items)
        return await original_enqueue(items)

    monkeypatch.setattr(queue, "enqueue_many", capturing_enqueue)

    payload = {
        "object_kind": "note",
        "object_attributes": {"action": "create", "note": "@human-user please review"},
        "merge_request": {"iid": 10},
        "project": {"path_with_namespace": "group/project"},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-non-agent",
            },
        )

    assert response.status_code == 200
    assert len(captured_items) == 1
    item = captured_items[0]
    assert item.is_mention_trigger is True
    # hold_agents should be empty since human-user is not a known agent
    assert item.hold_agents == []
