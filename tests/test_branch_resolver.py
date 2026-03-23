"""Robot Dev Team Project
File: tests/test_branch_resolver.py
Description: Pytest coverage for branch resolution logic.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import json

import pytest

from app.core.config import settings
from app.services import branch_resolver
from app.services.branch_resolver import (
    BackupRecord,
    BranchResult,
    get_branch_context,
    resolve_branch,
    _get_branch_from_mr_event,
    _smart_select_branch,
    _fetch_closes_issues_batch,
    _lookup_issue_branch,
)


class TestGetBranchFromMrEvent:
    """Tests for extracting branch from MR webhook payloads."""

    def test_extracts_source_branch(self):
        event = {
            "object_kind": "merge_request",
            "object_attributes": {
                "source_branch": "feature/test-branch",
                "target_branch": "main",
            },
        }
        assert _get_branch_from_mr_event(event) == "feature/test-branch"

    def test_returns_none_for_missing_source_branch(self):
        event = {
            "object_kind": "merge_request",
            "object_attributes": {
                "target_branch": "main",
            },
        }
        assert _get_branch_from_mr_event(event) is None

    def test_returns_none_for_empty_event(self):
        assert _get_branch_from_mr_event({}) is None


class TestGetBranchContext:
    """Tests for branch context extraction for prompt substitution."""

    def test_extracts_mr_branches(self):
        event = {
            "object_kind": "merge_request",
            "object_attributes": {
                "source_branch": "feature/new-feature",
                "target_branch": "main",
            },
        }
        ctx = get_branch_context(event)
        assert ctx["SOURCE_BRANCH"] == "feature/new-feature"
        assert ctx["TARGET_BRANCH"] == "main"

    def test_extracts_from_merge_request_object(self):
        event = {
            "object_kind": "note",
            "merge_request": {
                "source_branch": "fix/bug-123",
                "target_branch": "develop",
            },
        }
        ctx = get_branch_context(event)
        assert ctx["SOURCE_BRANCH"] == "fix/bug-123"
        assert ctx["TARGET_BRANCH"] == "develop"

    def test_returns_none_for_issue_event(self):
        event = {
            "object_kind": "issue",
            "object_attributes": {
                "title": "New issue",
            },
        }
        ctx = get_branch_context(event)
        assert ctx["SOURCE_BRANCH"] is None
        assert ctx["TARGET_BRANCH"] is None


class TestBranchResult:
    """Tests for BranchResult dataclass."""

    def test_success_result(self):
        result = BranchResult(success=True, branch="main", switched=True)
        assert result.success is True
        assert result.branch == "main"
        assert result.switched is True
        assert result.error is None
        assert result.backup_branch is None

    def test_error_result(self):
        result = BranchResult(success=False, error="Branch not found")
        assert result.success is False
        assert result.error == "Branch not found"
        assert result.branch is None

    def test_result_with_backup(self):
        result = BranchResult(
            success=True,
            branch="feature/test",
            switched=True,
            backups=[BackupRecord(branch="backup/claude/main-20250128-120000", reason="uncommitted_changes")],
        )
        assert result.backup_branch == "backup/claude/main-20250128-120000"
        assert result.backup_reason == "uncommitted_changes"

    def test_result_with_multiple_backups(self):
        result = BranchResult(
            success=True,
            branch="feature/test",
            switched=True,
            backups=[
                BackupRecord(branch="backup/claude/main-20250128-120000", reason="uncommitted_changes"),
                BackupRecord(branch="backup/claude/main-commits-20250128-120000", reason="local_commits"),
            ],
        )
        assert len(result.backups) == 2
        assert result.backups[0].branch == "backup/claude/main-20250128-120000"
        assert result.backups[0].reason == "uncommitted_changes"
        assert result.backups[1].branch == "backup/claude/main-commits-20250128-120000"
        assert result.backups[1].reason == "local_commits"
        # Backward compat properties return first backup
        assert result.backup_branch == "backup/claude/main-20250128-120000"
        assert result.backup_reason == "uncommitted_changes"

    def test_result_no_backups(self):
        result = BranchResult(success=True, branch="main")
        assert result.backups == []
        assert result.backup_branch is None
        assert result.backup_reason is None


@pytest.mark.asyncio
async def test_resolve_branch_disabled_returns_success(monkeypatch):
    """When enable_branch_switch is False, resolve_branch returns success without action."""
    monkeypatch.setattr(settings, "enable_branch_switch", False)

    result = await resolve_branch(
        event={"object_kind": "merge_request"},
        project_path="group/project",
        working_dir="/tmp/project",
        agent="claude",
    )

    assert result.success is True
    assert result.switched is False


@pytest.mark.asyncio
async def test_resolve_branch_mr_event_extracts_source_branch(monkeypatch, tmp_path):
    """MR events should use source_branch from payload."""
    monkeypatch.setattr(settings, "enable_branch_switch", True)

    # Mock git operations
    current_branch = "main"
    target_branch = "feature/test"

    async def mock_get_current_branch(working_dir):
        return current_branch

    async def mock_is_working_tree_clean(working_dir):
        return True

    async def mock_checkout_branch(working_dir, branch, agent="unknown"):
        return BranchResult(success=True, branch=branch, switched=True)

    monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
    monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
    monkeypatch.setattr(branch_resolver, "_checkout_branch", mock_checkout_branch)

    event = {
        "object_kind": "merge_request",
        "object_attributes": {
            "source_branch": target_branch,
            "target_branch": "main",
        },
    }

    result = await resolve_branch(
        event=event,
        project_path="group/project",
        working_dir=str(tmp_path),
        agent="claude",
    )

    assert result.success is True
    assert result.branch == target_branch
    assert result.switched is True


@pytest.mark.asyncio
async def test_resolve_branch_already_on_target(monkeypatch, tmp_path):
    """When already on target branch, no switch should occur."""
    monkeypatch.setattr(settings, "enable_branch_switch", True)

    async def mock_get_current_branch(working_dir):
        return "feature/test"

    async def mock_get_default_branch(working_dir):
        return "main"

    monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
    monkeypatch.setattr(branch_resolver, "_get_default_branch", mock_get_default_branch)

    event = {
        "object_kind": "merge_request",
        "object_attributes": {
            "source_branch": "feature/test",
            "target_branch": "main",
        },
    }

    result = await resolve_branch(
        event=event,
        project_path="group/project",
        working_dir=str(tmp_path),
        agent="claude",
    )

    assert result.success is True
    assert result.branch == "feature/test"
    assert result.switched is False


@pytest.mark.asyncio
async def test_resolve_branch_dirty_tree_creates_backup(monkeypatch, tmp_path):
    """Uncommitted changes should trigger backup branch creation."""
    monkeypatch.setattr(settings, "enable_branch_switch", True)

    async def mock_get_current_branch(working_dir):
        return "main"

    async def mock_is_working_tree_clean(working_dir):
        return False

    async def mock_create_backup_branch(working_dir, current_branch, agent):
        return BranchResult(
            success=True,
            backups=[BackupRecord(branch=f"backup/{agent}/{current_branch}-20250128-120000", reason="uncommitted_changes")],
        )

    async def mock_checkout_branch(working_dir, branch, agent="unknown"):
        return BranchResult(success=True, branch=branch, switched=True)

    monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
    monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
    monkeypatch.setattr(branch_resolver, "_create_backup_branch", mock_create_backup_branch)
    monkeypatch.setattr(branch_resolver, "_checkout_branch", mock_checkout_branch)

    event = {
        "object_kind": "merge_request",
        "object_attributes": {
            "source_branch": "feature/new-work",
            "target_branch": "main",
        },
    }

    result = await resolve_branch(
        event=event,
        project_path="group/project",
        working_dir=str(tmp_path),
        agent="claude",
    )

    assert result.success is True
    assert result.branch == "feature/new-work"
    assert result.switched is True
    assert result.backup_branch == "backup/claude/main-20250128-120000"


@pytest.mark.asyncio
async def test_resolve_branch_dirty_tree_backup_fails(monkeypatch, tmp_path):
    """If backup fails with dirty tree, resolution should fail."""
    monkeypatch.setattr(settings, "enable_branch_switch", True)

    async def mock_get_current_branch(working_dir):
        return "main"

    async def mock_is_working_tree_clean(working_dir):
        return False

    async def mock_create_backup_branch(working_dir, current_branch, agent):
        return BranchResult(success=False, error="Push failed")

    monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
    monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
    monkeypatch.setattr(branch_resolver, "_create_backup_branch", mock_create_backup_branch)

    event = {
        "object_kind": "merge_request",
        "object_attributes": {
            "source_branch": "feature/new-work",
        },
    }

    result = await resolve_branch(
        event=event,
        project_path="group/project",
        working_dir=str(tmp_path),
        agent="claude",
    )

    assert result.success is False
    assert "backup failed" in result.error.lower()


@pytest.mark.asyncio
async def test_resolve_branch_checkout_fails_clean_tree_continues(monkeypatch, tmp_path):
    """If checkout fails but tree is clean, continue on current branch."""
    monkeypatch.setattr(settings, "enable_branch_switch", True)

    async def mock_get_current_branch(working_dir):
        return "main"

    async def mock_is_working_tree_clean(working_dir):
        return True

    async def mock_checkout_branch(working_dir, branch, agent="unknown"):
        return BranchResult(success=False, error="Branch does not exist")

    monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
    monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
    monkeypatch.setattr(branch_resolver, "_checkout_branch", mock_checkout_branch)

    event = {
        "object_kind": "merge_request",
        "object_attributes": {
            "source_branch": "feature/nonexistent",
        },
    }

    result = await resolve_branch(
        event=event,
        project_path="group/project",
        working_dir=str(tmp_path),
        agent="claude",
    )

    # Should succeed but stay on current branch
    assert result.success is True
    assert result.branch == "main"
    assert result.switched is False
    assert "Checkout failed" in result.error


@pytest.mark.asyncio
async def test_resolve_branch_note_on_mr(monkeypatch, tmp_path):
    """Comment on MR should use merge_request.source_branch."""
    monkeypatch.setattr(settings, "enable_branch_switch", True)

    async def mock_get_current_branch(working_dir):
        return "main"

    async def mock_is_working_tree_clean(working_dir):
        return True

    async def mock_checkout_branch(working_dir, branch, agent="unknown"):
        return BranchResult(success=True, branch=branch, switched=True)

    monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
    monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
    monkeypatch.setattr(branch_resolver, "_checkout_branch", mock_checkout_branch)

    event = {
        "object_kind": "note",
        "object_attributes": {
            "noteable_type": "MergeRequest",
            "note": "LGTM",
        },
        "merge_request": {
            "source_branch": "feature/reviewed-branch",
            "target_branch": "main",
        },
    }

    result = await resolve_branch(
        event=event,
        project_path="group/project",
        working_dir=str(tmp_path),
        agent="claude",
    )

    assert result.success is True
    assert result.branch == "feature/reviewed-branch"
    assert result.switched is True


@pytest.mark.asyncio
async def test_resolve_branch_issue_event_no_mr_uses_default(monkeypatch, tmp_path):
    """Issue without linked MR should use default branch."""
    monkeypatch.setattr(settings, "enable_branch_switch", True)

    async def mock_get_current_branch(working_dir):
        return "feature/old-branch"

    async def mock_is_working_tree_clean(working_dir):
        return True

    async def mock_lookup_issue_branch(project_path, issue_iid):
        return None  # No linked MR

    async def mock_get_default_branch(working_dir):
        return "main"

    async def mock_checkout_branch(working_dir, branch, agent="unknown"):
        return BranchResult(success=True, branch=branch, switched=True)

    monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
    monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
    monkeypatch.setattr(branch_resolver, "_lookup_issue_branch", mock_lookup_issue_branch)
    monkeypatch.setattr(branch_resolver, "_get_default_branch", mock_get_default_branch)
    monkeypatch.setattr(branch_resolver, "_checkout_branch", mock_checkout_branch)

    event = {
        "object_kind": "issue",
        "object_attributes": {
            "iid": 42,
            "title": "New feature request",
        },
    }

    result = await resolve_branch(
        event=event,
        project_path="group/project",
        working_dir=str(tmp_path),
        agent="claude",
    )

    assert result.success is True
    assert result.branch == "main"
    assert result.switched is True


@pytest.mark.asyncio
async def test_resolve_branch_issue_with_linked_mr(monkeypatch, tmp_path):
    """Issue with linked MR should use MR's source branch."""
    monkeypatch.setattr(settings, "enable_branch_switch", True)

    async def mock_get_current_branch(working_dir):
        return "main"

    async def mock_is_working_tree_clean(working_dir):
        return True

    async def mock_lookup_issue_branch(project_path, issue_iid):
        return "feature/issue-42"  # Linked MR branch

    async def mock_checkout_branch(working_dir, branch, agent="unknown"):
        return BranchResult(success=True, branch=branch, switched=True)

    monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
    monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
    monkeypatch.setattr(branch_resolver, "_lookup_issue_branch", mock_lookup_issue_branch)
    monkeypatch.setattr(branch_resolver, "_checkout_branch", mock_checkout_branch)

    event = {
        "object_kind": "issue",
        "object_attributes": {
            "iid": 42,
            "title": "Feature with MR",
        },
    }

    result = await resolve_branch(
        event=event,
        project_path="group/project",
        working_dir=str(tmp_path),
        agent="claude",
    )

    assert result.success is True
    assert result.branch == "feature/issue-42"
    assert result.switched is True


