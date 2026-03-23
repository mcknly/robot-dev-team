"""Robot Dev Team Project
File: app/services/deduplication.py
Description: Simple in-memory deduplication for webhook events.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict


class EventDeduplicator:
    """Tracks processed webhook event IDs to avoid duplicate handling."""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._items: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def mark(self, key: str) -> bool:
        """Mark the event ID and return False if it has already been seen."""

        now = time.monotonic()
        async with self._lock:
            self._purge(now)
            if key in self._items:
                return False
            self._items[key] = now
            return True

    def _purge(self, now: float) -> None:
        """Remove expired entries."""

        to_delete = [key for key, ts in self._items.items() if now - ts > self._ttl]
        for key in to_delete:
            del self._items[key]


def create_deduplicator() -> EventDeduplicator:
    """Factory to keep call sites concise."""

    return EventDeduplicator()
