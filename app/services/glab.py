"""Robot Dev Team Project
File: app/services/glab.py
Description: Helpers around the GitLab CLI (`glab`).
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logging import get_logger

LOGGER = get_logger(__name__)


def _get_glab_env() -> Dict[str, str]:
    """Build environment variables for glab subprocess with authentication."""
    env = os.environ.copy()
    if settings.glab_token:
        env["GITLAB_TOKEN"] = settings.glab_token
    if settings.glab_host:
        env["GITLAB_HOST"] = settings.glab_host
    return env


async def run_glab_json(args: List[str], timeout: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Execute a `glab` command and parse JSON output."""

    cmd = ["glab", *args]
    timeout = timeout or settings.glab_timeout_seconds
    env = _get_glab_env()
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        LOGGER.warning("glab command not available; skipping enrichment", extra={"cmd": cmd})
        return None

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        LOGGER.error("glab command timed out", extra={"cmd": cmd})
        return None

    if process.returncode != 0:
        LOGGER.error(
            "glab command failed",
            extra={"cmd": cmd, "code": process.returncode, "stderr": stderr.decode().strip()},
        )
        return None

    payload = stdout.decode()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        LOGGER.debug(
            "glab output was not valid JSON; skipping enrichment",
            extra={"cmd": cmd, "output": payload},
        )
        return None

    if isinstance(parsed, dict):
        return parsed

    LOGGER.debug(
        "glab output was not a mapping; skipping enrichment",
        extra={"cmd": cmd, "output": payload},
    )
    return None


async def run_glab(args: List[str], timeout: Optional[int] = None) -> bool:
    """Execute a glab command and return True if successful."""
    cmd = ["glab", *args]
    timeout = timeout or settings.glab_timeout_seconds
    env = _get_glab_env()

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        LOGGER.warning("glab command not available", extra={"cmd": cmd})
        return False

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        LOGGER.error("glab command timed out", extra={"cmd": cmd})
        return False

    if process.returncode != 0:
        LOGGER.error(
            "glab command failed",
            extra={"cmd": cmd, "code": process.returncode, "stderr": stderr.decode().strip()},
        )
        return False
    return True


async def unassign_agent(project_path: str, iid: int, resource_type: str, agent: str) -> bool:
    """Unassign an agent from an issue or merge request.

    Args:
        project_path: GitLab project path (e.g., "namespace/project")
        iid: Issue or merge request IID
        resource_type: Either "issue" or "merge_request"
        agent: Username of the agent to unassign

    Returns:
        True if unassignment succeeded, False otherwise
    """
    cmd_type = "mr" if resource_type in ("merge_request", "mr") else "issue"

    LOGGER.info(
        "Unassigning agent",
        extra={"agent": agent, "type": cmd_type, "iid": iid, "project": project_path},
    )
    return await run_glab([cmd_type, "update", str(iid), "--repo", project_path, "--assignee", f"-{agent}"])


def _agent_token_env_var(agent: str) -> str:
    """Derive the GitLab token env var name for any agent using a naming convention.

    Convention: ``<AGENT_UPPER>_AGENT_GITLAB_TOKEN``
    (e.g. ``claude`` -> ``CLAUDE_AGENT_GITLAB_TOKEN``,
    ``qwen-code`` -> ``QWEN_CODE_AGENT_GITLAB_TOKEN``).
    """
    return agent.upper().replace("-", "_") + "_AGENT_GITLAB_TOKEN"


def resolve_agent_token(agent: str) -> Optional[str]:
    """Resolve the GitLab PAT for an agent using a naming convention.

    Derives the expected environment variable from the agent name
    (``<AGENT_UPPER>_AGENT_GITLAB_TOKEN``).  Returns ``None`` when
    the variable is not set, allowing callers to fall back gracefully.
    Raises ``ValueError`` when the variable exists but is empty or
    whitespace -- enforcing the policy that a *configured* agent
    token must actually contain a value.
    """
    env_var = _agent_token_env_var(agent.lower())
    raw = os.environ.get(env_var)
    if raw is None:
        return None
    if not raw.strip():
        raise ValueError(
            f"Missing GitLab token for agent '{agent}': "
            f"environment variable {env_var} is not set or empty"
        )
    return raw


def _get_agent_glab_env(agent: str) -> Dict[str, str]:
    """Build glab environment using the agent's own GitLab PAT.

    Falls back to the app-level ``GLAB_TOKEN`` when the agent-specific
    token cannot be resolved.  This fallback is intentionally preserved
    for best-effort paths like termination comments.
    """
    env = os.environ.copy()

    token: Optional[str] = None
    env_var = _agent_token_env_var(agent.lower())
    token = os.environ.get(env_var)

    if not token:
        token = settings.glab_token

    if token:
        env["GITLAB_TOKEN"] = token
    if settings.glab_host:
        env["GITLAB_HOST"] = settings.glab_host
    return env