@pytest.mark.asyncio
async def test_lookup_issue_branch_uses_glab_env(monkeypatch, tmp_path):
    """_lookup_issue_branch should use _get_glab_env for proper auth."""
    from app.services import glab

    called_env = {}

    async def mock_create_subprocess_exec(*args, **kwargs):
        called_env.update(kwargs.get("env", {}))
        mock_proc = type("MockProc", (), {
            "returncode": 0,
            "communicate": lambda self: (b"[]", b""),
        })()
        async def mock_communicate():
            return (b"[]", b"")
        mock_proc.communicate = mock_communicate
        return mock_proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)
    monkeypatch.setattr(glab.settings, "glab_token", "test-token")
    monkeypatch.setattr(glab.settings, "glab_host", "gitlab.example.com")

    from app.services.branch_resolver import _lookup_issue_branch
    await _lookup_issue_branch("group/project", 123)

    assert called_env.get("GITLAB_TOKEN") == "test-token"
    assert called_env.get("GITLAB_HOST") == "gitlab.example.com"


@pytest.mark.asyncio
async def test_lookup_issue_branch_timeout(monkeypatch):
    """_lookup_issue_branch should timeout and return None."""
    from app.services import branch_resolver
    from app.core.config import settings

    monkeypatch.setattr(settings, "glab_timeout_seconds", 0.01)

    async def mock_create_subprocess_exec(*args, **kwargs):
        mock_proc = type("MockProc", (), {
            "returncode": None,
            "kill": lambda self: None,
            "wait": lambda self: None,
        })()
        async def mock_communicate():
            import asyncio
            await asyncio.sleep(10)  # Will be cancelled by timeout
            return (b"[]", b"")
        async def mock_wait():
            pass
        mock_proc.communicate = mock_communicate
        mock_proc.wait = mock_wait
        return mock_proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

    result = await branch_resolver._lookup_issue_branch("group/project", 123)
    assert result is None


