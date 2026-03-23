"""Robot Dev Team Project
File: app/services/trigger_queue.py
Description: In-memory trigger queue to serialize agent dispatch handling.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app.core.logging import get_logger
from app.services.agents import AgentKilledError

LOGGER = get_logger(__name__)

# Key type for mention hold tracking: (project_path, iid, agent)
MentionHoldKey = Tuple[str, int, str]


@dataclass
class TriggerWorkItem:
    """Unit of work representing an agent dispatch for a route trigger."""

    event_id: str
    base_event_uuid: str
    event_name: str
    action: Optional[str]
    author: Optional[str]
    labels: List[str]
    mentions: List[str]
    route_name: str
    handler: Callable[[], Awaitable[List[Dict[str, Any]]]]
    future: asyncio.Future[Dict[str, Any]]
    # Mention-hold metadata
    project_path: Optional[str] = None
    iid: Optional[int] = None
    is_mention_trigger: bool = False
    is_assignment_trigger: bool = False
    hold_agents: List[str] = field(default_factory=list)
    # Internal flag: tracks whether this item has already been promoted to the
    # dispatch queue, preventing duplicate enqueue when multiple hold keys
    # (multi-agent mentions) expire independently.
    _promoted: bool = field(default=False, repr=False)


class MentionHoldBuffer:
    """Holds mention-triggered work items briefly before promotion to the queue.

    When a mention trigger is submitted, it is placed in a hold buffer with a
    configurable delay.  If an assignment trigger for the same
    (project, iid, agent) key arrives during the hold window, the mention
    item is suppressed.  If the timer expires, the mention item is promoted
    to the provided dispatch callback.
    """

    def __init__(self, hold_seconds: float = 3.0) -> None:
        self.hold_seconds = hold_seconds
        # Pending mention items keyed by (project, iid, agent_lower)
        self._pending: Dict[MentionHoldKey, _HeldMention] = {}
        # Recent assignment keys with auto-expiring timers (issue #75)
        self._recent_assignments: Dict[MentionHoldKey, asyncio.TimerHandle] = {}

    def hold(
        self,
        key: MentionHoldKey,
        item: TriggerWorkItem,
        promote_cb: Callable[[TriggerWorkItem], None],
    ) -> None:
        """Place a mention item in the hold buffer.

        If a recent assignment exists for this key, the mention is suppressed
        immediately (issue #75).  If a matching key is already held, the new
        item replaces it (the old timer is cancelled).
        """
        if self.has_recent_assignment(key):
            if not item.future.done():
                item.future.set_result({
                    "status": "suppressed",
                    "reason": "assignment-recent",
                    "event_id": item.event_id,
                    "base_event_uuid": item.base_event_uuid,
                    "event": item.event_name,
                    "action": item.action,
                    "mentions": item.mentions,
                    "route": item.route_name,
                    "agents": [],
                })
            LOGGER.info(
                "Mention suppressed by recent assignment: key=%s event_id=%s",
                key,
                item.event_id,
                extra={"key": str(key), "event_id": item.event_id},
            )
            return

        existing = self._pending.pop(key, None)
        if existing is not None:
            existing.timer.cancel()
            # Resolve the replaced item's future to prevent hanging requests
            if not existing.item.future.done():
                existing.item.future.set_result({
                    "status": "suppressed",
                    "reason": "mention-replaced",
                    "event_id": existing.item.event_id,
                    "base_event_uuid": existing.item.base_event_uuid,
                    "event": existing.item.event_name,
                    "action": existing.item.action,
                    "mentions": existing.item.mentions,
                    "route": existing.item.route_name,
                    "agents": [],
                })
                LOGGER.info(
                    "Replaced held mention future resolved: key=%s event_id=%s",
                    key,
                    existing.item.event_id,
                    extra={"key": str(key), "event_id": existing.item.event_id},
                )

        timer = asyncio.get_running_loop().call_later(
            self.hold_seconds,
            self._on_expire,
            key,
            item,
            promote_cb,
        )
        self._pending[key] = _HeldMention(item=item, timer=timer)
        LOGGER.info(
            "Mention held: key=%s event_id=%s hold_seconds=%s",
            key,
            item.event_id,
            self.hold_seconds,
            extra={"key": str(key), "event_id": item.event_id},
        )

    def cancel(self, key: MentionHoldKey) -> bool:
        """Cancel a held mention for the given key.  Returns True if suppressed."""
        held = self._pending.pop(key, None)
        if held is None:
            return False
        held.timer.cancel()
        # Resolve the future so the HTTP response can return
        if not held.item.future.done():
            held.item.future.set_result({
                "status": "suppressed",
                "reason": "assignment-coalesced",
                "event_id": held.item.event_id,
                "base_event_uuid": held.item.base_event_uuid,
                "event": held.item.event_name,
                "action": held.item.action,
                "mentions": held.item.mentions,
                "route": held.item.route_name,
                "agents": [],
            })
        LOGGER.info(
            "Mention suppressed by assignment: key=%s event_id=%s",
            key,
            held.item.event_id,
            extra={"key": str(key), "event_id": held.item.event_id},
        )
        return True

    def cancel_for_item(self, item: TriggerWorkItem) -> List[MentionHoldKey]:
        """Cancel all held mentions that overlap with an assignment item's agents.

        Also records recent assignment keys so mentions arriving after the
        assignment (within the hold window) are suppressed immediately
        (issue #75).

        Returns the list of keys that were suppressed.
        """
        if not item.project_path or item.iid is None:
            return []
        suppressed: List[MentionHoldKey] = []
        for agent in item.hold_agents:
            key = (item.project_path, item.iid, agent.lower())
            if self.cancel(key):
                suppressed.append(key)
            self.record_assignment(key)
        return suppressed

    def record_assignment(self, key: MentionHoldKey) -> None:
        """Record that an assignment was recently enqueued for this key.

        The record auto-expires after ``hold_seconds`` so that only mentions
        arriving within the suppression window are affected.
        """
        existing = self._recent_assignments.pop(key, None)
        if existing is not None:
            existing.cancel()
        timer = asyncio.get_running_loop().call_later(
            self.hold_seconds,
            self._recent_assignments.pop,
            key,
            None,
        )
        self._recent_assignments[key] = timer

    def has_recent_assignment(self, key: MentionHoldKey) -> bool:
        """Check if an assignment was recently enqueued for this key."""
        return key in self._recent_assignments

    def _on_expire(
        self,
        key: MentionHoldKey,
        item: TriggerWorkItem,
        promote_cb: Callable[[TriggerWorkItem], None],
    ) -> None:
        """Timer callback: promote the held mention to the queue."""
        if key in self._pending:
            del self._pending[key]
        LOGGER.info(
            "Mention hold expired, promoting: key=%s event_id=%s",
            key,
            item.event_id,
            extra={"key": str(key), "event_id": item.event_id},
        )
        promote_cb(item)

    def has_pending(self, key: MentionHoldKey) -> bool:
        """Check if a mention is currently held for the given key."""
        return key in self._pending


@dataclass
class _HeldMention:
    """Internal record for a held mention item."""

    item: TriggerWorkItem
    timer: asyncio.TimerHandle


class TriggerQueue:
    """FIFO queue with a background worker processing trigger items.

    Includes a MentionHoldBuffer that delays mention-triggered items briefly
    to allow assignment triggers to suppress them (see issue #69).
    """

    def __init__(self, hold_seconds: float = 3.0) -> None:
        self._queue: asyncio.Queue[TriggerWorkItem] = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._hold_buffer = MentionHoldBuffer(hold_seconds=hold_seconds)

    @property
    def hold_buffer(self) -> MentionHoldBuffer:
        return self._hold_buffer

    async def enqueue_many(self, items: List[TriggerWorkItem]) -> List[Dict[str, Any]]:
        """Enqueue multiple trigger items and wait for their completion in order.

        Mention-triggered items are routed through the hold buffer (if hold
        is enabled and they have the required metadata).  Assignment-triggered
        items cancel any matching held mentions before proceeding.
        """
        if not items:
            return []
        self._ensure_worker()

        for item in items:
            # Assignment items: cancel any held mentions first
            if item.is_assignment_trigger:
                suppressed = self._hold_buffer.cancel_for_item(item)
                if suppressed:
                    LOGGER.info(
                        "Assignment coalesced %d mention(s): %s",
                        len(suppressed),
                        suppressed,
                        extra={"suppressed_keys": [str(k) for k in suppressed]},
                    )

            # Mention items: route through hold buffer if possible
            if (
                item.is_mention_trigger
                and self._hold_buffer.hold_seconds > 0
                and item.project_path
                and item.iid is not None
                and item.hold_agents
            ):
                for agent in item.hold_agents:
                    key = (item.project_path, item.iid, agent.lower())
                    self._hold_buffer.hold(key, item, self._promote)
                continue

            # Direct enqueue for non-mention items or when hold is disabled
            self._enqueue_item(item)

        results: List[Dict[str, Any]] = await asyncio.gather(*(item.future for item in items))
        return results

    def _promote(self, item: TriggerWorkItem) -> None:
        """Callback from hold buffer: enqueue an item that survived the hold window."""
        if item.future.done() or item._promoted:
            return
        item._promoted = True
        self._ensure_worker()
        self._enqueue_item(item)

    def _enqueue_item(self, item: TriggerWorkItem) -> None:
        """Place an item on the FIFO queue."""
        self._queue.put_nowait(item)
        queue_size = self._queue.qsize()
        LOGGER.info(
            "Item enqueued: event_id=%s route=%s queue_size=%s",
            item.event_id,
            item.route_name,
            queue_size,
            extra={
                "event_id": item.event_id,
                "route": item.route_name,
                "queue_size": queue_size,
            },
        )

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                agents = await item.handler()
                result = {
                    "status": "ok",
                    "event_id": item.event_id,
                    "base_event_uuid": item.base_event_uuid,
                    "event": item.event_name,
                    "action": item.action,
                    "mentions": item.mentions,
                    "route": item.route_name,
                    "agents": agents,
                }
                if not item.future.done():
                    item.future.set_result(result)
            except AgentKilledError as exc:
                result = {
                    "status": "killed",
                    "event_id": item.event_id,
                    "base_event_uuid": item.base_event_uuid,
                    "event": item.event_name,
                    "action": item.action,
                    "mentions": item.mentions,
                    "route": item.route_name,
                    "error": str(exc),
                    "agents": [],
                }
                if not item.future.done():
                    item.future.set_result(result)
            except Exception as exc:  # pragma: no cover - defensive guard
                if not item.future.done():
                    item.future.set_exception(exc)
            finally:
                self._queue.task_done()
                remaining = self._queue.qsize()
                LOGGER.info(
                    "Queue item processed: event_id=%s route=%s remaining=%s",
                    item.event_id,
                    item.route_name,
                    remaining,
                    extra={
                        "event_id": item.event_id,
                        "route": item.route_name,
                        "remaining": remaining,
                    },
                )


__all__ = ["MentionHoldBuffer", "TriggerQueue", "TriggerWorkItem"]
