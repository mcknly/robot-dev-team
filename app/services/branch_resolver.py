"""Robot Dev Team Project
File: app/services/branch_resolver.py
Description: Resolves and checks out the appropriate branch before agent dispatch.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.services.git_runtime import GLAB_USR_TIMEOUT_SECONDS, git_auth_lock
from app.services.glab import _get_glab_env

LOGGER = get_logger(__name__)


@dataclass
class BackupRecord:
    """A single backup branch created during branch resolution."""

    branch: str
    reason: str


@dataclass
class BranchResult:
    """Result of branch resolution and checkout operation."""

    success: bool
    branch: Optional[str] = None
    error: Optional[str] = None
    backups: List[BackupRecord] = field(default_factory=list)
    switched: bool = False

    # Convenience properties for backward compatibility
    @property
    def backup_branch(self) -> Optional[str]:
        """Return the first backup branch name, or None."""
        return self.backups[0].branch if self.backups else None

    @property
    def backup_reason(self) -> Optional[str]:
        """Return the first backup reason, or None."""
        return self.backups[0].reason if self.backups else None


async def resolve_branch(
    event: Dict[str, Any],
    project_path: str,
    working_dir: str,
    agent: str,
) -> BranchResult:
    """Determine and checkout the appropriate branch for the event.

    Branch resolution rules:
    - MR event: use source_branch from object_attributes
    - Issue with linked MR: API lookup for associated branch
    - New issue (no MR): default branch (queried from remote)

    Args:
        event: Webhook payload dictionary
        project_path: GitLab project path_with_namespace (e.g., 'group/project')
        working_dir: Local filesystem path to the git repository
        agent: Agent name for logging/backup branch naming

    Returns:
        BranchResult with success status and branch information
    """
    if not settings.enable_branch_switch:
        return BranchResult(success=True, switched=False)

    object_kind = event.get("object_kind", "")
    target_branch: Optional[str] = None

    # Determine target branch based on event type
    if object_kind == "merge_request":
        target_branch = _get_branch_from_mr_event(event)
    elif object_kind == "note":
        # Note (comment) on MR or issue
        target_branch = await _get_branch_from_note_event(event, project_path)
    elif object_kind == "issue":
        target_branch = await _get_branch_from_issue_event(event, project_path)
    else:
        # For unknown events, use default branch
        target_branch = await _get_default_branch(working_dir)

    if not target_branch:
        # Fall back to default branch if we couldn't determine target
        target_branch = await _get_default_branch(working_dir)

    if not target_branch:
        return BranchResult(
            success=False,
            error="Could not determine target branch",
        )

    # Check current branch
    current_branch = await _get_current_branch(working_dir)

    if current_branch == target_branch:
        LOGGER.info(
            "Already on target branch '%s', syncing with origin",
            target_branch,
            extra={"branch": target_branch, "working_dir": working_dir},
        )
        # Even though we're on the target branch, sync with origin to pull any
        # changes that were pushed externally
        return await _sync_current_branch(working_dir, target_branch, agent)

    # Check for uncommitted changes
    is_clean = await _is_working_tree_clean(working_dir)

    if not is_clean:
        # Create backup branch with uncommitted changes
        backup_result = await _create_backup_branch(
            working_dir=working_dir,
            current_branch=current_branch,
            agent=agent,
        )
        if not backup_result.success:
            # Cannot continue with dirty working tree if backup failed
            return BranchResult(
                success=False,
                error=f"Uncommitted changes present and backup failed: {backup_result.error}",
            )
        LOGGER.warning(
            "Created backup branch for uncommitted changes",
            extra={
                "backup_branch": backup_result.backup_branch,
                "original_branch": current_branch,
                "working_dir": working_dir,
            },
        )

    # Fetch and checkout target branch
    checkout_result = await _checkout_branch(working_dir, target_branch, agent)

    if checkout_result.success:
        # Merge backups from dirty-tree backup and checkout (which may
        # have created its own backup for local commits ahead of origin)
        all_backups: List[BackupRecord] = []
        if not is_clean:
            all_backups.extend(backup_result.backups)
        all_backups.extend(checkout_result.backups)
        return BranchResult(
            success=True,
            branch=target_branch,
            switched=True,
            backups=all_backups,
        )

    # Checkout failed - preserve any backups created before the failure
    prior_backups: List[BackupRecord] = []
    if not is_clean:
        prior_backups.extend(backup_result.backups)
    prior_backups.extend(checkout_result.backups)

    if is_clean:
        LOGGER.warning(
            "Branch checkout failed for '%s', continuing on current branch '%s': %s",
            target_branch,
            current_branch,
            checkout_result.error,
            extra={
                "target_branch": target_branch,
                "current_branch": current_branch,
                "error": checkout_result.error,
            },
        )
        return BranchResult(
            success=True,
            branch=current_branch,
            switched=False,
            error=f"Checkout failed (continuing on {current_branch}): {checkout_result.error}",
            backups=prior_backups,
        )

    return BranchResult(
        success=checkout_result.success,
        branch=checkout_result.branch,
        error=checkout_result.error,
        switched=checkout_result.switched,
        backups=prior_backups,
    )


def _get_branch_from_mr_event(event: Dict[str, Any]) -> Optional[str]:
    """Extract source_branch from MR webhook payload."""
    obj_attrs = event.get("object_attributes", {})
    return obj_attrs.get("source_branch")


async def _get_branch_from_note_event(
    event: Dict[str, Any],
    project_path: str,
) -> Optional[str]:
    """Extract branch from note (comment) event.

    Notes on MRs use the MR's source branch.
    Notes on issues look up linked MRs.
    """
    noteable_type = event.get("object_attributes", {}).get("noteable_type", "")

    if noteable_type == "MergeRequest":
        # Comment on MR - get source branch from merge_request object
        mr = event.get("merge_request", {})
        return mr.get("source_branch")

    if noteable_type == "Issue":
        # Comment on issue - look up linked MRs
        issue = event.get("issue", {})
        iid = issue.get("iid")
        if iid is not None:
            note_body = event.get("object_attributes", {}).get("note", "")
            return await _lookup_issue_branch(
                project_path, iid, note_body=note_body
            )

    return None


async def _get_branch_from_issue_event(
    event: Dict[str, Any],
    project_path: str,
) -> Optional[str]:
    """Determine branch for issue event by looking up linked MRs."""
    obj_attrs = event.get("object_attributes", {})
    iid = obj_attrs.get("iid")

    if iid is not None:
        linked_branch = await _lookup_issue_branch(project_path, iid)
        if linked_branch:
            return linked_branch

    # No linked MR, return None to fall back to default branch
    return None


async def _lookup_issue_branch(
    project_path: str,
    issue_iid: int,
    note_body: str = "",
) -> Optional[str]:
    """Look up the source branch from MRs linked to an issue via GitLab API.

    When smart branch selection is enabled, uses the closes_issues API and
    note-mention parsing to deterministically select the best MR. When disabled,
    returns the source_branch of the first open MR (legacy behavior).
    """
    encoded_path = project_path.replace("/", "%2F")
    api_path = f"projects/{encoded_path}/issues/{issue_iid}/related_merge_requests"

    try:
        proc = await asyncio.create_subprocess_exec(
            "glab",
            "api",
            api_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_get_glab_env(),
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.glab_timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            LOGGER.warning(
                "Timed out fetching related MRs for issue",
                extra={
                    "project": project_path,
                    "issue_iid": issue_iid,
                    "timeout_seconds": settings.glab_timeout_seconds,
                },
            )
            return None

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")
            LOGGER.debug(
                "Failed to fetch related MRs for issue",
                extra={
                    "project": project_path,
                    "issue_iid": issue_iid,
                    "stderr": stderr_text,
                },
            )
            return None

        mrs: List[Dict[str, Any]] = json.loads(stdout.decode("utf-8"))

        # Filter to open MRs with a valid source_branch
        open_mrs = [
            mr for mr in mrs
            if mr.get("state") == "opened" and mr.get("source_branch")
        ]

        if not open_mrs:
            return None

        if not settings.enable_smart_branch_selection:
            # Legacy behavior: return first open MR's source branch
            mr = open_mrs[0]
            LOGGER.info(
                "Found linked MR for issue (legacy mode)",
                extra={
                    "issue_iid": issue_iid,
                    "mr_iid": mr.get("iid"),
                    "source_branch": mr.get("source_branch"),
                },
            )
            return mr["source_branch"]

        # Smart selection
        return await _smart_select_branch(
            project_path=project_path,
            encoded_path=encoded_path,
            issue_iid=issue_iid,
            open_mrs=open_mrs,
            note_body=note_body,
        )

    except Exception as exc:
        LOGGER.debug(
            "Exception looking up issue branch",
            extra={"error": str(exc), "issue_iid": issue_iid},
        )
        return None


_SMART_BRANCH_MAX_CANDIDATES = 20


async def _smart_select_branch(
    project_path: str,
    encoded_path: str,
    issue_iid: int,
    open_mrs: List[Dict[str, Any]],
    note_body: str = "",
) -> Optional[str]:
    """Deterministically select the best MR branch using smart heuristics.

    Ranking signals (highest priority first):
    1. Note-mentioned: MR iid explicitly referenced in the note body (!<iid>)
    2. Explicitly closes issue: closes_issues API confirms the MR closes this issue
    3. Most recently updated (updated_at DESC)
    4. Highest iid (iid DESC) as stable tiebreaker
    """
    # Sort deterministically before truncating (avoid mutating caller's list)
    candidates = sorted(
        open_mrs,
        key=lambda mr: (mr.get("updated_at", ""), mr.get("iid", 0)),
        reverse=True,
    )[:_SMART_BRANCH_MAX_CANDIDATES]

    if len(open_mrs) > _SMART_BRANCH_MAX_CANDIDATES:
        LOGGER.warning(
            "Truncated MR candidates for smart branch selection",
            extra={
                "issue_iid": issue_iid,
                "total_open_mrs": len(open_mrs),
                "max_candidates": _SMART_BRANCH_MAX_CANDIDATES,
            },
        )

    # Parse note mentions (!<iid> patterns)
    note_mentioned_iids: set[int] = set()
    if note_body:
        note_mentioned_iids = {
            int(m) for m in re.findall(r"(?<!\w)!(\d+)(?!\w)", note_body)
        }

    # If only one candidate, skip the closes_issues API call
    if len(candidates) == 1:
        mr = candidates[0]
        LOGGER.info(
            "Single open MR for issue, selected directly",
            extra={
                "issue_iid": issue_iid,
                "mr_iid": mr.get("iid"),
                "source_branch": mr["source_branch"],
            },
        )
        return mr["source_branch"]

    # Fetch closes_issues for each candidate concurrently
    closes_map = await _fetch_closes_issues_batch(
        encoded_path=encoded_path,
        candidates=candidates,
        issue_iid=issue_iid,
    )

    # Build scored list and apply explicit-close filter.
    # Note-mentioned MRs are always kept in the pool to allow intentional
    # steering via comments, even when a different MR explicitly closes the issue.
    explicitly_closing = [
        mr for mr in candidates if closes_map.get(mr.get("iid")) is True
    ]
    if explicitly_closing:
        pool = [
            mr for mr in candidates
            if closes_map.get(mr.get("iid")) is True
            or mr.get("iid") in note_mentioned_iids
        ]
    else:
        pool = list(candidates)

    def _rank_key(mr: Dict[str, Any]) -> tuple:
        mr_iid = mr.get("iid", 0)
        return (
            mr_iid in note_mentioned_iids,
            closes_map.get(mr_iid) is True,
            mr.get("updated_at", ""),
            mr_iid,
        )

    pool.sort(key=_rank_key, reverse=True)
    selected = pool[0]

    LOGGER.info(
        "Smart branch selection result",
        extra={
            "issue_iid": issue_iid,
            "selected_mr_iid": selected.get("iid"),
            "source_branch": selected["source_branch"],
            "candidate_count": len(candidates),
            "close_match_count": len(explicitly_closing),
            "note_mention_used": selected.get("iid") in note_mentioned_iids,
        },
    )

    return selected["source_branch"]


async def _fetch_closes_issues_batch(
    encoded_path: str,
    candidates: List[Dict[str, Any]],
    issue_iid: int,
) -> Dict[int, Optional[bool]]:
    """Fetch closes_issues for each candidate MR concurrently.

    Returns a dict mapping MR iid -> True (explicitly closes issue),
    False (does not close issue), or None (API call failed).
    """
    async def _check_one(mr_iid: int) -> tuple[int, Optional[bool]]:
        api_path = f"projects/{encoded_path}/merge_requests/{mr_iid}/closes_issues"
        try:
            proc = await asyncio.create_subprocess_exec(
                "glab",
                "api",
                api_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_get_glab_env(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=settings.glab_timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                LOGGER.warning(
                    "Timed out fetching closes_issues for MR",
                    extra={"mr_iid": mr_iid},
                )
                return (mr_iid, None)

            if proc.returncode != 0:
                LOGGER.debug(
                    "Failed to fetch closes_issues for MR",
                    extra={"mr_iid": mr_iid},
                )
                return (mr_iid, None)

            issues: List[Dict[str, Any]] = json.loads(
                stdout.decode("utf-8")
            )
            closes_this = any(
                i.get("iid") == issue_iid for i in issues
            )
            return (mr_iid, closes_this)

        except Exception as exc:
            LOGGER.debug(
                "Exception fetching closes_issues for MR",
                extra={"mr_iid": mr_iid, "error": str(exc)},
            )
            return (mr_iid, None)

    # Filter out candidates without a valid integer iid to avoid wasteful
    # API calls to merge_requests/0/closes_issues on malformed payloads
    valid = [(mr.get("iid"), mr) for mr in candidates if mr.get("iid")]
    tasks = [_check_one(iid) for iid, _ in valid]
    results = await asyncio.gather(*tasks)
    return dict(results)


async def _get_default_branch(working_dir: str) -> Optional[str]:
    """Query the default branch from git remote."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "remote",
            "show",
            "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            LOGGER.debug(
                "Failed to query default branch from remote",
                extra={"working_dir": working_dir},
            )
            return "main"  # Fallback

        stdout_text = stdout.decode("utf-8", errors="replace")
        # Parse "HEAD branch: main" line
        for line in stdout_text.splitlines():
            if "HEAD branch:" in line:
                match = re.search(r"HEAD branch:\s*(\S+)", line)
                if match:
                    return match.group(1)

        return "main"  # Fallback

    except Exception as exc:
        LOGGER.debug(
            "Exception getting default branch",
            extra={"error": str(exc), "working_dir": working_dir},
        )
        return "main"  # Fallback