@pytest.mark.asyncio
async def test_backup_push_failure_is_fatal(monkeypatch, tmp_path):
    """Backup push failure should return failure, not continue."""
    monkeypatch.setattr(settings, "enable_branch_switch", True)

    async def mock_get_current_branch(working_dir):
        return "main"

    async def mock_is_working_tree_clean(working_dir):
        return False

    call_count = {"backup_created": False}

    async def mock_create_backup_branch(working_dir, current_branch, agent):
        call_count["backup_created"] = True
        # Simulate push failure - backup branch is created locally but push failed
        return BranchResult(
            success=False,
            backups=[BackupRecord(branch="backup/claude/main-20250128-120000", reason="uncommitted_changes")],
            error="Failed to push backup branch to origin: permission denied",
        )

    monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
    monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
    monkeypatch.setattr(branch_resolver, "_create_backup_branch", mock_create_backup_branch)

    event = {
        "object_kind": "merge_request",
        "object_attributes": {
            "source_branch": "feature/new-work",
        },
    }

    result = await resolve_branch(
        event=event,
        project_path="group/project",
        working_dir=str(tmp_path),
        agent="claude",
    )

    assert call_count["backup_created"] is True
    assert result.success is False
    assert "backup failed" in result.error.lower()


class TestGetBranchAheadCount:
    """Tests for _get_branch_ahead_count helper function."""

    @pytest.mark.asyncio
    async def test_returns_zero_when_not_ahead(self, monkeypatch):
        """Returns 0 when local branch is not ahead of origin."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {
                "returncode": 0,
            })()

            async def mock_communicate():
                # Format: "<behind>\t<ahead>" - not ahead
                return (b"0\t0", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _get_branch_ahead_count
        result = await _get_branch_ahead_count("/tmp/repo", "feature-branch")
        assert result == 0

    @pytest.mark.asyncio
    async def test_returns_count_when_ahead(self, monkeypatch):
        """Returns correct count when local branch is ahead of origin."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {
                "returncode": 0,
            })()

            async def mock_communicate():
                # Format: "<behind>\t<ahead>" - 3 commits ahead
                return (b"0\t3", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _get_branch_ahead_count
        result = await _get_branch_ahead_count("/tmp/repo", "feature-branch")
        assert result == 3

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self, monkeypatch):
        """Returns None when git command fails (unknown state)."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {
                "returncode": 1,
            })()

            async def mock_communicate():
                return (b"", b"fatal: invalid ref")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _get_branch_ahead_count
        result = await _get_branch_ahead_count("/tmp/repo", "nonexistent-branch")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self, monkeypatch):
        """Returns None when an exception occurs (unknown state)."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            raise OSError("Command not found")

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _get_branch_ahead_count
        result = await _get_branch_ahead_count("/tmp/repo", "feature-branch")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_unexpected_output(self, monkeypatch):
        """Returns None when git output has unexpected format."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {
                "returncode": 0,
            })()

            async def mock_communicate():
                # Unexpected output format (single value instead of two)
                return (b"invalid", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _get_branch_ahead_count
        result = await _get_branch_ahead_count("/tmp/repo", "feature-branch")
        assert result is None


class TestBackupLocalCommits:
    """Tests for _backup_local_commits helper function."""

    @pytest.mark.asyncio
    async def test_creates_backup_branch_for_local_commits(self, monkeypatch):
        """Successfully creates and pushes backup branch for local commits."""
        call_history = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            call_history.append(args)
            mock_proc = type("MockProc", (), {
                "returncode": 0,
            })()

            async def mock_communicate():
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _backup_local_commits
        result = await _backup_local_commits("/tmp/repo", "feature-branch", "claude")

        assert result.success is True
        assert result.backup_branch is not None
        assert "backup/claude/feature-branch-commits-" in result.backup_branch
        # Verify auth, branch creation, and push were called
        assert len(call_history) == 3
        assert call_history[0][0] == "glab-usr"  # authenticate
        assert call_history[1][1] == "branch"  # git branch
        assert call_history[2][1] == "push"  # git push

    @pytest.mark.asyncio
    async def test_fails_when_branch_creation_fails(self, monkeypatch):
        """Returns failure when creating backup branch fails."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {
                "returncode": 1,
            })()

            async def mock_communicate():
                return (b"", b"fatal: branch already exists")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _backup_local_commits
        result = await _backup_local_commits("/tmp/repo", "feature-branch", "claude")

        assert result.success is False
        assert "Failed to create backup branch" in result.error

    @pytest.mark.asyncio
    async def test_fails_when_push_fails(self, monkeypatch):
        """Returns failure when pushing backup branch fails."""
        call_count = {"calls": 0}

        async def mock_create_subprocess_exec(*args, **kwargs):
            call_count["calls"] += 1
            # call 1: auth (succeeds), call 2: branch (succeeds), call 3: push (fails)
            should_fail = call_count["calls"] == 3
            mock_proc = type("MockProc", (), {
                "returncode": 1 if should_fail else 0,
            })()

            async def mock_communicate():
                if should_fail:
                    return (b"", b"error: permission denied")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _backup_local_commits
        result = await _backup_local_commits("/tmp/repo", "feature-branch", "claude")

        assert result.success is False
        assert "Failed to push backup branch" in result.error
        assert result.backup_branch is not None  # Branch was created locally


