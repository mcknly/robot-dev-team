"""Robot Dev Team Project
File: app/services/project_paths.py
Description: Resolve GitLab project paths to local working directories with auto-clone support.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from app.core.config import settings
from app.core.logging import get_logger

LOGGER = get_logger(__name__)

# Lock dictionary for preventing concurrent clone operations on the same project
_clone_locks: dict[str, asyncio.Lock] = {}
_clone_locks_mutex = asyncio.Lock()


def _validate_project_path(project_path: str) -> bool:
    """Validate project path to prevent directory traversal attacks."""
    if not project_path:
        return False
    if ".." in project_path:
        return False
    if project_path.startswith("/"):
        return False
    if not re.match(r"^[\w\-\.]+(/[\w\-\.]+)*$", project_path):
        return False
    return True


async def _get_clone_lock(project_path: str) -> asyncio.Lock:
    """Get or create a lock for the given project path."""
    async with _clone_locks_mutex:
        if project_path not in _clone_locks:
            _clone_locks[project_path] = asyncio.Lock()
        return _clone_locks[project_path]


def _validate_clone_url(clone_url: str) -> bool:
    """Validate that clone URL host matches the configured GitLab host."""
    try:
        parsed = urlparse(clone_url)
        expected_host = settings.glab_host
        if parsed.hostname != expected_host:
            LOGGER.error(
                "Clone URL host does not match configured GitLab host",
                extra={
                    "clone_url_host": parsed.hostname,
                    "expected_host": expected_host,
                },
            )
            return False
        return True
    except Exception as exc:
        LOGGER.error(
            "Failed to parse clone URL",
            extra={"clone_url": clone_url, "error": str(exc)},
        )
        return False


class ProjectPathResolver:
    """Resolves GitLab project paths to local directories with auto-clone support."""

    def __init__(self, projects_root: str = "projects") -> None:
        self._projects_root = Path(projects_root).resolve()
        self._projects_root_ro = Path(f"{projects_root}-ro").resolve()

    def resolve(self, project_path: Optional[str], access: str = "readonly") -> Optional[str]:
        """Resolve project path to container working directory.

        Args:
            project_path: GitLab project path_with_namespace (e.g., 'group/project')
            access: Access mode - 'readonly' or 'readwrite'. Determines which mount
                    point is used (/work/projects-ro vs /work/projects).

        Returns:
            Container path to the project directory, or None if not found/invalid.
        """
        if not project_path or not _validate_project_path(project_path):
            return None

        base = self._projects_root_ro if access == "readonly" else self._projects_root
        target = base / project_path

        if target.exists():
            # Verify this is a valid git repository
            if not (target / ".git").exists():
                LOGGER.warning(
                    "Directory exists but is not a git repository",
                    extra={"path": str(target)},
                )
                return None
            return str(target)

        return None

    async def ensure_project_exists(
        self,
        project_path: Optional[str],
        access: str = "readonly",
        clone_url: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> Optional[str]:
        """Ensure project directory exists, optionally cloning if missing.

        Args:
            project_path: GitLab project path_with_namespace (e.g., 'group/project')
            access: Access mode - 'readonly' or 'readwrite'.
            clone_url: Git clone URL (HTTPS preferred). Required for auto-clone.
            agent: Agent name for credential setup (e.g., 'claude', 'gemini').

        Returns:
            Container path to the project directory, or None if not found/clone failed.

        Raises:
            RuntimeError: If clone fails and auto-clone is enabled.
        """
        if not project_path or not _validate_project_path(project_path):
            LOGGER.warning(
                "Invalid project path",
                extra={"project_path": project_path},
            )
            return None

        # Check if project already exists
        existing = self.resolve(project_path, access=access)
        if existing:
            return existing

        # If auto-clone is disabled or no clone URL, return None
        if not settings.enable_auto_clone:
            LOGGER.info(
                "Project not found and auto-clone disabled",
                extra={"project_path": project_path},
            )
            return None

        if not clone_url:
            LOGGER.warning(
                "Project not found and no clone URL provided",
                extra={"project_path": project_path},
            )
            return None

        # Validate clone URL host matches configured GitLab host
        if not _validate_clone_url(clone_url):
            LOGGER.error(
                "Clone URL validation failed",
                extra={"project_path": project_path, "clone_url": clone_url},
            )
            return None

        # Attempt to clone the repository
        clone_lock = await _get_clone_lock(project_path)
        async with clone_lock:
            # Re-check after acquiring lock (another task may have cloned)
            existing = self.resolve(project_path, access=access)
            if existing:
                return existing

            # Clone to the read-write location
            clone_target = self._projects_root / project_path
            success = await self._clone_repository(
                clone_url=clone_url,
                target_dir=clone_target,
                agent=agent,
            )

            if not success:
                raise RuntimeError(f"Failed to clone repository: {project_path}")

            # Return the appropriate path based on access mode
            return self.resolve(project_path, access=access)

    async def _clone_repository(
        self,
        clone_url: str,
        target_dir: Path,
        agent: Optional[str] = None,
    ) -> bool:
        """Clone a repository to the target directory.

        Args:
            clone_url: Git clone URL.
            target_dir: Target directory for the clone.
            agent: Agent name for credential setup.

        Returns:
            True if clone succeeded, False otherwise.
        """
        LOGGER.info(
            "Cloning repository",
            extra={
                "clone_url": clone_url,
                "target_dir": str(target_dir),
                "agent": agent,
            },
        )

        # Create parent directories
        target_dir.parent.mkdir(parents=True, exist_ok=True)

        # Check if target already exists (not a git repo)
        if target_dir.exists():
            if not (target_dir / ".git").exists():
                LOGGER.error(
                    "Target directory exists but is not a git repository",
                    extra={"target_dir": str(target_dir)},
                )
                return False
            LOGGER.info(
                "Target directory already exists as git repository",
                extra={"target_dir": str(target_dir)},
            )
            return True

        # Set up credentials via glab-usr if agent is provided
        if agent:
            auth_success = await self._authenticate_for_clone(agent)
            if not auth_success:
                LOGGER.error(
                    "Failed to set up git credentials for clone",
                    extra={"agent": agent},
                )
                return False

        # Build clone command
        clone_args = ["git", "clone"]
        if settings.auto_clone_depth > 0:
            clone_args.extend(["--depth", str(settings.auto_clone_depth)])
        clone_args.extend([clone_url, str(target_dir)])

        try:
            proc = await asyncio.create_subprocess_exec(
                *clone_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                stderr_text = stderr.decode("utf-8", errors="replace")
                LOGGER.error(
                    "Git clone failed",
                    extra={
                        "returncode": proc.returncode,
                        "stderr": stderr_text,
                        "clone_url": clone_url,
                    },
                )
                # Cleanup partial clone
                self._cleanup_partial_clone(target_dir)
                return False

            LOGGER.info(
                "Repository cloned successfully",
                extra={"target_dir": str(target_dir)},
            )
            return True

        except Exception as exc:
            LOGGER.error(
                "Exception during git clone",
                extra={"error": str(exc), "clone_url": clone_url},
            )
            # Cleanup partial clone
            self._cleanup_partial_clone(target_dir)
            return False

    def _cleanup_partial_clone(self, target_dir: Path) -> None:
        """Remove a partially cloned directory to avoid leaving stale state."""
        if target_dir.exists():
            LOGGER.info(
                "Cleaning up partial clone",
                extra={"target_dir": str(target_dir)},
            )
            try:
                shutil.rmtree(target_dir)
            except Exception as exc:
                LOGGER.error(
                    "Failed to cleanup partial clone",
                    extra={"target_dir": str(target_dir), "error": str(exc)},
                )

    async def _authenticate_for_clone(self, agent: str) -> bool:
        """Set up git credentials for the given agent using glab-usr."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "glab-usr",
                agent,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                stderr_text = stderr.decode("utf-8", errors="replace")
                LOGGER.error(
                    "glab-usr authentication failed",
                    extra={"agent": agent, "stderr": stderr_text},
                )
                return False

            return True

        except FileNotFoundError:
            LOGGER.error("glab-usr command not found")
            return False
        except Exception as exc:
            LOGGER.error(
                "Exception during glab-usr authentication",
                extra={"error": str(exc), "agent": agent},
            )
            return False

    def check_projects_root_writable(self) -> bool:
        """Check if the projects root directory is writable."""
        try:
            if not self._projects_root.exists():
                LOGGER.warning(
                    "Projects root directory does not exist",
                    extra={"path": str(self._projects_root)},
                )
                return False
            return os.access(self._projects_root, os.W_OK)
        except Exception as exc:
            LOGGER.error(
                "Error checking projects root writability",
                extra={"error": str(exc)},
            )
            return False


PROJECT_PATHS = ProjectPathResolver()


__all__ = ["ProjectPathResolver", "PROJECT_PATHS"]
