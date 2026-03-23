"""Robot Dev Team Project
File: app/core/logging.py
Description: Logging configuration helpers.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import logging
from logging.config import dictConfig

from app.core.config import settings


class DashboardLogHandler(logging.Handler):
    """Mirror log records to the live dashboard when enabled."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - thin wrapper
        from app.services.dashboard import dashboard_manager

        dashboard_manager.publish_system(record.getMessage(), record.levelname, record.name)


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
        }
    },
    "root": {
        "level": "INFO",
        "handlers": ["console"],
    },
}


def setup_logging() -> None:
    """Apply the logging configuration once at startup."""

    LOGGING_CONFIG["root"]["level"] = settings.app_log_level
    dictConfig(LOGGING_CONFIG)

    if settings.live_dashboard_enabled:
        handler = DashboardLogHandler()
        logging.getLogger().addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a module-specific logger."""

    return logging.getLogger(name)