class TestCheckoutBranchAheadHandling:
    """Tests for _checkout_branch handling of local commits ahead of origin."""

    @pytest.mark.asyncio
    async def test_skips_reset_when_not_ahead(self, monkeypatch):
        """Does not backup when local branch is not ahead of origin."""
        commands_executed = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            commands_executed.append(args)
            mock_proc = type("MockProc", (), {
                "returncode": 0,
            })()

            async def mock_communicate():
                # Return "0\t0" for rev-list --left-right --count
                if "rev-list" in args:
                    return (b"0\t0", b"")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _checkout_branch
        result = await _checkout_branch("/tmp/repo", "feature-branch", "claude")

        assert result.success is True
        # Verify backup branch was not created (no "git branch backup/..." call)
        branch_calls = [c for c in commands_executed if c[1] == "branch"]
        assert len(branch_calls) == 0

    @pytest.mark.asyncio
    async def test_creates_backup_when_ahead(self, monkeypatch):
        """Creates backup branch when local is ahead of origin and returns it."""
        commands_executed = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            commands_executed.append(args)
            mock_proc = type("MockProc", (), {
                "returncode": 0,
            })()

            async def mock_communicate():
                # Return "0\t2" for rev-list --left-right --count (2 commits ahead)
                if "rev-list" in args:
                    return (b"0\t2", b"")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _checkout_branch
        result = await _checkout_branch("/tmp/repo", "feature-branch", "claude")

        assert result.success is True
        # Verify backup branch was created
        branch_calls = [c for c in commands_executed if c[1] == "branch"]
        assert len(branch_calls) == 1
        assert "backup/claude/feature-branch-commits-" in branch_calls[0][2]
        # Verify backup_branch is returned in result
        assert result.backup_branch is not None
        assert "backup/claude/feature-branch-commits-" in result.backup_branch

    @pytest.mark.asyncio
    async def test_fails_when_backup_push_fails_on_ahead(self, monkeypatch):
        """Fails checkout if backup push fails when local is ahead."""
        call_sequence = {"index": 0}
        # Sequence: glab-usr, fetch, checkout, rev-list, glab-usr, branch, push
        # Index:    0         1      2         3         4         5       6

        async def mock_create_subprocess_exec(*args, **kwargs):
            idx = call_sequence["index"]
            call_sequence["index"] += 1

            # Push (index 6) should fail
            should_fail = idx == 6

            mock_proc = type("MockProc", (), {
                "returncode": 1 if should_fail else 0,
            })()

            async def mock_communicate():
                if should_fail:
                    return (b"", b"error: permission denied")
                # Return "0\t1" for rev-list (1 commit ahead)
                if "rev-list" in args:
                    return (b"0\t1", b"")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _checkout_branch
        result = await _checkout_branch("/tmp/repo", "feature-branch", "claude")

        assert result.success is False
        assert "backup of local commits failed" in result.error.lower()

    @pytest.mark.asyncio
    async def test_skips_reset_when_fetch_fails(self, monkeypatch):
        """Skips hard reset when fetch from origin fails."""
        commands_executed = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            commands_executed.append(args)
            mock_proc = type("MockProc", (), {
                # Fetch fails, checkout succeeds
                "returncode": 1 if "fetch" in args else 0,
            })()

            async def mock_communicate():
                if "fetch" in args:
                    return (b"", b"fatal: couldn't find remote ref")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _checkout_branch
        result = await _checkout_branch("/tmp/repo", "local-only-branch", "claude")

        assert result.success is True
        # Verify no reset was attempted (no "git reset" call)
        reset_calls = [c for c in commands_executed if c[1] == "reset"]
        assert len(reset_calls) == 0

    @pytest.mark.asyncio
    async def test_creates_backup_when_ahead_check_fails(self, monkeypatch):
        """Creates backup branch when ahead check fails (unknown state)."""
        commands_executed = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            commands_executed.append(args)
            mock_proc = type("MockProc", (), {
                # rev-list command fails, all others succeed
                "returncode": 1 if "rev-list" in args else 0,
            })()

            async def mock_communicate():
                if "rev-list" in args:
                    return (b"", b"fatal: ambiguous argument")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _checkout_branch
        result = await _checkout_branch("/tmp/repo", "feature-branch", "claude")

        assert result.success is True
        # Verify backup branch was created due to unknown ahead state
        branch_calls = [c for c in commands_executed if c[1] == "branch"]
        assert len(branch_calls) == 1
        assert "backup/claude/feature-branch-commits-" in branch_calls[0][2]
        # Verify backup_branch is returned in result
        assert result.backup_branch is not None
        assert "backup/claude/feature-branch-commits-" in result.backup_branch

    @pytest.mark.asyncio
    async def test_fails_when_backup_push_fails_on_unknown_ahead(self, monkeypatch):
        """Fails checkout if backup push fails when ahead check fails (unknown state)."""
        call_sequence = {"index": 0}
        # Expected: fetch, checkout, rev-list (fails), branch (backup), push (fails)
        expected_sequence = ["fetch", "checkout", "rev-list", "branch", "push"]

        async def mock_create_subprocess_exec(*args, **kwargs):
            idx = call_sequence["index"]
            call_sequence["index"] += 1

            # rev-list (index 2) fails, push (index 4) fails
            if "rev-list" in args:
                should_fail = True
            elif "push" in args:
                should_fail = True
            else:
                should_fail = False

            mock_proc = type("MockProc", (), {
                "returncode": 1 if should_fail else 0,
            })()

            async def mock_communicate():
                if "rev-list" in args:
                    return (b"", b"fatal: ambiguous argument")
                if "push" in args:
                    return (b"", b"error: permission denied")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _checkout_branch
        result = await _checkout_branch("/tmp/repo", "feature-branch", "claude")

        assert result.success is False
        assert "backup of local commits failed" in result.error.lower()


