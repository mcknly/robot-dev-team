"""Robot Dev Team Project
File: tests/test_project_paths.py
Description: Pytest coverage for project path resolution and auto-clone behaviors.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import asyncio

import pytest

from app.core.config import settings
from app.services.project_paths import (
    ProjectPathResolver,
    _validate_clone_url,
    _validate_project_path,
)


class TestValidateProjectPath:
    """Tests for _validate_project_path function."""

    def test_empty_path(self):
        assert _validate_project_path("") is False
        assert _validate_project_path(None) is False

    def test_path_traversal_attacks(self):
        assert _validate_project_path("..") is False
        assert _validate_project_path("../escape") is False
        assert _validate_project_path("foo/../bar") is False
        assert _validate_project_path("foo/..") is False
        assert _validate_project_path("foo/bar/..") is False

    def test_absolute_paths_rejected(self):
        assert _validate_project_path("/etc/passwd") is False
        assert _validate_project_path("/root") is False
        assert _validate_project_path("/group/project") is False

    def test_valid_paths(self):
        assert _validate_project_path("group/project") is True
        assert _validate_project_path("my-org/my-project") is True
        assert _validate_project_path("user123/repo_name") is True
        assert _validate_project_path("a/b/c") is True
        assert _validate_project_path("single-segment") is True

    def test_paths_with_dots(self):
        # Single dots in names are allowed
        assert _validate_project_path("my.org/my.project") is True
        assert _validate_project_path("v1.0/release") is True

    def test_special_characters_rejected(self):
        assert _validate_project_path("group/project;rm -rf") is False
        assert _validate_project_path("group/project|cat /etc/passwd") is False
        assert _validate_project_path("group/project$HOME") is False


class TestValidateCloneUrl:
    """Tests for _validate_clone_url function."""

    def test_matching_host(self, monkeypatch):
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")
        assert _validate_clone_url("https://gitlab.example.com/group/project.git") is True

    def test_mismatched_host(self, monkeypatch):
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")
        assert _validate_clone_url("https://evil.com/group/project.git") is False
        assert _validate_clone_url("https://gitlab.com/group/project.git") is False

    def test_invalid_url(self, monkeypatch):
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")
        # urlparse doesn't raise on malformed URLs, but hostname will be None
        assert _validate_clone_url("not-a-url") is False

    def test_http_url(self, monkeypatch):
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")
        assert _validate_clone_url("http://gitlab.example.com/group/project.git") is True

    def test_ssh_url(self, monkeypatch):
        """SSH URLs have different structure, hostname extraction differs."""
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")
        # Standard SSH URL format doesn't parse hostname the same way
        assert _validate_clone_url("git@gitlab.example.com:group/project.git") is False


class TestProjectPathResolverResolve:
    """Tests for ProjectPathResolver.resolve method."""

    def test_resolve_existing_git_repo(self, tmp_path):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        project_dir = projects_root / "group" / "project"
        project_dir.mkdir(parents=True)
        (project_dir / ".git").mkdir()

        resolver = ProjectPathResolver(str(projects_root))
        result = resolver.resolve("group/project", access="readwrite")
        assert result == str(project_dir)

    def test_resolve_existing_directory_not_git_repo(self, tmp_path):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        project_dir = projects_root / "group" / "project"
        project_dir.mkdir(parents=True)
        # No .git directory

        resolver = ProjectPathResolver(str(projects_root))
        result = resolver.resolve("group/project", access="readwrite")
        assert result is None

    def test_resolve_nonexistent_path(self, tmp_path):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()

        resolver = ProjectPathResolver(str(projects_root))
        result = resolver.resolve("group/nonexistent", access="readwrite")
        assert result is None

    def test_resolve_invalid_path(self, tmp_path):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()

        resolver = ProjectPathResolver(str(projects_root))
        assert resolver.resolve("../escape", access="readwrite") is None
        assert resolver.resolve("", access="readwrite") is None
        assert resolver.resolve(None, access="readwrite") is None

    def test_resolve_readonly_uses_ro_path(self, tmp_path):
        projects_root = tmp_path / "projects"
        projects_root_ro = tmp_path / "projects-ro"
        projects_root.mkdir()
        projects_root_ro.mkdir()

        project_dir_ro = projects_root_ro / "group" / "project"
        project_dir_ro.mkdir(parents=True)
        (project_dir_ro / ".git").mkdir()

        resolver = ProjectPathResolver(str(projects_root))
        result = resolver.resolve("group/project", access="readonly")
        assert result == str(project_dir_ro)


class TestProjectPathResolverEnsureProjectExists:
    """Tests for ProjectPathResolver.ensure_project_exists method."""

    @pytest.mark.asyncio
    async def test_returns_existing_project(self, tmp_path, monkeypatch):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        project_dir = projects_root / "group" / "project"
        project_dir.mkdir(parents=True)
        (project_dir / ".git").mkdir()

        resolver = ProjectPathResolver(str(projects_root))
        result = await resolver.ensure_project_exists("group/project", access="readwrite")
        assert result == str(project_dir)

    @pytest.mark.asyncio
    async def test_returns_none_when_auto_clone_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "enable_auto_clone", False)
        projects_root = tmp_path / "projects"
        projects_root.mkdir()

        resolver = ProjectPathResolver(str(projects_root))
        result = await resolver.ensure_project_exists(
            "group/nonexistent",
            access="readwrite",
            clone_url="https://gitlab.com/group/nonexistent.git",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_clone_url(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "enable_auto_clone", True)
        projects_root = tmp_path / "projects"
        projects_root.mkdir()

        resolver = ProjectPathResolver(str(projects_root))
        result = await resolver.ensure_project_exists("group/nonexistent", access="readwrite")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_clone_url_host_mismatch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "enable_auto_clone", True)
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")
        projects_root = tmp_path / "projects"
        projects_root.mkdir()

        resolver = ProjectPathResolver(str(projects_root))
        result = await resolver.ensure_project_exists(
            "group/project",
            access="readwrite",
            clone_url="https://evil.com/group/project.git",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_invalid_path(self, tmp_path, monkeypatch):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()

        resolver = ProjectPathResolver(str(projects_root))
        result = await resolver.ensure_project_exists("../escape", access="readwrite")
        assert result is None


class TestCleanupPartialClone:
    """Tests for _cleanup_partial_clone method."""

    def test_removes_existing_directory(self, tmp_path):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        partial_dir = projects_root / "partial"
        partial_dir.mkdir()
        (partial_dir / "some_file.txt").write_text("content")

        resolver = ProjectPathResolver(str(projects_root))
        resolver._cleanup_partial_clone(partial_dir)

        assert not partial_dir.exists()

    def test_handles_nonexistent_directory(self, tmp_path):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        nonexistent = projects_root / "nonexistent"

        resolver = ProjectPathResolver(str(projects_root))
        # Should not raise
        resolver._cleanup_partial_clone(nonexistent)


class TestCloneLocking:
    """Tests for concurrent clone locking."""

    @pytest.mark.asyncio
    async def test_concurrent_clones_are_serialized(self, tmp_path, monkeypatch):
        """Verify that concurrent clone attempts for the same project are serialized."""
        monkeypatch.setattr(settings, "enable_auto_clone", True)
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        (tmp_path / "projects-ro").mkdir()

        clone_count = 0
        clone_started_events = []
        clone_completed_events = []

        resolver = ProjectPathResolver(str(projects_root))

        original_clone_repository = resolver._clone_repository

        async def mock_clone_repository(clone_url, target_dir, agent=None):
            nonlocal clone_count
            clone_count += 1
            current_count = clone_count

            start_event = asyncio.Event()
            clone_started_events.append(start_event)
            start_event.set()

            # Simulate clone work
            await asyncio.sleep(0.05)

            # Create the directory to simulate successful clone
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / ".git").mkdir(exist_ok=True)

            complete_event = asyncio.Event()
            clone_completed_events.append(complete_event)
            complete_event.set()

            return True

        monkeypatch.setattr(resolver, "_clone_repository", mock_clone_repository)

        # Launch two concurrent clone attempts for the same project
        task1 = asyncio.create_task(
            resolver.ensure_project_exists(
                "group/project",
                access="readwrite",
                clone_url="https://gitlab.example.com/group/project.git",
            )
        )
        task2 = asyncio.create_task(
            resolver.ensure_project_exists(
                "group/project",
                access="readwrite",
                clone_url="https://gitlab.example.com/group/project.git",
            )
        )

        results = await asyncio.gather(task1, task2)

        # Both should return successful paths
        assert all(r is not None for r in results)
        # Only one clone should have been attempted (second should find existing)
        assert clone_count == 1


class TestCheckProjectsRootWritable:
    """Tests for check_projects_root_writable method."""

    def test_writable_directory(self, tmp_path):
        projects_root = tmp_path / "projects"
        projects_root.mkdir()

        resolver = ProjectPathResolver(str(projects_root))
        assert resolver.check_projects_root_writable() is True

    def test_nonexistent_directory(self, tmp_path):
        nonexistent = tmp_path / "nonexistent"

        resolver = ProjectPathResolver(str(nonexistent))
        assert resolver.check_projects_root_writable() is False