async def _get_current_branch(working_dir: str) -> Optional[str]:
    """Get the current branch name."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        stdout, _ = await proc.communicate()

        if proc.returncode == 0:
            return stdout.decode("utf-8").strip()
        return None

    except Exception:
        return None


async def _is_working_tree_clean(working_dir: str) -> bool:
    """Check if the working tree has no uncommitted changes."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            return False

        # Empty output means clean working tree
        return len(stdout.strip()) == 0

    except Exception:
        return False


async def _create_backup_branch(
    working_dir: str,
    current_branch: Optional[str],
    agent: str,
) -> BranchResult:
    """Create a backup branch with all uncommitted changes and push to remote.

    Creates branch: backup/<agent>/<original-branch>-<timestamp>
    Commits all changes with an auto-backup message.
    Pushes the backup branch to origin.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch_base = current_branch or "unknown"
    backup_branch = f"backup/{agent}/{branch_base}-{timestamp}"

    # Authenticate git for push operations
    await _authenticate_git(agent)

    try:
        # Create and checkout backup branch
        proc = await asyncio.create_subprocess_exec(
            "git",
            "checkout",
            "-b",
            backup_branch,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            return BranchResult(
                success=False,
                error=f"Failed to create backup branch: {stderr.decode('utf-8', errors='replace')}",
            )

        # Stage all changes
        proc = await asyncio.create_subprocess_exec(
            "git",
            "add",
            "-A",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        await proc.communicate()

        if proc.returncode != 0:
            return BranchResult(
                success=False,
                error="Failed to stage changes for backup",
            )

        # Commit with backup message
        commit_message = f"[auto-backup] Uncommitted changes from {branch_base}"
        proc = await asyncio.create_subprocess_exec(
            "git",
            "commit",
            "-m",
            commit_message,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            # May fail if nothing to commit after add -A (e.g., only untracked ignored files)
            stderr_text = stderr.decode("utf-8", errors="replace")
            if "nothing to commit" not in stderr_text.lower():
                return BranchResult(
                    success=False,
                    error=f"Failed to commit backup: {stderr_text}",
                )

        # Push backup branch to origin
        proc = await asyncio.create_subprocess_exec(
            "git",
            "push",
            "-u",
            "origin",
            backup_branch,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")
            LOGGER.error(
                "Failed to push backup branch to origin",
                extra={
                    "backup_branch": backup_branch,
                    "stderr": stderr_text,
                },
            )
            # Push failure is fatal to prevent data loss if subsequent hard reset succeeds
            return BranchResult(
                success=False,
                backups=[BackupRecord(branch=backup_branch, reason="uncommitted_changes")],
                error=f"Failed to push backup branch to origin: {stderr_text}",
            )

        return BranchResult(
            success=True,
            backups=[BackupRecord(branch=backup_branch, reason="uncommitted_changes")],
        )

    except Exception as exc:
        return BranchResult(
            success=False,
            error=f"Exception during backup: {str(exc)}",
        )


async def _get_branch_divergence(
    working_dir: str,
    branch: str,
) -> tuple[Optional[int], Optional[int]]:
    """Check how many commits the local branch is behind/ahead of origin.

    Returns:
        Tuple of (behind_count, ahead_count):
        - (int, int) on success
        - (None, None) if the check failed (unknown state)
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-list",
            "--left-right",
            "--count",
            f"origin/{branch}...{branch}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            return (None, None)  # Unknown state - check failed

        # Output format: "<behind>\t<ahead>"
        output = stdout.decode("utf-8").strip()
        parts = output.split()
        if len(parts) == 2:
            return (int(parts[0]), int(parts[1]))  # (behind, ahead)
        return (None, None)  # Unknown state - unexpected output format

    except Exception:
        return (None, None)  # Unknown state - exception occurred