class TestSyncCurrentBranch:
    """Tests for _sync_current_branch helper function."""

    @pytest.mark.asyncio
    async def test_sync_when_behind_origin(self, monkeypatch):
        """Resets to origin when local branch is behind."""
        commands_executed = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            commands_executed.append(args)
            mock_proc = type("MockProc", (), {
                "returncode": 0,
            })()

            async def mock_communicate():
                if "rev-list" in args:
                    # 2 behind, 0 ahead
                    return (b"2\t0", b"")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _sync_current_branch
        result = await _sync_current_branch("/tmp/repo", "main", "claude")

        assert result.success is True
        # Verify reset was called
        reset_calls = [c for c in commands_executed if "reset" in c]
        assert len(reset_calls) == 1
        assert "origin/main" in reset_calls[0]

    @pytest.mark.asyncio
    async def test_sync_when_already_in_sync(self, monkeypatch):
        """Does nothing when branch is already in sync with origin."""
        commands_executed = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            commands_executed.append(args)
            mock_proc = type("MockProc", (), {
                "returncode": 0,
            })()

            async def mock_communicate():
                if "rev-list" in args:
                    # 0 behind, 0 ahead - in sync
                    return (b"0\t0", b"")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _sync_current_branch
        result = await _sync_current_branch("/tmp/repo", "main", "claude")

        assert result.success is True
        # Verify no reset was called
        reset_calls = [c for c in commands_executed if "reset" in c]
        assert len(reset_calls) == 0

    @pytest.mark.asyncio
    async def test_sync_when_ahead_creates_backup(self, monkeypatch):
        """Creates backup when local has unpushed commits before reset."""
        commands_executed = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            commands_executed.append(args)
            mock_proc = type("MockProc", (), {
                "returncode": 0,
            })()

            async def mock_communicate():
                if "rev-list" in args:
                    # 1 behind, 2 ahead - diverged
                    return (b"1\t2", b"")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _sync_current_branch
        result = await _sync_current_branch("/tmp/repo", "main", "claude")

        assert result.success is True
        assert result.backup_branch is not None
        assert "backup/claude/main-commits-" in result.backup_branch

    @pytest.mark.asyncio
    async def test_sync_continues_when_fetch_fails(self, monkeypatch):
        """Returns success with error message when fetch fails."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {
                "returncode": 1 if "fetch" in args else 0,
            })()

            async def mock_communicate():
                if "fetch" in args:
                    return (b"", b"fatal: unable to access")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _sync_current_branch
        result = await _sync_current_branch("/tmp/repo", "main", "claude")

        assert result.success is True
        assert "Fetch failed" in result.error

    @pytest.mark.asyncio
    async def test_sync_skips_when_divergence_unknown(self, monkeypatch):
        """Skips sync when divergence check fails."""
        commands_executed = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            commands_executed.append(args)
            mock_proc = type("MockProc", (), {
                "returncode": 1 if "rev-list" in args else 0,
            })()

            async def mock_communicate():
                if "rev-list" in args:
                    return (b"", b"fatal: ambiguous argument")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        from app.services.branch_resolver import _sync_current_branch
        result = await _sync_current_branch("/tmp/repo", "main", "claude")

        assert result.success is True
        # No reset should be attempted
        reset_calls = [c for c in commands_executed if "reset" in c]
        assert len(reset_calls) == 0


class TestResolveBranchSyncsCurrentBranch:
    """Tests that resolve_branch syncs when already on target branch."""

    @pytest.mark.asyncio
    async def test_syncs_when_already_on_target_and_behind(self, monkeypatch, tmp_path):
        """When already on target branch, fetches and resets if behind."""
        monkeypatch.setattr(settings, "enable_branch_switch", True)

        # Create a git repo
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/feature-branch\n")
        (git_dir / "refs" / "heads").mkdir(parents=True)
        (git_dir / "refs" / "heads" / "feature-branch").write_text("abc123\n")

        commands_executed = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            commands_executed.append(args)
            mock_proc = type("MockProc", (), {
                "returncode": 0,
            })()

            async def mock_communicate():
                if "rev-parse" in args and "--abbrev-ref" in args:
                    return (b"feature-branch", b"")
                if "rev-list" in args:
                    # 3 behind, 0 ahead
                    return (b"3\t0", b"")
                if "status" in args and "--porcelain" in args:
                    return (b"", b"")  # clean
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        event = {
            "object_kind": "merge_request",
            "object_attributes": {
                "source_branch": "feature-branch",
            },
        }

        result = await resolve_branch(
            event=event,
            project_path="group/project",
            working_dir=str(tmp_path),
            agent="claude",
        )

        assert result.success is True
        # Verify fetch and reset were called
        fetch_calls = [c for c in commands_executed if "fetch" in c]
        reset_calls = [c for c in commands_executed if "reset" in c]
        assert len(fetch_calls) >= 1
        assert len(reset_calls) == 1


class TestSmartBranchSelection:
    """Tests for the smart branch selection algorithm."""

    @pytest.mark.asyncio
    async def test_single_candidate_skips_closes_api(self, monkeypatch):
        """With only one open MR, returns it directly without closes_issues call."""
        monkeypatch.setattr(settings, "enable_smart_branch_selection", True)

        api_calls = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            api_calls.append(args)
            mock_proc = type("MockProc", (), {"returncode": 0})()

            async def mock_communicate():
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        open_mrs = [
            {"iid": 10, "state": "opened", "source_branch": "fix/issue-5", "updated_at": "2026-01-01T00:00:00Z"},
        ]

        result = await _smart_select_branch(
            project_path="group/project",
            encoded_path="group%2Fproject",
            issue_iid=5,
            open_mrs=open_mrs,
        )

        assert result == "fix/issue-5"
        # No closes_issues API calls should have been made
        assert len(api_calls) == 0

    @pytest.mark.asyncio
    async def test_prefers_mr_that_explicitly_closes_issue(self, monkeypatch):
        """When multiple MRs exist, prefers the one that explicitly closes the issue."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()
            api_path = args[2] if len(args) > 2 else ""

            async def mock_communicate():
                if "merge_requests/20/closes_issues" in api_path:
                    # MR 20 closes issue 5
                    return (b'[{"iid": 5}]', b"")
                elif "merge_requests/10/closes_issues" in api_path:
                    # MR 10 does not close issue 5
                    return (b'[{"iid": 99}]', b"")
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        open_mrs = [
            {"iid": 10, "state": "opened", "source_branch": "feature/unrelated", "updated_at": "2026-02-01T00:00:00Z"},
            {"iid": 20, "state": "opened", "source_branch": "fix/issue-5", "updated_at": "2026-01-01T00:00:00Z"},
        ]

        result = await _smart_select_branch(
            project_path="group/project",
            encoded_path="group%2Fproject",
            issue_iid=5,
            open_mrs=open_mrs,
        )

        assert result == "fix/issue-5"

    @pytest.mark.asyncio
    async def test_note_mention_overrides_closes_issue(self, monkeypatch):
        """Note mention (!<iid>) takes priority over closes_issues signal."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()
            api_path = args[2] if len(args) > 2 else ""

            async def mock_communicate():
                if "merge_requests/20/closes_issues" in api_path:
                    return (b'[{"iid": 5}]', b"")
                elif "merge_requests/10/closes_issues" in api_path:
                    return (b"[]", b"")
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        open_mrs = [
            {"iid": 10, "state": "opened", "source_branch": "feature/mentioned", "updated_at": "2026-01-01T00:00:00Z"},
            {"iid": 20, "state": "opened", "source_branch": "fix/closes-issue", "updated_at": "2026-02-01T00:00:00Z"},
        ]

        # Note body explicitly mentions MR !10
        result = await _smart_select_branch(
            project_path="group/project",
            encoded_path="group%2Fproject",
            issue_iid=5,
            open_mrs=open_mrs,
            note_body="Please work on !10 instead",
        )

        assert result == "feature/mentioned"

    @pytest.mark.asyncio
    async def test_falls_back_to_recency_when_no_close_matches(self, monkeypatch):
        """Without close matches, selects most recently updated MR."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()

            async def mock_communicate():
                # No MR closes the issue
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        open_mrs = [
            {"iid": 10, "state": "opened", "source_branch": "feature/old", "updated_at": "2026-01-01T00:00:00Z"},
            {"iid": 20, "state": "opened", "source_branch": "feature/recent", "updated_at": "2026-02-01T00:00:00Z"},
        ]

        result = await _smart_select_branch(
            project_path="group/project",
            encoded_path="group%2Fproject",
            issue_iid=5,
            open_mrs=open_mrs,
        )

        assert result == "feature/recent"

    @pytest.mark.asyncio
    async def test_iid_tiebreaker_when_same_updated_at(self, monkeypatch):
        """When updated_at is identical, highest iid wins."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()

            async def mock_communicate():
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        open_mrs = [
            {"iid": 10, "state": "opened", "source_branch": "feature/older-iid", "updated_at": "2026-01-15T00:00:00Z"},
            {"iid": 30, "state": "opened", "source_branch": "feature/higher-iid", "updated_at": "2026-01-15T00:00:00Z"},
        ]

        result = await _smart_select_branch(
            project_path="group/project",
            encoded_path="group%2Fproject",
            issue_iid=5,
            open_mrs=open_mrs,
        )

        assert result == "feature/higher-iid"

    @pytest.mark.asyncio
    async def test_api_failure_treated_as_unknown(self, monkeypatch):
        """If closes_issues API fails, result is unknown (not penalized)."""

        call_count = {"n": 0}

        async def mock_create_subprocess_exec(*args, **kwargs):
            call_count["n"] += 1
            api_path = args[2] if len(args) > 2 else ""
            mock_proc = type("MockProc", (), {
                # MR 10's API call fails
                "returncode": 1 if "merge_requests/10" in api_path else 0,
            })()

            async def mock_communicate():
                if "merge_requests/10" in api_path:
                    return (b"", b"500 Internal Server Error")
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        open_mrs = [
            {"iid": 10, "state": "opened", "source_branch": "feature/api-error", "updated_at": "2026-02-01T00:00:00Z"},
            {"iid": 20, "state": "opened", "source_branch": "feature/ok", "updated_at": "2026-01-01T00:00:00Z"},
        ]

        result = await _smart_select_branch(
            project_path="group/project",
            encoded_path="group%2Fproject",
            issue_iid=5,
            open_mrs=open_mrs,
        )

        # MR 10 is more recent, API failure should not penalize it
        assert result == "feature/api-error"

    @pytest.mark.asyncio
    async def test_all_api_failures_uses_recency_fallback(self, monkeypatch):
        """If all closes_issues calls fail, uses recency-based fallback."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 1})()

            async def mock_communicate():
                return (b"", b"API error")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        open_mrs = [
            {"iid": 10, "state": "opened", "source_branch": "feature/a", "updated_at": "2026-01-01T00:00:00Z"},
            {"iid": 20, "state": "opened", "source_branch": "feature/b", "updated_at": "2026-02-01T00:00:00Z"},
        ]

        result = await _smart_select_branch(
            project_path="group/project",
            encoded_path="group%2Fproject",
            issue_iid=5,
            open_mrs=open_mrs,
        )

        # No close matches, all API calls failed -> fallback to most recent
        assert result == "feature/b"

    @pytest.mark.asyncio
    async def test_truncates_candidates_at_max(self, monkeypatch):
        """Candidates are truncated at _SMART_BRANCH_MAX_CANDIDATES."""
        from app.services.branch_resolver import _SMART_BRANCH_MAX_CANDIDATES

        api_call_count = {"n": 0}

        async def mock_create_subprocess_exec(*args, **kwargs):
            api_call_count["n"] += 1
            mock_proc = type("MockProc", (), {"returncode": 0})()

            async def mock_communicate():
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        # Create more MRs than the max
        open_mrs = [
            {
                "iid": i,
                "state": "opened",
                "source_branch": f"feature/mr-{i}",
                "updated_at": f"2026-01-{i:02d}T00:00:00Z",
            }
            for i in range(1, _SMART_BRANCH_MAX_CANDIDATES + 10)
        ]

        result = await _smart_select_branch(
            project_path="group/project",
            encoded_path="group%2Fproject",
            issue_iid=5,
            open_mrs=open_mrs,
        )

        # Should select the most recently updated (highest iid given sequential dates)
        assert result is not None
        # Verify API calls are capped at _SMART_BRANCH_MAX_CANDIDATES
        assert api_call_count["n"] == _SMART_BRANCH_MAX_CANDIDATES

    @pytest.mark.asyncio
    async def test_does_not_mutate_input_list(self, monkeypatch):
        """_smart_select_branch should not mutate the caller's open_mrs list."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()

            async def mock_communicate():
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        open_mrs = [
            {"iid": 10, "state": "opened", "source_branch": "feature/a", "updated_at": "2026-01-01T00:00:00Z"},
            {"iid": 30, "state": "opened", "source_branch": "feature/c", "updated_at": "2026-01-15T00:00:00Z"},
            {"iid": 20, "state": "opened", "source_branch": "feature/b", "updated_at": "2026-01-10T00:00:00Z"},
        ]
        original_order = [mr["iid"] for mr in open_mrs]

        await _smart_select_branch(
            project_path="group/project",
            encoded_path="group%2Fproject",
            issue_iid=5,
            open_mrs=open_mrs,
        )

        # Verify the input list was not reordered
        assert [mr["iid"] for mr in open_mrs] == original_order

    @pytest.mark.asyncio
    async def test_note_mention_parsing_ignores_inline_exclamations(self, monkeypatch):
        """!<iid> in the middle of a word should not match."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()

            async def mock_communicate():
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        open_mrs = [
            {"iid": 10, "state": "opened", "source_branch": "feature/a", "updated_at": "2026-01-01T00:00:00Z"},
            {"iid": 20, "state": "opened", "source_branch": "feature/b", "updated_at": "2026-02-01T00:00:00Z"},
        ]

        # "abc!10def" should not match MR !10
        result = await _smart_select_branch(
            project_path="group/project",
            encoded_path="group%2Fproject",
            issue_iid=5,
            open_mrs=open_mrs,
            note_body="Something abc!10def not a real mention",
        )

        # Should fall back to recency (MR 20)
        assert result == "feature/b"


