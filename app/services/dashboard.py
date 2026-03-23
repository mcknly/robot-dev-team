"""Robot Dev Team Project
File: app/services/dashboard.py
Description: Live dashboard management utilities.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from app.core.config import settings


@dataclass
class DashboardEvent:
    """Structured payload delivered to dashboard subscribers."""

    type: str
    data: Dict[str, Any]


class DashboardManager:
    """Coordinates real-time event fan-out to dashboard subscribers."""

    def __init__(self) -> None:
        self._enabled: bool = settings.live_dashboard_enabled
        self._subscribers: Set[asyncio.Queue[Dict[str, Any]]] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._active_agents: Dict[str, Dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self._enabled:
            return
        self._loop = loop

    async def subscribe(self) -> asyncio.Queue[Dict[str, Any]]:
        if not self._enabled:
            raise RuntimeError("Live dashboard not enabled")

        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._subscribers.add(queue)
        if self._active_agents:
            queue.put_nowait(
                {
                    "type": "agent_status",
                    "timestamp": self._timestamp(),
                    "active_agents": list(self._active_agents.values()),
                }
            )
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def agent_started(self, event_id: str, agent: str, task: str) -> str:
        if not self._enabled:
            return ""
        key = f"{event_id}:{agent}:{task}"
        self._active_agents[key] = {
            "event_id": event_id,
            "agent": agent,
            "task": task,
        }
        self._publish(
            {
                "type": "agent_status",
                "timestamp": self._timestamp(),
                "active_agents": list(self._active_agents.values()),
            }
        )
        return key

    def agent_finished(self, key: str) -> None:
        if not self._enabled or not key:
            return
        self._active_agents.pop(key, None)
        self._publish(
            {
                "type": "agent_status",
                "timestamp": self._timestamp(),
                "active_agents": list(self._active_agents.values()),
            }
        )

    def publish_prompt(self, event_id: str, agent: str, task: str, line: str) -> None:
        self._publish_stream("prompt", line, event_id, agent, task)

    def publish_stdout(self, event_id: str, agent: str, task: str, line: str) -> None:
        self._publish_stream("stdout", line, event_id, agent, task)

    def publish_stderr(self, event_id: str, agent: str, task: str, line: str) -> None:
        self._publish_stream("stderr", line, event_id, agent, task)

    def publish_system(self, message: str, level: str, logger_name: str) -> None:
        if not self._enabled:
            return
        payload = {
            "type": "stream",
            "stream": "system",
            "timestamp": self._timestamp(),
            "level": level,
            "logger": logger_name,
            "line": message,
        }
        self._publish(payload)

    def _publish_stream(
        self,
        stream: str,
        line: str,
        event_id: str,
        agent: str,
        task: str,
    ) -> None:
        if not self._enabled:
            return
        payload = {
            "type": "stream",
            "stream": stream,
            "event_id": event_id,
            "agent": agent,
            "task": task,
            "line": line,
        }
        self._publish(payload)

    def _publish(self, message: Dict[str, Any]) -> None:
        if not self._enabled:
            return
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._loop = loop
        if not loop.is_running():
            return
        loop.call_soon_threadsafe(self._fan_out, message)

    def _fan_out(self, message: Dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:  # pragma: no cover - defensive
                pass

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


dashboard_manager = DashboardManager()
