"""Robot Dev Team Project
File: tests/test_log_pruning.py
Description: Pytest coverage for log pruning routines.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.log_pruning import LogPruner


@pytest.fixture
def log_pruner(tmp_path: Path) -> LogPruner:
    """Returns a LogPruner instance with a temporary log directory."""
    return LogPruner(log_dir=str(tmp_path), retention_days=7, pruning_interval_hours=24, enabled=True)


def test_prune_logs_deletes_old_files(log_pruner: LogPruner, tmp_path: Path) -> None:
    """Tests that prune_logs deletes old files."""
    now = datetime.now(timezone.utc)
    with patch("app.services.log_pruning.datetime", wraps=datetime) as mock_datetime:
        mock_datetime.now.return_value = now
        # Create some dummy log files
        new_file = tmp_path / "new_file.log"
        new_file.touch()
        old_file = tmp_path / "old_file.log"
        old_file.touch()
        eight_days_ago = now - timedelta(days=8)
        import os
        os.utime(old_file, (eight_days_ago.timestamp(), eight_days_ago.timestamp()))

        log_pruner.prune_logs()

    assert not old_file.exists()
    assert new_file.exists()


def test_prune_logs_does_not_delete_gitkeep(log_pruner: LogPruner, tmp_path: Path) -> None:
    """Tests that prune_logs does not delete the .gitkeep file."""
    (tmp_path / ".gitkeep").touch()
    log_pruner.prune_logs()
    assert (tmp_path / ".gitkeep").exists()


def test_prune_logs_does_not_delete_recent_files(log_pruner: LogPruner, tmp_path: Path) -> None:
    """Tests that prune_logs does not delete recent files."""
    (tmp_path / "recent_file.log").touch()
    log_pruner.prune_logs()
    assert (tmp_path / "recent_file.log").exists()


def test_prune_logs_handles_errors_gracefully(tmp_path: Path) -> None:
    """Tests that prune_logs handles errors gracefully."""
    error_file = tmp_path / "error_file.log"
    error_file.touch()

    with patch("pathlib.Path.stat", side_effect=Exception("Test error")), patch(
        "app.services.log_pruning.logger.exception"
    ) as mock_logger:
        log_pruner = LogPruner(log_dir=str(tmp_path), retention_days=7, pruning_interval_hours=24, enabled=True)
        log_pruner.prune_logs()
        mock_logger.assert_called_once()
