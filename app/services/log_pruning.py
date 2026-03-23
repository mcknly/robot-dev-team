"""Robot Dev Team Project
File: app/services/log_pruning.py
Description: Service for pruning old log files.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)


class LogPruner:
    """Prunes old log files."""

    def __init__(
        self,
        log_dir: str = settings.run_logs_dir,
        retention_days: int = settings.log_retention_days,
        pruning_interval_hours: int = settings.log_pruning_interval_hours,
        enabled: bool = settings.log_pruning_enabled,
    ):
        self.log_dir = Path(log_dir)
        self.retention_delta = timedelta(days=retention_days)
        self.pruning_interval = timedelta(hours=pruning_interval_hours)
        self.enabled = enabled

    async def run_pruning_loop(self) -> None:
        """Runs the pruning process in a loop."""
        if not self.enabled:
            logger.info("Log pruning is disabled.")
            return

        logger.info(
            "Starting log pruning loop with a %s retention period and %s interval.",
            self.retention_delta,
            self.pruning_interval,
        )
        while True:
            try:
                self.prune_logs()
            except Exception:
                logger.exception("Error during log pruning.")
            await asyncio.sleep(self.pruning_interval.total_seconds())

    def prune_logs(self) -> None:
        """Prunes old log files from the log directory."""
        if not self._directory_exists():
            return

        now = datetime.now(timezone.utc)
        cutoff_time = now - self.retention_delta
        files_deleted = 0

        for path in self._iter_log_files():
            try:
                if self._should_remove(path, now, cutoff_time):
                    path.unlink()
                    files_deleted += 1
            except Exception:  # pragma: no cover - defensive guard
                logger.exception("Error processing file: %s", path)

        if files_deleted > 0:
            logger.info("Pruned %d old log files.", files_deleted)

    def _directory_exists(self) -> bool:
        try:
            return self.log_dir.exists()
        except Exception:  # pragma: no cover - defensive guard
            logger.exception("Error accessing log directory: %s", self.log_dir)
            return False

    def _iter_log_files(self):
        try:
            for path in self.log_dir.iterdir():
                if path.name == ".gitkeep":
                    continue
                yield path
        except Exception:  # pragma: no cover - defensive guard
            logger.exception("Error listing log directory: %s", self.log_dir)

    @staticmethod
    def _should_remove(path: Path, now: datetime, cutoff: datetime) -> bool:
        if not path.is_file():
            return False

        file_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if file_mtime >= cutoff:
            return False

        return now - file_mtime > timedelta(minutes=1)


log_pruner = LogPruner()
