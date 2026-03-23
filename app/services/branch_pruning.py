"""Robot Dev Team Project
File: app/services/branch_pruning.py
Description: Service for pruning remote branches that have been merged to the base branch.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import timedelta
from fnmatch import fnmatch
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger
from app.services.git_runtime import GLAB_USR_TIMEOUT_SECONDS, git_auth_lock

logger = get_logger(__name__)

PROJECTS_ROOT = Path("/work/projects")

# Refs that must never be pruned regardless of configuration
_BUILTIN_PROTECTED = {"main", "master", "HEAD"}


class BranchPruner:
    """Prunes remote branches that have been fully merged into the base branch."""

    def __init__(
        self,
        enabled: bool = settings.branch_pruning_enabled,
        interval_hours: int = settings.branch_pruning_interval_hours,
        dry_run: bool = settings.branch_pruning_dry_run,
        base_branch: str = settings.branch_pruning_base_branch,
        protected_patterns: str = settings.branch_pruning_protected_patterns,
        agent: str = settings.branch_pruning_agent,
        min_age_hours: int = settings.branch_pruning_min_age_hours,
        projects_root: Path | None = None,
    ):
        self.enabled = enabled
        self.interval = timedelta(hours=interval_hours)
        self.dry_run = dry_run
        self.base_branch = base_branch
        self.agent = agent
        self.min_age_seconds = min_age_hours * 3600
        self.projects_root = projects_root or PROJECTS_ROOT
        self._protected_patterns = self._parse_patterns(protected_patterns)

    @staticmethod
    def _parse_patterns(raw: str) -> list[str]:
        """Parse comma-separated protected patterns into a list."""
        return [p.strip() for p in raw.split(",") if p.strip()]

    def _is_protected(self, branch_name: str) -> bool:
        """Check whether a branch name matches any protected pattern."""
        if branch_name in _BUILTIN_PROTECTED:
            return True
        for pattern in self._protected_patterns:
            if fnmatch(branch_name, pattern):
                return True
        return False

    # Seconds to wait before the first pruning cycle after startup.  This
    # prevents the pruner from holding git_auth_lock while the app is most
    # likely to receive its first webhook triggers (right after a deploy or
    # container restart).
    _STARTUP_DELAY_SECONDS: float = 60.0

    async def run_pruning_loop(self) -> None:
        """Run the branch pruning process on a recurring schedule."""
        if not self.enabled:
            logger.info("Branch pruning is disabled.")
            return

        mode = "DRY-RUN" if self.dry_run else "LIVE"
        logger.info(
            "Starting branch pruning loop (%s) with %s interval "
            "(first cycle in %.0fs).",
            mode,
            self.interval,
            self._STARTUP_DELAY_SECONDS,
        )
        # Delay the first cycle so agent dispatch is not blocked on startup.
        await asyncio.sleep(self._STARTUP_DELAY_SECONDS)
        while True:
            try:
                await self.prune_once()
            except Exception:
                logger.exception("Error during branch pruning cycle.")
            await asyncio.sleep(self.interval.total_seconds())

    async def prune_once(self) -> dict[str, list[str]]:
        """Run a single pruning pass across all discovered project repositories.

        Returns a mapping of project path to list of pruned (or would-be-pruned) branches.
        """
        results: dict[str, list[str]] = {}

        repo_dirs = self._discover_repos()
        if not repo_dirs:
            logger.info("No git repositories found under %s", self.projects_root)
            return results

        # Acquire the shared auth lock for the entire pruning pass to prevent
        # credential races with webhook-triggered agent operations.
        async with git_auth_lock:
            # Authenticate git credentials once before iterating repos
            if not await self._authenticate(self.agent):
                logger.error("Git authentication failed; skipping pruning cycle.")
                return results

            for repo_dir in repo_dirs:
                try:
                    pruned = await self._prune_repo(repo_dir)
                    if pruned:
                        results[str(repo_dir)] = pruned
                except Exception:
                    logger.exception("Error pruning repo: %s", repo_dir)

        return results

    def _discover_repos(self) -> list[Path]:
        """Discover git repositories under the projects root (namespace/project layout)."""
        repos: list[Path] = []
        if not self.projects_root.is_dir():
            return repos

        for namespace_dir in sorted(self.projects_root.iterdir()):
            if not namespace_dir.is_dir():
                continue
            for project_dir in sorted(namespace_dir.iterdir()):
                if not project_dir.is_dir():
                    continue
                if (project_dir / ".git").is_dir():
                    repos.append(project_dir)
        return repos

    async def _detect_base_branch(self, repo_dir: Path) -> str:
        """Detect the default branch for a repository from the remote.

        Queries ``git remote show origin`` and parses the ``HEAD branch:`` line.
        Falls back to the configured ``self.base_branch`` when detection fails.
        """
        rc, stdout, stderr = await self._run_git(
            ["remote", "show", "origin"], cwd=repo_dir
        )
        if rc != 0:
            logger.debug(
                "Could not query remote for %s, using configured base '%s': %s",
                repo_dir,
                self.base_branch,
                stderr,
            )
            return self.base_branch

        for line in stdout.splitlines():
            if "HEAD branch:" in line:
                match = re.search(r"HEAD branch:\s*(\S+)", line)
                if match:
                    return match.group(1)

        logger.debug(
            "Could not parse HEAD branch for %s, using configured base '%s'",
            repo_dir,
            self.base_branch,
        )
        return self.base_branch

    async def _prune_repo(self, repo_dir: Path) -> list[str]:
        """Prune merged branches in a single repository."""
        # Skip shallow clones -- merge reachability is unreliable
        if (repo_dir / ".git" / "shallow").exists():
            logger.info("Skipping shallow clone: %s", repo_dir)
            return []

        # Fetch and prune stale remote tracking refs
        rc, _, stderr = await self._run_git(["fetch", "--prune", "origin"], cwd=repo_dir)
        if rc != 0:
            logger.warning("git fetch failed for %s: %s", repo_dir, stderr)
            return []

        # Detect per-repo base branch (falls back to configured default)
        base = await self._detect_base_branch(repo_dir)

        # List remote branches merged into origin/<base>
        rc, stdout, stderr = await self._run_git(
            ["branch", "-r", "--merged", f"origin/{base}"],
            cwd=repo_dir,
        )
        if rc != 0:
            logger.warning(
                "git branch --merged failed for %s (base=%s): %s",
                repo_dir,
                base,
                stderr,
            )
            return []

        merged_branches = self._parse_merged_branches(stdout)
        pruned: list[str] = []

        for branch in merged_branches:
            if self._is_protected(branch):
                continue

            # Check merge age -- skip branches merged less than min_age_seconds ago
            if self.min_age_seconds > 0:
                age = await self._get_merge_age(repo_dir, branch, base)
                if age is not None and age < self.min_age_seconds:
                    logger.debug(
                        "Skipping recently-merged branch %s in %s (age=%.0fh, min=%dh)",
                        branch,
                        repo_dir,
                        age / 3600,
                        self.min_age_seconds // 3600,
                    )
                    continue

            if self.dry_run:
                logger.info("[DRY-RUN] Would prune branch %s in %s", branch, repo_dir)
                pruned.append(branch)
                continue

            rc, _, stderr = await self._run_git(
                ["push", "origin", "--delete", branch],
                cwd=repo_dir,
            )
            if rc == 0:
                logger.info("Pruned branch %s in %s", branch, repo_dir)
                pruned.append(branch)
            else:
                logger.warning(
                    "Failed to delete branch %s in %s: %s", branch, repo_dir, stderr
                )

        if pruned:
            mode = "Would prune" if self.dry_run else "Pruned"
            logger.info("%s %d branch(es) in %s", mode, len(pruned), repo_dir)

        return pruned

    @staticmethod
    def _parse_merged_branches(raw_output: str) -> list[str]:
        """Parse output of ``git branch -r --merged`` into branch names.

        Input lines look like ``  origin/feature-x`` or ``  origin/HEAD -> origin/main``.
        Returns short branch names (without the ``origin/`` prefix).
        """
        branches: list[str] = []
        for line in raw_output.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # Skip pointer refs like "origin/HEAD -> origin/main"
            if "->" in line:
                continue
            # Strip remote prefix
            if line.startswith("origin/"):
                branch = line[len("origin/"):]
                branches.append(branch)
        return branches

    async def _get_merge_age(
        self, repo_dir: Path, branch: str, base: str
    ) -> float | None:
        """Return the age (in seconds) since a branch was merged into the base.

        First looks for the merge commit on ``origin/<base>`` that incorporated
        ``origin/<branch>`` (standard merge).  Falls back to the branch tip
        commit date, which is correct for fast-forward merges where the branch
        tip IS the commit on the base branch.

        Returns ``None`` if the age cannot be determined.
        """
        # Try to find the merge commit on the base branch
        rc, stdout, _ = await self._run_git(
            [
                "log", "-1", "--format=%ct", "--merges",
                "--ancestry-path", f"origin/{branch}..origin/{base}",
            ],
            cwd=repo_dir,
        )
        if rc == 0 and stdout.strip():
            try:
                merge_ts = int(stdout.strip())
                return time.time() - merge_ts
            except (ValueError, TypeError):
                pass

        # Fallback: use branch tip commit date (correct for fast-forward merges)
        rc, stdout, _ = await self._run_git(
            ["log", "-1", "--format=%ct", f"origin/{branch}"],
            cwd=repo_dir,
        )
        if rc != 0:
            return None
        try:
            commit_ts = int(stdout.strip())
            return time.time() - commit_ts
        except (ValueError, TypeError):
            return None

    # Per-command timeout (seconds) for git subprocesses.  Prevents a hanging
    # fetch/push from holding git_auth_lock indefinitely and stalling agent
    # dispatch.
    _GIT_TIMEOUT_SECONDS: float = 120.0

    @staticmethod
    async def _run_git(
        args: list[str],
        cwd: Path,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        """Execute a git command and return (returncode, stdout, stderr).

        If *timeout* is provided (or falls back to the class default), the
        subprocess is killed after that many seconds and the call returns a
        non-zero status with a descriptive stderr message.
        """
        if timeout is None:
            timeout = BranchPruner._GIT_TIMEOUT_SECONDS
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            # Drain remaining output after kill to avoid zombie processes.
            await proc.wait()
            cmd_str = " ".join(["git", *args])
            logger.warning(
                "git command timed out after %.0fs: %s (cwd=%s)",
                timeout,
                cmd_str,
                cwd,
            )
            return (
                -1,
                "",
                f"command timed out after {timeout:.0f}s: {cmd_str}",
            )
        return (
            proc.returncode or 0,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )

    # Timeout for the glab-usr authentication subprocess.  Uses the shared
    # constant from git_runtime so all lock holders have the same bound.
    _AUTH_TIMEOUT_SECONDS: float = GLAB_USR_TIMEOUT_SECONDS

    @staticmethod
    async def _authenticate(agent: str) -> bool:
        """Set up git credentials via glab-usr for the configured agent."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "glab-usr",
                agent,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            try:
                _, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=BranchPruner._AUTH_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.error(
                    "glab-usr timed out after %.0fs for agent '%s'",
                    BranchPruner._AUTH_TIMEOUT_SECONDS,
                    agent,
                )
                return False
            if proc.returncode != 0:
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
                logger.error("glab-usr authentication failed: %s", stderr_text)
                return False
            return True
        except FileNotFoundError:
            logger.error("glab-usr command not found")
            return False
        except Exception:
            logger.exception("Exception during glab-usr authentication")
            return False


branch_pruner = BranchPruner()