class TestLegacyBranchSelection:
    """Tests for legacy (non-smart) branch selection mode."""

    @pytest.mark.asyncio
    async def test_legacy_mode_returns_first_open_mr(self, monkeypatch):
        """With smart selection disabled, returns first open MR."""
        monkeypatch.setattr(settings, "enable_smart_branch_selection", False)

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()

            async def mock_communicate():
                mrs = [
                    {"iid": 10, "state": "opened", "source_branch": "feature/first"},
                    {"iid": 20, "state": "opened", "source_branch": "feature/second"},
                ]
                return (json.dumps(mrs).encode(), b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        result = await _lookup_issue_branch("group/project", 5)
        assert result == "feature/first"

    @pytest.mark.asyncio
    async def test_legacy_mode_skips_closed_mrs(self, monkeypatch):
        """Legacy mode still filters out closed/merged MRs."""
        monkeypatch.setattr(settings, "enable_smart_branch_selection", False)

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()

            async def mock_communicate():
                mrs = [
                    {"iid": 10, "state": "merged", "source_branch": "feature/merged"},
                    {"iid": 20, "state": "opened", "source_branch": "feature/open"},
                ]
                return (json.dumps(mrs).encode(), b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        result = await _lookup_issue_branch("group/project", 5)
        assert result == "feature/open"

    @pytest.mark.asyncio
    async def test_legacy_mode_returns_none_when_no_open_mrs(self, monkeypatch):
        """Legacy mode returns None when all MRs are closed."""
        monkeypatch.setattr(settings, "enable_smart_branch_selection", False)

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()

            async def mock_communicate():
                mrs = [
                    {"iid": 10, "state": "merged", "source_branch": "feature/merged"},
                ]
                return (json.dumps(mrs).encode(), b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        result = await _lookup_issue_branch("group/project", 5)
        assert result is None


class TestFetchClosesIssuesBatch:
    """Tests for _fetch_closes_issues_batch helper."""

    @pytest.mark.asyncio
    async def test_returns_true_when_issue_in_closes_list(self, monkeypatch):
        """Returns True for MRs whose closes_issues contains the target issue."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()

            async def mock_communicate():
                return (b'[{"iid": 5}, {"iid": 10}]', b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        candidates = [{"iid": 100}]
        result = await _fetch_closes_issues_batch(
            encoded_path="group%2Fproject",
            candidates=candidates,
            issue_iid=5,
        )

        assert result[100] is True

    @pytest.mark.asyncio
    async def test_returns_false_when_issue_not_in_closes_list(self, monkeypatch):
        """Returns False for MRs whose closes_issues does not contain the target."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()

            async def mock_communicate():
                return (b'[{"iid": 99}]', b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        candidates = [{"iid": 100}]
        result = await _fetch_closes_issues_batch(
            encoded_path="group%2Fproject",
            candidates=candidates,
            issue_iid=5,
        )

        assert result[100] is False

    @pytest.mark.asyncio
    async def test_returns_none_on_api_failure(self, monkeypatch):
        """Returns None for MRs where API call fails."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 1})()

            async def mock_communicate():
                return (b"", b"500 error")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        candidates = [{"iid": 100}]
        result = await _fetch_closes_issues_batch(
            encoded_path="group%2Fproject",
            candidates=candidates,
            issue_iid=5,
        )

        assert result[100] is None

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self, monkeypatch):
        """Returns None for MRs where API call times out."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 0.01)

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {
                "returncode": None,
                "kill": lambda self: None,
            })()

            async def mock_communicate():
                import asyncio as _asyncio
                await _asyncio.sleep(10)
                return (b"[]", b"")

            async def mock_wait():
                pass

            mock_proc.communicate = mock_communicate
            mock_proc.wait = mock_wait
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        candidates = [{"iid": 100}]
        result = await _fetch_closes_issues_batch(
            encoded_path="group%2Fproject",
            candidates=candidates,
            issue_iid=5,
        )

        assert result[100] is None

    @pytest.mark.asyncio
    async def test_handles_multiple_candidates_concurrently(self, monkeypatch):
        """Handles multiple candidates in parallel."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()
            api_path = args[2] if len(args) > 2 else ""

            async def mock_communicate():
                if "merge_requests/100" in api_path:
                    return (b'[{"iid": 5}]', b"")  # closes issue 5
                elif "merge_requests/200" in api_path:
                    return (b'[{"iid": 99}]', b"")  # does not close issue 5
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        candidates = [{"iid": 100}, {"iid": 200}]
        result = await _fetch_closes_issues_batch(
            encoded_path="group%2Fproject",
            candidates=candidates,
            issue_iid=5,
        )

        assert result[100] is True
        assert result[200] is False

    @pytest.mark.asyncio
    async def test_skips_candidates_without_valid_iid(self, monkeypatch):
        """Candidates missing an iid key are skipped (no API call made)."""
        api_calls = []

        async def mock_create_subprocess_exec(*args, **kwargs):
            api_calls.append(args)
            mock_proc = type("MockProc", (), {"returncode": 0})()

            async def mock_communicate():
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        candidates = [
            {"iid": 100},
            {},                # missing iid entirely
            {"iid": None},     # iid is None (falsy)
            {"iid": 0},        # iid is 0 (falsy)
            {"iid": 200},
        ]
        result = await _fetch_closes_issues_batch(
            encoded_path="group%2Fproject",
            candidates=candidates,
            issue_iid=5,
        )

        # Only 2 API calls should be made (for iid 100 and 200)
        assert len(api_calls) == 2
        assert 100 in result
        assert 200 in result
        # Falsy iids should not appear in results
        assert 0 not in result
        assert None not in result


class TestSmartBranchSelectionIntegration:
    """Integration tests for smart branch selection through the full lookup path."""

    @pytest.mark.asyncio
    async def test_note_event_threads_note_body(self, monkeypatch, tmp_path):
        """Note events pass note body to smart selection for mention parsing."""
        monkeypatch.setattr(settings, "enable_branch_switch", True)
        monkeypatch.setattr(settings, "enable_smart_branch_selection", True)

        captured_note_body = {}

        original_lookup = _lookup_issue_branch

        async def mock_lookup(project_path, issue_iid, note_body=""):
            captured_note_body["value"] = note_body
            return "feature/from-note"

        async def mock_get_current_branch(working_dir):
            return "main"

        async def mock_is_working_tree_clean(working_dir):
            return True

        async def mock_checkout_branch(working_dir, branch, agent="unknown"):
            return BranchResult(success=True, branch=branch, switched=True)

        monkeypatch.setattr(branch_resolver, "_lookup_issue_branch", mock_lookup)
        monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
        monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
        monkeypatch.setattr(branch_resolver, "_checkout_branch", mock_checkout_branch)

        event = {
            "object_kind": "note",
            "object_attributes": {
                "noteable_type": "Issue",
                "note": "Please work on !42 for this",
            },
            "issue": {
                "iid": 5,
            },
        }

        result = await resolve_branch(
            event=event,
            project_path="group/project",
            working_dir=str(tmp_path),
            agent="claude",
        )

        assert result.success is True
        assert result.branch == "feature/from-note"
        assert captured_note_body["value"] == "Please work on !42 for this"

    @pytest.mark.asyncio
    async def test_issue_event_does_not_pass_note_body(self, monkeypatch, tmp_path):
        """Issue events pass empty note_body (no note content available)."""
        monkeypatch.setattr(settings, "enable_branch_switch", True)
        monkeypatch.setattr(settings, "enable_smart_branch_selection", True)

        captured_note_body = {}

        async def mock_lookup(project_path, issue_iid, note_body=""):
            captured_note_body["value"] = note_body
            return "feature/from-issue"

        async def mock_get_current_branch(working_dir):
            return "main"

        async def mock_is_working_tree_clean(working_dir):
            return True

        async def mock_checkout_branch(working_dir, branch, agent="unknown"):
            return BranchResult(success=True, branch=branch, switched=True)

        monkeypatch.setattr(branch_resolver, "_lookup_issue_branch", mock_lookup)
        monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
        monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
        monkeypatch.setattr(branch_resolver, "_checkout_branch", mock_checkout_branch)

        event = {
            "object_kind": "issue",
            "object_attributes": {
                "iid": 5,
                "title": "Some issue",
            },
        }

        result = await resolve_branch(
            event=event,
            project_path="group/project",
            working_dir=str(tmp_path),
            agent="claude",
        )

        assert result.success is True
        assert result.branch == "feature/from-issue"
        assert captured_note_body["value"] == ""

    @pytest.mark.asyncio
    async def test_multiple_closing_mrs_uses_recency(self, monkeypatch):
        """When multiple MRs close the issue, recency breaks the tie."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()
            api_path = args[2] if len(args) > 2 else ""

            async def mock_communicate():
                if "closes_issues" in api_path:
                    # Both MRs close issue 5
                    return (b'[{"iid": 5}]', b"")
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        open_mrs = [
            {"iid": 10, "state": "opened", "source_branch": "fix/attempt-1", "updated_at": "2026-01-01T00:00:00Z"},
            {"iid": 20, "state": "opened", "source_branch": "fix/attempt-2", "updated_at": "2026-02-01T00:00:00Z"},
        ]

        result = await _smart_select_branch(
            project_path="group/project",
            encoded_path="group%2Fproject",
            issue_iid=5,
            open_mrs=open_mrs,
        )

        # MR 20 is more recent among the closing candidates
        assert result == "fix/attempt-2"

    @pytest.mark.asyncio
    async def test_mention_only_mr_filtered_when_closing_mr_exists(self, monkeypatch):
        """MR linked via casual mention is filtered out when a closing MR exists."""

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()
            api_path = args[2] if len(args) > 2 else ""

            async def mock_communicate():
                if "merge_requests/10/closes_issues" in api_path:
                    # MR 10 was just mentioned, does not close the issue
                    return (b"[]", b"")
                elif "merge_requests/20/closes_issues" in api_path:
                    # MR 20 explicitly closes the issue
                    return (b'[{"iid": 5}]', b"")
                return (b"[]", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        open_mrs = [
            {"iid": 10, "state": "opened", "source_branch": "feature/mentioned-only", "updated_at": "2026-02-01T00:00:00Z"},
            {"iid": 20, "state": "opened", "source_branch": "fix/proper-fix", "updated_at": "2026-01-15T00:00:00Z"},
        ]

        result = await _smart_select_branch(
            project_path="group/project",
            encoded_path="group%2Fproject",
            issue_iid=5,
            open_mrs=open_mrs,
        )

        # Should select MR 20 (explicitly closes) despite MR 10 being more recent
        assert result == "fix/proper-fix"

    @pytest.mark.asyncio
    async def test_end_to_end_note_mention_steers_branch(self, monkeypatch, tmp_path):
        """Full path: resolve_branch -> note event -> smart selection with note mention.

        Exercises the complete call chain with subprocess mocks to verify
        note_body is correctly threaded from the webhook payload through to
        the smart selection ranking.
        """
        monkeypatch.setattr(settings, "enable_branch_switch", True)
        monkeypatch.setattr(settings, "enable_smart_branch_selection", True)

        async def mock_create_subprocess_exec(*args, **kwargs):
            mock_proc = type("MockProc", (), {"returncode": 0})()
            api_path = args[2] if len(args) > 2 else ""

            async def mock_communicate():
                # related_merge_requests API - return two open MRs
                if "related_merge_requests" in api_path:
                    mrs = [
                        {"iid": 10, "state": "opened", "source_branch": "feature/mentioned",
                         "updated_at": "2026-01-01T00:00:00Z"},
                        {"iid": 20, "state": "opened", "source_branch": "fix/closing",
                         "updated_at": "2026-02-01T00:00:00Z"},
                    ]
                    return (json.dumps(mrs).encode(), b"")
                # closes_issues for MR 20 - it closes the issue
                if "merge_requests/20/closes_issues" in api_path:
                    return (b'[{"iid": 7}]', b"")
                # closes_issues for MR 10 - does not close
                if "merge_requests/10/closes_issues" in api_path:
                    return (b"[]", b"")
                # git rev-parse for current branch
                if "rev-parse" in args:
                    return (b"main", b"")
                # git status --porcelain (clean)
                if "status" in args:
                    return (b"", b"")
                return (b"", b"")

            mock_proc.communicate = mock_communicate
            return mock_proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

        async def mock_get_current_branch(working_dir):
            return "main"

        async def mock_is_working_tree_clean(working_dir):
            return True

        async def mock_checkout_branch(working_dir, branch, agent="unknown"):
            return BranchResult(success=True, branch=branch, switched=True)

        monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
        monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
        monkeypatch.setattr(branch_resolver, "_checkout_branch", mock_checkout_branch)

        event = {
            "object_kind": "note",
            "object_attributes": {
                "noteable_type": "Issue",
                "note": "Please switch to !10 for this work",
            },
            "issue": {
                "iid": 7,
            },
        }

        result = await resolve_branch(
            event=event,
            project_path="group/project",
            working_dir=str(tmp_path),
            agent="claude",
        )

        assert result.success is True
        # Note mention of !10 should override the closing MR 20
        assert result.branch == "feature/mentioned"


@pytest.mark.asyncio
async def test_sync_dual_backup_both_uncommitted_and_ahead(monkeypatch):
    """When _sync_current_branch encounters both dirty tree AND local commits
    ahead of origin, both backup branches should appear in the result."""
    monkeypatch.setattr(settings, "enable_branch_switch", True)

    async def mock_authenticate_git(agent):
        return True

    async def mock_is_working_tree_clean(working_dir):
        return False

    backup_call_count = {"uncommitted": 0, "commits": 0}

    async def mock_create_backup_branch(working_dir, current_branch, agent):
        backup_call_count["uncommitted"] += 1
        return BranchResult(
            success=True,
            backups=[BackupRecord(branch=f"backup/{agent}/{current_branch}-20250128-120000", reason="uncommitted_changes")],
        )

    async def mock_backup_local_commits(working_dir, branch, agent):
        backup_call_count["commits"] += 1
        return BranchResult(
            success=True,
            backups=[BackupRecord(branch=f"backup/{agent}/{branch}-commits-20250128-120000", reason="local_commits")],
        )

    async def mock_get_branch_divergence(working_dir, branch):
        return (2, 3)  # behind 2, ahead 3

    # Mock fetch and reset to succeed
    async def mock_create_subprocess_exec(*args, **kwargs):
        class FakeProc:
            returncode = 0
            async def communicate(self):
                return (b"", b"")
        return FakeProc()

    monkeypatch.setattr(branch_resolver, "_authenticate_git", mock_authenticate_git)
    monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
    monkeypatch.setattr(branch_resolver, "_create_backup_branch", mock_create_backup_branch)
    monkeypatch.setattr(branch_resolver, "_backup_local_commits", mock_backup_local_commits)
    monkeypatch.setattr(branch_resolver, "_get_branch_divergence", mock_get_branch_divergence)
    monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create_subprocess_exec)

    from app.services.branch_resolver import _sync_current_branch
    result = await _sync_current_branch("/tmp/repo", "main", "claude")

    assert result.success is True
    assert len(result.backups) == 2
    assert result.backups[0].branch == "backup/claude/main-20250128-120000"
    assert result.backups[0].reason == "uncommitted_changes"
    assert result.backups[1].branch == "backup/claude/main-commits-20250128-120000"
    assert result.backups[1].reason == "local_commits"
    assert backup_call_count["uncommitted"] == 1
    assert backup_call_count["commits"] == 1


@pytest.mark.asyncio
async def test_resolve_branch_dirty_tree_plus_checkout_ahead(monkeypatch, tmp_path):
    """When resolve_branch backs up a dirty tree AND _checkout_branch backs up
    local commits on the target branch, both backups appear in the result."""
    monkeypatch.setattr(settings, "enable_branch_switch", True)

    async def mock_get_current_branch(working_dir):
        return "old-branch"

    async def mock_is_working_tree_clean(working_dir):
        return False

    async def mock_create_backup_branch(working_dir, current_branch, agent):
        return BranchResult(
            success=True,
            backups=[BackupRecord(branch=f"backup/{agent}/{current_branch}-20250128-120000", reason="uncommitted_changes")],
        )

    async def mock_checkout_branch(working_dir, branch, agent="unknown"):
        # Simulate checkout that also created a backup for local commits ahead of origin
        return BranchResult(
            success=True,
            branch=branch,
            switched=True,
            backups=[BackupRecord(branch=f"backup/{agent}/{branch}-commits-20250128-120000", reason="local_commits")],
        )

    monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
    monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
    monkeypatch.setattr(branch_resolver, "_create_backup_branch", mock_create_backup_branch)
    monkeypatch.setattr(branch_resolver, "_checkout_branch", mock_checkout_branch)

    event = {
        "object_kind": "merge_request",
        "object_attributes": {
            "source_branch": "feature/target",
            "target_branch": "main",
        },
    }

    result = await resolve_branch(
        event=event,
        project_path="group/project",
        working_dir=str(tmp_path),
        agent="claude",
    )

    assert result.success is True
    assert result.branch == "feature/target"
    assert result.switched is True
    assert len(result.backups) == 2
    assert result.backups[0].branch == "backup/claude/old-branch-20250128-120000"
    assert result.backups[0].reason == "uncommitted_changes"
    assert result.backups[1].branch == "backup/claude/feature/target-commits-20250128-120000"
    assert result.backups[1].reason == "local_commits"


@pytest.mark.asyncio
async def test_resolve_branch_dirty_backup_preserved_on_checkout_failure(monkeypatch, tmp_path):
    """When a dirty-tree backup succeeds but _checkout_branch fails, the
    backup record must still be present on the returned (failed) result so
    that the notification layer can inform the user."""
    monkeypatch.setattr(settings, "enable_branch_switch", True)

    async def mock_get_current_branch(working_dir):
        return "old-branch"

    async def mock_is_working_tree_clean(working_dir):
        return False

    async def mock_create_backup_branch(working_dir, current_branch, agent):
        return BranchResult(
            success=True,
            backups=[BackupRecord(branch=f"backup/{agent}/{current_branch}-20250128-120000", reason="uncommitted_changes")],
        )

    async def mock_checkout_branch(working_dir, branch, agent="unknown"):
        return BranchResult(
            success=False,
            error="checkout failed: conflict",
        )

    monkeypatch.setattr(branch_resolver, "_get_current_branch", mock_get_current_branch)
    monkeypatch.setattr(branch_resolver, "_is_working_tree_clean", mock_is_working_tree_clean)
    monkeypatch.setattr(branch_resolver, "_create_backup_branch", mock_create_backup_branch)
    monkeypatch.setattr(branch_resolver, "_checkout_branch", mock_checkout_branch)

    event = {
        "object_kind": "merge_request",
        "object_attributes": {
            "source_branch": "feature/target",
            "target_branch": "main",
        },
    }

    result = await resolve_branch(
        event=event,
        project_path="group/project",
        working_dir=str(tmp_path),
        agent="claude",
    )

    assert result.success is False
    assert len(result.backups) == 1
    assert result.backups[0].branch == "backup/claude/old-branch-20250128-120000"
    assert result.backups[0].reason == "uncommitted_changes"


@pytest.mark.asyncio
async def test_authenticate_git_timeout_returns_false():
    """A hanging glab-usr call is killed and _authenticate_git returns False."""
    import asyncio
    from unittest.mock import patch, AsyncMock
    from app.services.git_runtime import git_auth_lock

    async def _hanging_communicate():
        await asyncio.sleep(10)
        return (b"", b"")

    mock_proc = type("FakeProc", (), {})()
    mock_proc.communicate = _hanging_communicate
    mock_proc.kill = lambda: None

    async def _wait():
        return 0
    mock_proc.wait = _wait

    assert not git_auth_lock.locked()

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)), \
         patch("app.services.branch_resolver.GLAB_USR_TIMEOUT_SECONDS", 0.05):
        result = await branch_resolver._authenticate_git("claude")

    assert result is False
    assert not git_auth_lock.locked()


@pytest.mark.asyncio
async def test_authenticate_git_normal_completes():
    """A fast glab-usr call completes normally with the timeout in place."""
    import asyncio
    from unittest.mock import patch, AsyncMock

    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"ok\n", b"")
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        result = await branch_resolver._authenticate_git("claude")

    assert result is True
