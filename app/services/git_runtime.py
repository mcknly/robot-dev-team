"""Robot Dev Team Project
File: app/services/git_runtime.py
Description: Shared runtime coordination for git/glab authentication.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio

# Application-global lock that serializes auth-sensitive git operations.
# Both the agent dispatch path (glab-usr + branch resolution + fetch/push)
# and the background BranchPruner (glab-usr + fetch + merged-branch query +
# delete) must acquire this lock before mutating shared glab/git credential
# state.  Since webhook-triggered agent work is already serialized through
# TriggerQueue, this lock primarily prevents the background pruner from
# overlapping with agent git operations.
git_auth_lock = asyncio.Lock()

# Default timeout (seconds) for glab-usr authentication subprocesses.
# All callers that run glab-usr under git_auth_lock should use this value
# to ensure the lock is always released in bounded time.
GLAB_USR_TIMEOUT_SECONDS: float = 30.0
