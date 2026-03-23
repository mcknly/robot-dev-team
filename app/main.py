"""Robot Dev Team Project
File: app/main.py
Description: FastAPI application entrypoint.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI

from app.api.webhooks import router as webhooks_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.services.dashboard import dashboard_manager
from app.services.branch_pruning import branch_pruner
from app.services.log_pruning import log_pruner

setup_logging()


def ensure_directories() -> None:
    Path(settings.run_logs_dir).mkdir(parents=True, exist_ok=True)


def configure_dashboard() -> None:
    if settings.live_dashboard_enabled:
        dashboard_manager.set_loop(asyncio.get_running_loop())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup
    ensure_directories()
    configure_dashboard()
    pruning_task = None
    if log_pruner.enabled:
        pruning_task = asyncio.create_task(log_pruner.run_pruning_loop())
    app.state.log_pruning_task = pruning_task

    branch_pruning_task = None
    if branch_pruner.enabled:
        branch_pruning_task = asyncio.create_task(branch_pruner.run_pruning_loop())
    app.state.branch_pruning_task = branch_pruning_task

    yield

    # shutdown
    if pruning_task is not None:
        pruning_task.cancel()
        with suppress(asyncio.CancelledError):
            await pruning_task

    if branch_pruning_task is not None:
        branch_pruning_task.cancel()
        with suppress(asyncio.CancelledError):
            await branch_pruning_task


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(webhooks_router)

if settings.live_dashboard_enabled:
    from app.api.dashboard import router as dashboard_router

    app.include_router(dashboard_router)


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