async def _get_branch_ahead_count(working_dir: str, branch: str) -> Optional[int]:
    """Check how many commits the local branch is ahead of origin.

    Returns:
        - The number of commits local is ahead (0 or positive integer) on success
        - None if the check failed (unknown state)
    """
    _, ahead = await _get_branch_divergence(working_dir, branch)
    return ahead


async def _backup_local_commits(
    working_dir: str,
    branch: str,
    agent: str,
) -> BranchResult:
    """Create a backup branch to preserve local commits before hard reset.

    Creates branch: backup/<agent>/<branch>-commits-<timestamp>
    Pushes the backup branch to origin.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_branch = f"backup/{agent}/{branch}-commits-{timestamp}"

    # Authenticate git for push operations
    await _authenticate_git(agent)

    try:
        # Create backup branch at current HEAD
        proc = await asyncio.create_subprocess_exec(
            "git",
            "branch",
            backup_branch,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")
            return BranchResult(
                success=False,
                error=f"Failed to create backup branch for local commits: {stderr_text}",
            )

        # Push backup branch to origin
        proc = await asyncio.create_subprocess_exec(
            "git",
            "push",
            "-u",
            "origin",
            backup_branch,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")
            LOGGER.error(
                "Failed to push backup branch for local commits",
                extra={
                    "backup_branch": backup_branch,
                    "stderr": stderr_text,
                },
            )
            return BranchResult(
                success=False,
                backups=[BackupRecord(branch=backup_branch, reason="local_commits")],
                error=f"Failed to push backup branch for local commits: {stderr_text}",
            )

        LOGGER.info(
            "Created backup branch for local commits",
            extra={
                "backup_branch": backup_branch,
                "original_branch": branch,
            },
        )

        return BranchResult(
            success=True,
            backups=[BackupRecord(branch=backup_branch, reason="local_commits")],
        )

    except Exception as exc:
        return BranchResult(
            success=False,
            error=f"Exception during local commits backup: {str(exc)}",
        )


async def _sync_current_branch(
    working_dir: str,
    branch: str,
    agent: str,
) -> BranchResult:
    """Sync the current branch with origin by fetching and resetting if needed.

    This handles the case where we're already on the target branch but the
    remote may have new commits. If local has uncommitted changes or unpushed
    commits, they are backed up before resetting.

    Returns:
        BranchResult with success status and any backup branch created.
    """
    # Authenticate git for fetch operations
    await _authenticate_git(agent)

    # Fetch the branch from origin
    proc = await asyncio.create_subprocess_exec(
        "git",
        "fetch",
        "origin",
        branch,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=working_dir,
        env=os.environ.copy(),
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace")
        LOGGER.debug(
            "Failed to fetch branch from origin during sync",
            extra={"branch": branch, "stderr": stderr_text},
        )
        # Can't sync if fetch fails, but this isn't fatal
        return BranchResult(
            success=True,
            branch=branch,
            switched=False,
            error=f"Fetch failed during sync: {stderr_text}",
        )

    # Check divergence from origin
    behind, ahead = await _get_branch_divergence(working_dir, branch)

    if behind is None:
        # Couldn't determine divergence, skip sync
        LOGGER.debug(
            "Could not determine branch divergence, skipping sync",
            extra={"branch": branch},
        )
        return BranchResult(success=True, branch=branch, switched=False)

    if behind == 0 and ahead == 0:
        # Already in sync
        LOGGER.info(
            "Branch '%s' already in sync with origin",
            branch,
            extra={"branch": branch},
        )
        return BranchResult(success=True, branch=branch, switched=False)

    LOGGER.info(
        "Branch '%s' diverged from origin (behind=%d, ahead=%d), syncing",
        branch,
        behind,
        ahead,
        extra={"branch": branch, "behind": behind, "ahead": ahead},
    )

    backups: List[BackupRecord] = []

    # Check for uncommitted changes
    is_clean = await _is_working_tree_clean(working_dir)

    if not is_clean:
        # Backup uncommitted changes first
        backup_result = await _create_backup_branch(
            working_dir=working_dir,
            current_branch=branch,
            agent=agent,
        )
        if not backup_result.success:
            return BranchResult(
                success=False,
                error=f"Sync failed: could not backup uncommitted changes: {backup_result.error}",
            )
        backups.extend(backup_result.backups)
        LOGGER.warning(
            "Backed up uncommitted changes before sync",
            extra={"backup_branch": backup_result.backup_branch},
        )

    # If local has unpushed commits, back them up
    if ahead and ahead > 0:
        commits_backup = await _backup_local_commits(working_dir, branch, agent)
        if not commits_backup.success:
            return BranchResult(
                success=False,
                error=f"Sync failed: could not backup local commits: {commits_backup.error}",
            )
        backups.extend(commits_backup.backups)
        LOGGER.warning(
            "Backed up local commits before sync",
            extra={
                "backup_branch": commits_backup.backup_branch,
                "ahead_count": ahead,
            },
        )

    # Reset to origin
    proc = await asyncio.create_subprocess_exec(
        "git",
        "reset",
        "--hard",
        f"origin/{branch}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=working_dir,
        env=os.environ.copy(),
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace")
        LOGGER.error(
            "Failed to reset to origin during sync",
            extra={"branch": branch, "stderr": stderr_text},
        )
        return BranchResult(
            success=False,
            error=f"Reset failed during sync: {stderr_text}",
            backups=backups,
        )

    LOGGER.info(
        "Successfully synced branch '%s' with origin",
        branch,
        extra={"branch": branch, "behind": behind, "ahead": ahead},
    )

    return BranchResult(
        success=True,
        branch=branch,
        switched=False,
        backups=backups,
    )


async def _authenticate_git(agent: str) -> bool:
    """Set up git credentials for the given agent using glab-usr.

    This configures the git credential helper and author identity for
    authenticated operations like fetch and push.

    Acquires the shared git auth lock to prevent credential races with
    the background BranchPruner.
    """
    async with git_auth_lock:
        try:
            proc = await asyncio.create_subprocess_exec(
                "glab-usr",
                agent,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=GLAB_USR_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                LOGGER.warning(
                    "glab-usr timed out after %.0fs for agent '%s'",
                    GLAB_USR_TIMEOUT_SECONDS,
                    agent,
                )
                return False

            if proc.returncode != 0:
                stderr_text = stderr.decode("utf-8", errors="replace")
                LOGGER.warning(
                    "glab-usr authentication failed",
                    extra={"agent": agent, "stderr": stderr_text},
                )
                return False

            return True

        except FileNotFoundError:
            LOGGER.warning("glab-usr command not found, git operations may fail")
            return False
        except Exception as exc:
            LOGGER.warning(
                "Exception during glab-usr authentication",
                extra={"error": str(exc), "agent": agent},
            )
            return False


async def _checkout_branch(
    working_dir: str,
    branch: str,
    agent: str = "unknown",
) -> BranchResult:
    """Fetch and checkout the specified branch.

    Always fetches the specific branch from origin before checkout.
    Only performs hard reset if local branch is not ahead of origin.
    If local branch has unpushed commits, creates a backup branch first.
    """
    # Authenticate git for fetch/push operations
    await _authenticate_git(agent)

    # Fetch the specific branch from origin
    proc = await asyncio.create_subprocess_exec(
        "git",
        "fetch",
        "origin",
        branch,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=working_dir,
        env=os.environ.copy(),
    )
    _, stderr = await proc.communicate()

    fetch_succeeded = proc.returncode == 0
    if not fetch_succeeded:
        stderr_text = stderr.decode("utf-8", errors="replace")
        # Branch may not exist on remote (e.g., default branch for new issue)
        LOGGER.debug(
            "Failed to fetch branch from origin",
            extra={"branch": branch, "stderr": stderr_text},
        )
        # Try to checkout anyway - branch may exist locally

    # Checkout the branch
    proc = await asyncio.create_subprocess_exec(
        "git",
        "checkout",
        branch,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=working_dir,
        env=os.environ.copy(),
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace")
        # Try to create tracking branch if checkout failed
        proc = await asyncio.create_subprocess_exec(
            "git",
            "checkout",
            "-b",
            branch,
            f"origin/{branch}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")
            return BranchResult(
                success=False,
                error=f"Failed to checkout branch {branch}: {stderr_text}",
            )

    # Track backup records for local commits (if created)
    checkout_backups: List[BackupRecord] = []

    # Only reset if fetch succeeded (we have a valid origin/<branch> to reset to)
    if fetch_succeeded:
        # Check if local branch is ahead of origin before resetting
        ahead_count = await _get_branch_ahead_count(working_dir, branch)

        # If ahead_count is None, the check failed - treat as unknown state
        # and force backup to be safe
        if ahead_count is None:
            LOGGER.warning(
                "Could not determine if local branch is ahead of origin, "
                "creating backup before reset to be safe",
                extra={
                    "branch": branch,
                    "working_dir": working_dir,
                },
            )
            # Create backup branch to preserve any potential local commits
            backup_result = await _backup_local_commits(working_dir, branch, agent)
            if not backup_result.success:
                # Backup failed - don't proceed with reset to prevent data loss
                LOGGER.error(
                    "Aborting hard reset: failed to backup local commits (unknown ahead state)",
                    extra={
                        "branch": branch,
                        "error": backup_result.error,
                    },
                )
                return BranchResult(
                    success=False,
                    error=f"Cannot reset branch: backup of local commits failed: {backup_result.error}",
                )
            checkout_backups.extend(backup_result.backups)
            LOGGER.info(
                "Local commits backed up (unknown ahead state), proceeding with reset",
                extra={
                    "backup_branch": backup_result.backup_branch,
                },
            )
        elif ahead_count > 0:
            LOGGER.warning(
                "Local branch is ahead of origin, creating backup before reset",
                extra={
                    "branch": branch,
                    "ahead_count": ahead_count,
                    "working_dir": working_dir,
                },
            )
            # Create backup branch to preserve local commits
            backup_result = await _backup_local_commits(working_dir, branch, agent)
            if not backup_result.success:
                # Backup failed - don't proceed with reset to prevent data loss
                LOGGER.error(
                    "Aborting hard reset: failed to backup local commits",
                    extra={
                        "branch": branch,
                        "error": backup_result.error,
                    },
                )
                return BranchResult(
                    success=False,
                    error=f"Cannot reset branch: backup of local commits failed: {backup_result.error}",
                )
            checkout_backups.extend(backup_result.backups)
            LOGGER.info(
                "Local commits backed up, proceeding with reset",
                extra={
                    "backup_branch": backup_result.backup_branch,
                },
            )

        # Reset to origin/<branch> to ensure we're in sync
        proc = await asyncio.create_subprocess_exec(
            "git",
            "reset",
            "--hard",
            f"origin/{branch}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=os.environ.copy(),
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")
            LOGGER.debug(
                "Failed to reset to origin branch",
                extra={"branch": branch, "stderr": stderr_text},
            )
    else:
        LOGGER.debug(
            "Skipping hard reset: fetch from origin failed (branch may not exist on remote)",
            extra={"branch": branch},
        )

    LOGGER.info(
        "Checked out branch '%s'",
        branch,
        extra={"branch": branch, "working_dir": working_dir},
    )

    return BranchResult(
        success=True,
        branch=branch,
        switched=True,
        backups=checkout_backups,
    )


def get_branch_context(event: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Extract branch context from webhook event for prompt substitution.

    Returns dict with keys: SOURCE_BRANCH, TARGET_BRANCH
    """
    obj_attrs = event.get("object_attributes", {})
    mr = event.get("merge_request", {})

    source_branch = obj_attrs.get("source_branch") or mr.get("source_branch")
    target_branch = obj_attrs.get("target_branch") or mr.get("target_branch")

    return {
        "SOURCE_BRANCH": source_branch,
        "TARGET_BRANCH": target_branch,
    }


__all__ = ["resolve_branch", "BranchResult", "BackupRecord", "get_branch_context"]