async def notify_agent_termination(
    project_path: str,
    iid: int,
    resource_type: str,
    agent_name: str,
    reason: str,
    details: str = "",
) -> bool:
    """Post a termination comment on a GitLab issue or merge request.

    Uses the killed agent's own PAT so the comment appears under the
    agent's identity.  Designed to be reusable across manual kills and
    timeouts (see issue #62).

    Args:
        project_path: GitLab project path (e.g. ``namespace/project``)
        iid: Issue or merge request IID
        resource_type: ``"issue"`` or ``"merge_request"``
        agent_name: Username of the terminated agent
        reason: Short label (e.g. ``"Manual Kill"`` or ``"Timeout"``)
        details: Optional extra context for the comment body

    Returns:
        True on success, False otherwise.
    """
    cmd_type = "mr" if resource_type in ("merge_request", "mr") else "issue"

    body = f"**Agent terminated** -- `@{agent_name}` was stopped. Reason: **{reason}**."
    if details:
        body += f"\n\n{details}"

    cmd = [
        cmd_type, "comment", str(iid),
        "--repo", project_path,
        "--message", body,
    ]

    env = _get_agent_glab_env(agent_name)

    LOGGER.info(
        "Posting termination comment",
        extra={
            "agent": agent_name,
            "type": cmd_type,
            "iid": iid,
            "project": project_path,
            "reason": reason,
        },
    )

    full_cmd = ["glab", *cmd]
    timeout = settings.glab_timeout_seconds
    try:
        process = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        LOGGER.warning("glab command not available for termination comment", extra={"cmd": full_cmd})
        return False

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        LOGGER.error("glab termination comment timed out", extra={"cmd": full_cmd})
        return False

    if process.returncode != 0:
        LOGGER.error(
            "glab termination comment failed",
            extra={"cmd": full_cmd, "code": process.returncode, "stderr": stderr.decode().strip()},
        )
        return False

    return True


async def notify_backup_created(
    project_path: str,
    iid: int,
    resource_type: str,
    agent_name: str,
    backup_branch: str,
    backup_reason: str = "",
) -> bool:
    """Post a comment when an auto-backup branch is created.

    Uses the agent's own PAT so the comment appears under the agent's
    identity.  Best-effort: failures are logged but never block dispatch.

    Args:
        project_path: GitLab project path (e.g. ``namespace/project``)
        iid: Issue or merge request IID
        resource_type: ``"issue"`` or ``"merge_request"``
        agent_name: Username of the agent that triggered the backup
        backup_branch: Name of the created backup branch
        backup_reason: Why the backup was created
            (``"uncommitted_changes"`` or ``"local_commits"``)

    Returns:
        True on success, False otherwise.
    """
    cmd_type = "mr" if resource_type in ("merge_request", "mr") else "issue"

    if backup_reason == "uncommitted_changes":
        reason_text = "Uncommitted changes were detected in the working tree."
    elif backup_reason == "local_commits":
        reason_text = "Local commits ahead of origin were detected."
    else:
        reason_text = "Changes were detected that needed to be preserved."

    body = (
        f"**Auto-backup created** -- `@{agent_name}` preserved changes "
        f"before switching branches.\n\n"
        f"**Reason:** {reason_text}\n"
        f"**Backup branch:** `{backup_branch}`\n\n"
        f"To recover:\n"
        f"```\n"
        f"git fetch origin {backup_branch}\n"
        f"git checkout {backup_branch}\n"
        f"```"
    )

    cmd = [
        cmd_type, "comment", str(iid),
        "--repo", project_path,
        "--message", body,
    ]

    env = _get_agent_glab_env(agent_name)

    LOGGER.info(
        "Posting backup notification comment",
        extra={
            "agent": agent_name,
            "type": cmd_type,
            "iid": iid,
            "project": project_path,
            "backup_branch": backup_branch,
            "backup_reason": backup_reason,
        },
    )

    full_cmd = ["glab", *cmd]
    timeout = settings.glab_timeout_seconds
    try:
        process = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        LOGGER.warning("glab command not available for backup comment", extra={"cmd": full_cmd})
        return False

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        LOGGER.error("glab backup comment timed out", extra={"cmd": full_cmd})
        return False

    if process.returncode != 0:
        LOGGER.error(
            "glab backup comment failed",
            extra={"cmd": full_cmd, "code": process.returncode, "stderr": stderr.decode().strip()},
        )
        return False

    return True


async def fetch_merge_request(project_path: str, iid: int) -> Optional[Dict[str, Any]]:
    return await run_glab_json(["mr", "view", str(iid), "--repo", project_path, "-F", "json"])


async def fetch_issue(project_path: str, iid: int) -> Optional[Dict[str, Any]]:
    return await run_glab_json(["issue", "view", str(iid), "--repo", project_path, "-F", "json"])
