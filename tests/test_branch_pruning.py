"""Robot Dev Team Project
File: tests/test_branch_pruning.py
Description: Pytest coverage for branch pruning routines.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.branch_pruning import BranchPruner


@pytest.fixture
def pruner(tmp_path: Path) -> BranchPruner:
    """Returns a BranchPruner configured for testing with live deletion."""
    return BranchPruner(
        enabled=True,
        interval_hours=24,
        dry_run=False,
        base_branch="main",
        protected_patterns="main,master,HEAD,backup/*",
        agent="claude",
        min_age_hours=0,
        projects_root=tmp_path,
    )


@pytest.fixture
def dry_run_pruner(tmp_path: Path) -> BranchPruner:
    """Returns a BranchPruner configured for dry-run mode."""
    return BranchPruner(
        enabled=True,
        interval_hours=24,
        dry_run=True,
        base_branch="main",
        protected_patterns="main,master,HEAD,backup/*",
        agent="claude",
        min_age_hours=0,
        projects_root=tmp_path,
    )


def _make_repo(tmp_path: Path, namespace: str, project: str, shallow: bool = False) -> Path:
    """Create a fake git repository directory structure."""
    repo = tmp_path / namespace / project
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True)
    if shallow:
        (git_dir / "shallow").touch()
    return repo


# --- Pattern matching tests ---


class TestIsProtected:
    def test_builtin_protected_main(self, pruner: BranchPruner) -> None:
        assert pruner._is_protected("main") is True

    def test_builtin_protected_master(self, pruner: BranchPruner) -> None:
        assert pruner._is_protected("master") is True

    def test_builtin_protected_head(self, pruner: BranchPruner) -> None:
        assert pruner._is_protected("HEAD") is True

    def test_backup_glob_pattern(self, pruner: BranchPruner) -> None:
        assert pruner._is_protected("backup/claude/feature-123-20260101") is True

    def test_feature_branch_not_protected(self, pruner: BranchPruner) -> None:
        assert pruner._is_protected("feature/issue-42-widgets") is False

    def test_fix_branch_not_protected(self, pruner: BranchPruner) -> None:
        assert pruner._is_protected("fix/typo") is False


# --- Branch output parsing tests ---


class TestParseMergedBranches:
    def test_parses_normal_branches(self) -> None:
        output = "  origin/feature-a\n  origin/feature-b\n"
        assert BranchPruner._parse_merged_branches(output) == ["feature-a", "feature-b"]

    def test_skips_head_pointer(self) -> None:
        output = "  origin/HEAD -> origin/main\n  origin/feature-a\n"
        assert BranchPruner._parse_merged_branches(output) == ["feature-a"]

    def test_empty_output(self) -> None:
        assert BranchPruner._parse_merged_branches("") == []

    def test_whitespace_only(self) -> None:
        assert BranchPruner._parse_merged_branches("   \n  \n") == []

    def test_mixed_content(self) -> None:
        output = (
            "  origin/HEAD -> origin/main\n"
            "  origin/main\n"
            "  origin/feature/nested-path\n"
            "  origin/fix-123\n"
        )
        result = BranchPruner._parse_merged_branches(output)
        assert result == ["main", "feature/nested-path", "fix-123"]


# --- Repository discovery tests ---


class TestDiscoverRepos:
    def test_discovers_repos(self, pruner: BranchPruner, tmp_path: Path) -> None:
        _make_repo(tmp_path, "group", "project-a")
        _make_repo(tmp_path, "group", "project-b")
        repos = pruner._discover_repos()
        assert len(repos) == 2

    def test_skips_non_git_dirs(self, pruner: BranchPruner, tmp_path: Path) -> None:
        (tmp_path / "group" / "not-a-repo").mkdir(parents=True)
        _make_repo(tmp_path, "group", "real-repo")
        repos = pruner._discover_repos()
        assert len(repos) == 1

    def test_empty_root(self, pruner: BranchPruner) -> None:
        assert pruner._discover_repos() == []

    def test_no_projects_root(self, tmp_path: Path) -> None:
        pruner = BranchPruner(
            enabled=True, interval_hours=1, dry_run=True,
            base_branch="main", protected_patterns="main",
            agent="claude", min_age_hours=0,
            projects_root=tmp_path / "nonexistent",
        )
        assert pruner._discover_repos() == []


# --- Dynamic base branch detection tests ---


class TestDetectBaseBranch:
    @pytest.mark.asyncio
    async def test_detects_main(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")
        remote_output = (
            "* remote origin\n"
            "  Fetch URL: git@example.com:ns/repo.git\n"
            "  HEAD branch: main\n"
        )

        async def mock_run_git(args, cwd):
            if args[0] == "remote":
                return (0, remote_output, "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._detect_base_branch(repo)

        assert result == "main"

    @pytest.mark.asyncio
    async def test_detects_master(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")
        remote_output = (
            "* remote origin\n"
            "  HEAD branch: master\n"
        )

        async def mock_run_git(args, cwd):
            if args[0] == "remote":
                return (0, remote_output, "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._detect_base_branch(repo)

        assert result == "master"

    @pytest.mark.asyncio
    async def test_detects_custom_branch(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")
        remote_output = "  HEAD branch: develop\n"

        async def mock_run_git(args, cwd):
            if args[0] == "remote":
                return (0, remote_output, "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._detect_base_branch(repo)

        assert result == "develop"

    @pytest.mark.asyncio
    async def test_falls_back_on_failure(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")

        async def mock_run_git(args, cwd):
            if args[0] == "remote":
                return (1, "", "fatal: not a git repository")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._detect_base_branch(repo)

        assert result == "main"  # Falls back to configured base_branch

    @pytest.mark.asyncio
    async def test_falls_back_on_missing_head_line(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")
        remote_output = "* remote origin\n  Fetch URL: git@example.com:ns/repo.git\n"

        async def mock_run_git(args, cwd):
            if args[0] == "remote":
                return (0, remote_output, "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._detect_base_branch(repo)

        assert result == "main"


# --- Branch age filter tests ---


class TestGetMergeAge:
    @pytest.mark.asyncio
    async def test_uses_merge_commit_timestamp(self, pruner: BranchPruner, tmp_path: Path) -> None:
        """When a merge commit exists, its timestamp determines the age."""
        repo = _make_repo(tmp_path, "ns", "repo")
        merge_ts = str(int(time.time()) - 7200)  # merged 2 hours ago

        async def mock_run_git(args, cwd):
            if args[0] == "log" and "--merges" in args:
                return (0, merge_ts + "\n", "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            age = await pruner._get_merge_age(repo, "feature-x", "main")

        assert age is not None
        assert age >= 7199  # Allow slight timing variation

    @pytest.mark.asyncio
    async def test_falls_back_to_tip_for_fast_forward(self, pruner: BranchPruner, tmp_path: Path) -> None:
        """When no merge commit exists (fast-forward), falls back to branch tip date."""
        repo = _make_repo(tmp_path, "ns", "repo")
        tip_ts = str(int(time.time()) - 3600)  # tip commit 1 hour ago

        async def mock_run_git(args, cwd):
            if args[0] == "log" and "--merges" in args:
                return (0, "", "")  # No merge commit found
            if args[0] == "log":
                return (0, tip_ts + "\n", "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            age = await pruner._get_merge_age(repo, "feature-x", "main")

        assert age is not None
        assert age >= 3599

    @pytest.mark.asyncio
    async def test_old_branch_recent_merge_uses_merge_time(self, pruner: BranchPruner, tmp_path: Path) -> None:
        """Regression: an old branch merged recently should report recent age."""
        repo = _make_repo(tmp_path, "ns", "repo")
        merge_ts = str(int(time.time()) - 1800)  # merged 30 minutes ago
        tip_ts = str(int(time.time()) - 604800)  # branch tip is 7 days old

        async def mock_run_git(args, cwd):
            if args[0] == "log" and "--merges" in args:
                return (0, merge_ts + "\n", "")
            if args[0] == "log":
                return (0, tip_ts + "\n", "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            age = await pruner._get_merge_age(repo, "feature-x", "main")

        # Should use the merge commit time (30 min), NOT the tip time (7 days)
        assert age is not None
        assert age < 3600  # Less than 1 hour

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")

        async def mock_run_git(args, cwd):
            return (1, "", "unknown ref")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            age = await pruner._get_merge_age(repo, "nonexistent", "main")

        assert age is None

    @pytest.mark.asyncio
    async def test_returns_none_on_bad_output(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")

        async def mock_run_git(args, cwd):
            if args[0] == "log" and "--merges" in args:
                return (0, "not-a-number\n", "")
            return (1, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            age = await pruner._get_merge_age(repo, "feature-x", "main")

        assert age is None


class TestAgeFilter:
    @pytest.mark.asyncio
    async def test_skips_recently_merged_branches(self, tmp_path: Path) -> None:
        pruner = BranchPruner(
            enabled=True, interval_hours=24, dry_run=False,
            base_branch="main", protected_patterns="main,master,HEAD",
            agent="claude", min_age_hours=24,
            projects_root=tmp_path,
        )
        repo = _make_repo(tmp_path, "ns", "repo")
        merged_output = "  origin/feature-new\n  origin/feature-old\n"
        recent_merge_ts = str(int(time.time()) - 3600)  # merged 1 hour ago
        old_merge_ts = str(int(time.time()) - 100000)  # merged ~28 hours ago

        async def mock_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            if args[0] == "remote":
                return (0, "  HEAD branch: main\n", "")
            if args[0] == "branch":
                return (0, merged_output, "")
            if args[0] == "log" and "--merges" in args:
                # Ancestry-path arg contains "origin/<branch>..origin/<base>"
                ancestry_arg = [a for a in args if ".." in a]
                if ancestry_arg:
                    if "origin/feature-new" in ancestry_arg[0]:
                        return (0, recent_merge_ts + "\n", "")
                    if "origin/feature-old" in ancestry_arg[0]:
                        return (0, old_merge_ts + "\n", "")
            if args[0] == "push":
                return (0, "", "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._prune_repo(repo)

        assert "feature-old" in result
        assert "feature-new" not in result

    @pytest.mark.asyncio
    async def test_old_branch_recently_merged_is_skipped(self, tmp_path: Path) -> None:
        """Regression: a branch with old commits but merged recently must NOT be pruned."""
        pruner = BranchPruner(
            enabled=True, interval_hours=24, dry_run=False,
            base_branch="main", protected_patterns="main,master,HEAD",
            agent="claude", min_age_hours=24,
            projects_root=tmp_path,
        )
        repo = _make_repo(tmp_path, "ns", "repo")
        merged_output = "  origin/feature-ancient-but-just-merged\n"
        # Branch tip is 7 days old, but it was merged just 30 minutes ago
        recent_merge_ts = str(int(time.time()) - 1800)  # merge commit 30 min ago
        old_tip_ts = str(int(time.time()) - 604800)  # branch tip 7 days old

        async def mock_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            if args[0] == "remote":
                return (0, "  HEAD branch: main\n", "")
            if args[0] == "branch":
                return (0, merged_output, "")
            if args[0] == "log" and "--merges" in args:
                # Merge commit is recent (30 min ago)
                return (0, recent_merge_ts + "\n", "")
            if args[0] == "log":
                # Branch tip is old (7 days) -- must NOT be used when merge commit exists
                return (0, old_tip_ts + "\n", "")
            if args[0] == "push":
                return (0, "", "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._prune_repo(repo)

        # Must NOT be pruned -- merge is only 30 min old, threshold is 24h
        assert "feature-ancient-but-just-merged" not in result

    @pytest.mark.asyncio
    async def test_zero_min_age_skips_filter(self, pruner: BranchPruner, tmp_path: Path) -> None:
        """When min_age_hours=0, all merged branches are eligible."""
        repo = _make_repo(tmp_path, "ns", "repo")
        merged_output = "  origin/feature-new\n"

        async def mock_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            if args[0] == "remote":
                return (0, "  HEAD branch: main\n", "")
            if args[0] == "branch":
                return (0, merged_output, "")
            if args[0] == "push":
                return (0, "", "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._prune_repo(repo)

        assert "feature-new" in result

    @pytest.mark.asyncio
    async def test_age_check_failure_does_not_skip_branch(self, tmp_path: Path) -> None:
        """When age cannot be determined, the branch is still eligible for pruning."""
        pruner = BranchPruner(
            enabled=True, interval_hours=24, dry_run=False,
            base_branch="main", protected_patterns="main,master,HEAD",
            agent="claude", min_age_hours=24,
            projects_root=tmp_path,
        )
        repo = _make_repo(tmp_path, "ns", "repo")
        merged_output = "  origin/feature-x\n"

        async def mock_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            if args[0] == "remote":
                return (0, "  HEAD branch: main\n", "")
            if args[0] == "branch":
                return (0, merged_output, "")
            if args[0] == "log":
                return (1, "", "unknown ref")
            if args[0] == "push":
                return (0, "", "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._prune_repo(repo)

        assert "feature-x" in result


# --- Pruning logic tests ---


class TestPruneRepo:
    @pytest.mark.asyncio
    async def test_skips_shallow_clone(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "shallow-repo", shallow=True)
        result = await pruner._prune_repo(repo)
        assert result == []

    @pytest.mark.asyncio
    async def test_prunes_merged_branches(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")
        merged_output = "  origin/feature-done\n  origin/fix-old\n  origin/main\n"

        async def mock_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            if args[0] == "remote":
                return (0, "  HEAD branch: main\n", "")
            if args[0] == "branch":
                return (0, merged_output, "")
            if args[0] == "push":
                return (0, "", "")
            return (1, "", "unknown command")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._prune_repo(repo)

        assert "feature-done" in result
        assert "fix-old" in result
        assert "main" not in result

    @pytest.mark.asyncio
    async def test_dry_run_does_not_delete(self, dry_run_pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")
        merged_output = "  origin/feature-done\n"

        call_log: list[list[str]] = []

        async def mock_run_git(args, cwd):
            call_log.append(list(args))
            if args[0] == "fetch":
                return (0, "", "")
            if args[0] == "remote":
                return (0, "  HEAD branch: main\n", "")
            if args[0] == "branch":
                return (0, merged_output, "")
            return (0, "", "")

        with patch.object(dry_run_pruner, "_run_git", side_effect=mock_run_git):
            result = await dry_run_pruner._prune_repo(repo)

        assert "feature-done" in result
        # Ensure no push --delete was called
        push_calls = [c for c in call_log if c[0] == "push"]
        assert push_calls == []

    @pytest.mark.asyncio
    async def test_fetch_failure_skips_repo(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")

        async def mock_run_git(args, cwd):
            if args[0] == "fetch":
                return (1, "", "fatal: could not connect")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._prune_repo(repo)

        assert result == []

    @pytest.mark.asyncio
    async def test_branch_merged_failure_skips_repo(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")

        async def mock_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            if args[0] == "remote":
                return (0, "  HEAD branch: main\n", "")
            if args[0] == "branch":
                return (1, "", "error")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._prune_repo(repo)

        assert result == []

    @pytest.mark.asyncio
    async def test_delete_failure_continues(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")
        merged_output = "  origin/branch-a\n  origin/branch-b\n"

        async def mock_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            if args[0] == "remote":
                return (0, "  HEAD branch: main\n", "")
            if args[0] == "branch":
                return (0, merged_output, "")
            if args[0] == "push" and "branch-a" in args:
                return (1, "", "permission denied")
            if args[0] == "push" and "branch-b" in args:
                return (0, "", "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._prune_repo(repo)

        assert "branch-a" not in result
        assert "branch-b" in result

    @pytest.mark.asyncio
    async def test_protected_backup_branches_skipped(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")
        merged_output = "  origin/backup/claude/feature-123\n  origin/feature-done\n"

        async def mock_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            if args[0] == "remote":
                return (0, "  HEAD branch: main\n", "")
            if args[0] == "branch":
                return (0, merged_output, "")
            if args[0] == "push":
                return (0, "", "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner._prune_repo(repo)

        assert "backup/claude/feature-123" not in result
        assert "feature-done" in result

    @pytest.mark.asyncio
    async def test_uses_detected_base_branch(self, tmp_path: Path) -> None:
        """Pruner uses the per-repo detected base branch, not the global config."""
        pruner = BranchPruner(
            enabled=True, interval_hours=24, dry_run=True,
            base_branch="main", protected_patterns="main,master,HEAD",
            agent="claude", min_age_hours=0,
            projects_root=tmp_path,
        )
        repo = _make_repo(tmp_path, "ns", "repo")

        branch_cmd_args: list[list[str]] = []

        async def mock_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            if args[0] == "remote":
                return (0, "  HEAD branch: develop\n", "")
            if args[0] == "branch":
                branch_cmd_args.append(list(args))
                return (0, "", "")
            return (0, "", "")

        with patch.object(pruner, "_run_git", side_effect=mock_run_git):
            await pruner._prune_repo(repo)

        # Verify that git branch --merged used the detected "develop" base
        assert len(branch_cmd_args) == 1
        assert "origin/develop" in branch_cmd_args[0]


# --- Full prune_once tests ---


class TestPruneOnce:
    @pytest.mark.asyncio
    async def test_no_repos_returns_empty(self, pruner: BranchPruner) -> None:
        with patch.object(pruner, "_authenticate", return_value=True):
            result = await pruner.prune_once()
        assert result == {}

    @pytest.mark.asyncio
    async def test_auth_failure_returns_empty(self, pruner: BranchPruner, tmp_path: Path) -> None:
        _make_repo(tmp_path, "ns", "repo")
        with patch.object(pruner, "_authenticate", return_value=False):
            result = await pruner.prune_once()
        assert result == {}

    @pytest.mark.asyncio
    async def test_prune_once_aggregates_results(self, pruner: BranchPruner, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, "ns", "repo")
        merged_output = "  origin/old-branch\n"

        async def mock_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            if args[0] == "remote":
                return (0, "  HEAD branch: main\n", "")
            if args[0] == "branch":
                return (0, merged_output, "")
            if args[0] == "push":
                return (0, "", "")
            return (0, "", "")

        with patch.object(pruner, "_authenticate", return_value=True), \
             patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner.prune_once()

        assert str(repo) in result
        assert "old-branch" in result[str(repo)]

    @pytest.mark.asyncio
    async def test_repo_exception_does_not_abort(self, pruner: BranchPruner, tmp_path: Path) -> None:
        _make_repo(tmp_path, "ns", "repo-a")
        _make_repo(tmp_path, "ns", "repo-b")

        call_count = 0

        async def mock_prune_repo(repo_dir):
            nonlocal call_count
            call_count += 1
            if "repo-a" in str(repo_dir):
                raise RuntimeError("simulated failure")
            return ["cleaned-branch"]

        with patch.object(pruner, "_authenticate", return_value=True), \
             patch.object(pruner, "_prune_repo", side_effect=mock_prune_repo):
            result = await pruner.prune_once()

        assert call_count == 2
        assert any("cleaned-branch" in v for v in result.values())


# --- Authentication tests ---


class TestAuthenticate:
    @pytest.mark.asyncio
    async def test_auth_success(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"", b"")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            assert await BranchPruner._authenticate("claude") is True

    @pytest.mark.asyncio
    async def test_auth_failure(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = (b"", b"auth error")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            assert await BranchPruner._authenticate("claude") is False

    @pytest.mark.asyncio
    async def test_auth_command_not_found(self) -> None:
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            assert await BranchPruner._authenticate("claude") is False


# --- Config parsing tests ---


class TestParsePatterns:
    def test_parses_comma_separated(self) -> None:
        result = BranchPruner._parse_patterns("main,master,HEAD,backup/*")
        assert result == ["main", "master", "HEAD", "backup/*"]

    def test_handles_whitespace(self) -> None:
        result = BranchPruner._parse_patterns(" main , backup/* , release/* ")
        assert result == ["main", "backup/*", "release/*"]

    def test_empty_string(self) -> None:
        assert BranchPruner._parse_patterns("") == []


# --- Min age config test ---


class TestMinAgeConfig:
    def test_min_age_hours_converted_to_seconds(self) -> None:
        pruner = BranchPruner(
            enabled=True, interval_hours=1, dry_run=True,
            base_branch="main", protected_patterns="main",
            agent="claude", min_age_hours=48,
        )
        assert pruner.min_age_seconds == 48 * 3600

    def test_min_age_zero(self) -> None:
        pruner = BranchPruner(
            enabled=True, interval_hours=1, dry_run=True,
            base_branch="main", protected_patterns="main",
            agent="claude", min_age_hours=0,
        )
        assert pruner.min_age_seconds == 0


# --- Disabled pruner test ---


class TestDisabledPruner:
    @pytest.mark.asyncio
    async def test_disabled_does_not_loop(self) -> None:
        pruner = BranchPruner(
            enabled=False, interval_hours=1, dry_run=True,
            base_branch="main", protected_patterns="main",
            agent="claude", min_age_hours=0,
        )
        # run_pruning_loop should return immediately when disabled
        await pruner.run_pruning_loop()


# --- Subprocess timeout tests ---


class TestRunGitTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_error(self, pruner: BranchPruner, tmp_path: Path) -> None:
        """A git command that exceeds the timeout is killed and returns an error."""
        repo = _make_repo(tmp_path, "ns", "repo")

        async def _hanging_communicate():
            await asyncio.sleep(10)
            return (b"", b"")

        mock_proc = AsyncMock()
        mock_proc.communicate = _hanging_communicate
        mock_proc.kill = lambda: None
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            rc, stdout, stderr = await pruner._run_git(
                ["fetch", "origin"], cwd=repo, timeout=0.05,
            )

        assert rc == -1
        assert "timed out" in stderr

    @pytest.mark.asyncio
    async def test_normal_command_not_affected(self, pruner: BranchPruner, tmp_path: Path) -> None:
        """A fast git command completes normally with the timeout in place."""
        repo = _make_repo(tmp_path, "ns", "repo")

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"ok\n", b"")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            rc, stdout, stderr = await pruner._run_git(
                ["status"], cwd=repo, timeout=5.0,
            )

        assert rc == 0
        assert stdout == "ok\n"


class TestAuthenticateTimeout:
    @pytest.mark.asyncio
    async def test_auth_timeout_returns_false(self) -> None:
        """A hanging glab-usr call is killed and returns False."""
        async def _hanging_communicate():
            await asyncio.sleep(10)
            return (b"", b"")

        mock_proc = AsyncMock()
        mock_proc.communicate = _hanging_communicate
        mock_proc.kill = lambda: None
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch.object(BranchPruner, "_AUTH_TIMEOUT_SECONDS", 0.05):
            result = await BranchPruner._authenticate("claude")

        assert result is False


class TestStartupDelay:
    @pytest.mark.asyncio
    async def test_startup_delay_before_first_cycle(self) -> None:
        """run_pruning_loop waits before the first prune_once call."""
        pruner = BranchPruner(
            enabled=True, interval_hours=24, dry_run=True,
            base_branch="main", protected_patterns="main",
            agent="claude", min_age_hours=0,
        )

        sleep_calls: list[float] = []
        original_sleep = asyncio.sleep

        async def tracking_sleep(seconds, *args, **kwargs):
            sleep_calls.append(seconds)
            # Cancel the loop after recording the startup delay
            raise asyncio.CancelledError()

        with patch("app.services.branch_pruning.asyncio.sleep", side_effect=tracking_sleep):
            with pytest.raises(asyncio.CancelledError):
                await pruner.run_pruning_loop()

        # The first sleep should be the startup delay, not the interval
        assert len(sleep_calls) >= 1
        assert sleep_calls[0] == pruner._STARTUP_DELAY_SECONDS


class TestPruneOnceTimeout:
    @pytest.mark.asyncio
    async def test_hanging_fetch_does_not_block_indefinitely(
        self, tmp_path: Path,
    ) -> None:
        """A hanging git fetch during prune_once is killed by the timeout,
        allowing the lock to be released."""
        pruner = BranchPruner(
            enabled=True, interval_hours=24, dry_run=False,
            base_branch="main", protected_patterns="main,master,HEAD",
            agent="claude", min_age_hours=0,
            projects_root=tmp_path,
        )
        repo = _make_repo(tmp_path, "ns", "repo")

        async def mock_run_git(args, cwd, timeout=None):
            if args[0] == "fetch":
                # Simulate a hanging fetch that exceeds the timeout
                return (-1, "", "command timed out after 120s: git fetch --prune origin")
            return (0, "", "")

        with patch.object(pruner, "_authenticate", return_value=True), \
             patch.object(pruner, "_run_git", side_effect=mock_run_git):
            result = await pruner.prune_once()

        # The repo should be skipped (fetch failed) but prune_once should
        # complete rather than hanging
        assert result == {}
