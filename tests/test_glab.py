"""Robot Dev Team Project
File: tests/test_glab.py
Description: Pytest coverage for glab service helpers.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.glab import (
    _agent_token_env_var,
    _get_agent_glab_env,
    _get_glab_env,
    notify_agent_termination,
    notify_backup_created,
    resolve_agent_token,
    run_glab,
    unassign_agent,
)


class TestGetGlabEnv:
    """Tests for the _get_glab_env() helper function."""

    def test_includes_gitlab_token_when_configured(self, monkeypatch):
        """When glab_token is set in settings, GITLAB_TOKEN is included in env."""
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.setattr(settings, "glab_token", "test-token-123")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        env = _get_glab_env()

        assert env["GITLAB_TOKEN"] == "test-token-123"
        assert env["GITLAB_HOST"] == "gitlab.example.com"

    def test_excludes_gitlab_token_when_empty(self, monkeypatch):
        """When glab_token is empty, GITLAB_TOKEN is not added to env."""
        monkeypatch.setattr(settings, "glab_token", "")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)

        env = _get_glab_env()

        assert "GITLAB_TOKEN" not in env
        assert env["GITLAB_HOST"] == "gitlab.example.com"

    def test_preserves_existing_environment(self, monkeypatch):
        """The function copies os.environ and preserves existing variables."""
        monkeypatch.setenv("EXISTING_VAR", "existing-value")
        monkeypatch.setattr(settings, "glab_token", "test-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        env = _get_glab_env()

        assert env["EXISTING_VAR"] == "existing-value"
        assert env["GITLAB_TOKEN"] == "test-token"

    def test_does_not_mutate_original_environ(self, monkeypatch):
        """The function returns a copy, not the original os.environ."""
        original_environ = os.environ.copy()
        monkeypatch.setattr(settings, "glab_token", "test-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        env = _get_glab_env()
        env["NEW_VAR"] = "new-value"

        assert "NEW_VAR" not in os.environ
        assert os.environ.keys() == original_environ.keys()

    def test_includes_gitlab_host_when_configured(self, monkeypatch):
        """When glab_host is set in settings, GITLAB_HOST is included in env."""
        monkeypatch.delenv("GITLAB_HOST", raising=False)
        monkeypatch.setattr(settings, "glab_token", "")
        monkeypatch.setattr(settings, "glab_host", "custom.gitlab.com")

        env = _get_glab_env()

        assert env["GITLAB_HOST"] == "custom.gitlab.com"

    def test_excludes_gitlab_host_when_empty(self, monkeypatch):
        """When glab_host is empty, GITLAB_HOST is not added to env."""
        monkeypatch.setattr(settings, "glab_token", "")
        monkeypatch.setattr(settings, "glab_host", "")
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.delenv("GITLAB_HOST", raising=False)

        env = _get_glab_env()

        assert "GITLAB_HOST" not in env


class TestRunGlab:
    """Tests for the run_glab() helper function."""

    @pytest.mark.asyncio
    async def test_run_glab_success(self, monkeypatch):
        """run_glab returns True when command succeeds."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "test-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await run_glab(["issue", "update", "project#1", "--assignee", "-claude"])

        assert result is True

    @pytest.mark.asyncio
    async def test_run_glab_failure(self, monkeypatch):
        """run_glab returns False when command fails."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "test-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 1
        mock_process.communicate = AsyncMock(return_value=(b"", b"error message"))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await run_glab(["issue", "update", "project#1", "--assignee", "-claude"])

        assert result is False

    @pytest.mark.asyncio
    async def test_run_glab_timeout(self, monkeypatch):
        """run_glab returns False on timeout."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 1)
        monkeypatch.setattr(settings, "glab_token", "test-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.kill = MagicMock()
        mock_process.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await run_glab(["issue", "update", "project#1", "--assignee", "-claude"])

        assert result is False
        mock_process.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_glab_file_not_found(self, monkeypatch):
        """run_glab returns False when glab is not installed."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "test-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            result = await run_glab(["issue", "update", "project#1", "--assignee", "-claude"])

        assert result is False


class TestUnassignAgent:
    """Tests for the unassign_agent() helper function."""

    @pytest.mark.asyncio
    async def test_unassign_agent_issue(self, monkeypatch):
        """unassign_agent builds correct command for issues."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "test-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            result = await unassign_agent("namespace/project", 42, "issue", "claude")

        assert result is True
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0]
        assert call_args == ("glab", "issue", "update", "42", "--repo", "namespace/project", "--assignee", "-claude")

    @pytest.mark.asyncio
    async def test_unassign_agent_mr(self, monkeypatch):
        """unassign_agent builds correct command for merge requests."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "test-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            result = await unassign_agent("namespace/project", 99, "merge_request", "gemini")

        assert result is True
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0]
        assert call_args == ("glab", "mr", "update", "99", "--repo", "namespace/project", "--assignee", "-gemini")

    @pytest.mark.asyncio
    async def test_unassign_agent_mr_alias(self, monkeypatch):
        """unassign_agent handles 'mr' as resource_type alias."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "test-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            result = await unassign_agent("namespace/project", 77, "mr", "codex")

        assert result is True
        call_args = mock_exec.call_args[0]
        assert call_args == ("glab", "mr", "update", "77", "--repo", "namespace/project", "--assignee", "-codex")


class TestGetAgentGlabEnv:
    """Tests for _get_agent_glab_env() helper."""

    def test_uses_agent_token_when_available(self, monkeypatch):
        """Agent-specific token is preferred when set."""
        monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "agent-pat-123")
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        env = _get_agent_glab_env("claude")

        assert env["GITLAB_TOKEN"] == "agent-pat-123"
        assert env["GITLAB_HOST"] == "gitlab.example.com"

    def test_falls_back_to_app_token(self, monkeypatch):
        """Falls back to app-level GLAB_TOKEN when agent token is missing."""
        monkeypatch.delenv("CLAUDE_AGENT_GITLAB_TOKEN", raising=False)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        env = _get_agent_glab_env("claude")

        assert env["GITLAB_TOKEN"] == "app-token"

    def test_unconfigured_agent_falls_back_to_app_token(self, monkeypatch):
        """An agent without a token env var falls back to app-level token."""
        monkeypatch.delenv("UNKNOWN_AGENT_AGENT_GITLAB_TOKEN", raising=False)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        env = _get_agent_glab_env("unknown-agent")

        assert env["GITLAB_TOKEN"] == "app-token"

    def test_case_insensitive_lookup(self, monkeypatch):
        """Agent name lookup is case-insensitive."""
        monkeypatch.setenv("GEMINI_AGENT_GITLAB_TOKEN", "gemini-pat")
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        env = _get_agent_glab_env("Gemini")

        assert env["GITLAB_TOKEN"] == "gemini-pat"


class TestNotifyAgentTermination:
    """Tests for notify_agent_termination() helper."""

    @pytest.mark.asyncio
    async def test_posts_comment_on_issue(self, monkeypatch):
        """notify_agent_termination posts a glab comment on an issue."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            result = await notify_agent_termination(
                "namespace/project", 42, "issue", "claude",
                reason="Manual Kill",
                details="Operator terminated the agent via the dashboard.",
            )

        assert result is True
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0]
        assert call_args[0] == "glab"
        assert call_args[1] == "issue"
        assert call_args[2] == "comment"
        assert call_args[3] == "42"
        assert "--repo" in call_args
        assert "namespace/project" in call_args

    @pytest.mark.asyncio
    async def test_posts_comment_on_mr(self, monkeypatch):
        """notify_agent_termination posts a glab comment on a merge request."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            result = await notify_agent_termination(
                "namespace/project", 99, "merge_request", "gemini",
                reason="Timeout",
            )

        assert result is True
        call_args = mock_exec.call_args[0]
        assert call_args[1] == "mr"

    @pytest.mark.asyncio
    async def test_returns_false_on_failure(self, monkeypatch):
        """notify_agent_termination returns False on glab failure."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 1
        mock_process.communicate = AsyncMock(return_value=(b"", b"error"))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await notify_agent_termination(
                "namespace/project", 42, "issue", "claude",
                reason="Manual Kill",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self, monkeypatch):
        """notify_agent_termination returns False on timeout."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 1)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.kill = MagicMock()
        mock_process.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await notify_agent_termination(
                "namespace/project", 42, "issue", "claude",
                reason="Manual Kill",
            )

        assert result is False
        mock_process.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_glab_not_found(self, monkeypatch):
        """notify_agent_termination returns False when glab is not installed."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            result = await notify_agent_termination(
                "namespace/project", 42, "issue", "claude",
                reason="Manual Kill",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_uses_agent_token(self, monkeypatch):
        """notify_agent_termination uses the agent's PAT for authentication."""
        monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "claude-pat-123")
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            result = await notify_agent_termination(
                "namespace/project", 42, "issue", "claude",
                reason="Manual Kill",
            )

        assert result is True
        call_kwargs = mock_exec.call_args[1]
        assert call_kwargs["env"]["GITLAB_TOKEN"] == "claude-pat-123"

    @pytest.mark.asyncio
    async def test_includes_details_in_message(self, monkeypatch):
        """notify_agent_termination includes details in the comment body."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await notify_agent_termination(
                "namespace/project", 42, "issue", "claude",
                reason="Manual Kill",
                details="Operator terminated the agent via the dashboard.",
            )

        call_args = mock_exec.call_args[0]
        message_idx = list(call_args).index("--message") + 1
        message = call_args[message_idx]
        assert "Manual Kill" in message
        assert "@claude" in message
        assert "Operator terminated" in message


class TestResolveAgentToken:
    """Tests for resolve_agent_token() convention-based helper."""

    def test_returns_token_for_agent(self, monkeypatch):
        """Returns the token when the convention-based env var is set."""
        monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "claude-pat-123")

        assert resolve_agent_token("claude") == "claude-pat-123"

    def test_case_insensitive_lookup(self, monkeypatch):
        """Agent name lookup is case-insensitive."""
        monkeypatch.setenv("GEMINI_AGENT_GITLAB_TOKEN", "gemini-pat")

        assert resolve_agent_token("Gemini") == "gemini-pat"

    def test_returns_none_when_env_var_not_set(self, monkeypatch):
        """Returns None when the derived env var is not present."""
        monkeypatch.delenv("CUSTOM_BOT_AGENT_GITLAB_TOKEN", raising=False)

        assert resolve_agent_token("custom-bot") is None

    def test_raises_for_empty_token(self, monkeypatch):
        """Agent with empty env var raises ValueError."""
        monkeypatch.setenv("CODEX_AGENT_GITLAB_TOKEN", "")

        with pytest.raises(ValueError, match="CODEX_AGENT_GITLAB_TOKEN"):
            resolve_agent_token("codex")

    def test_raises_for_whitespace_token(self, monkeypatch):
        """Agent with whitespace-only env var raises ValueError."""
        monkeypatch.setenv("CODEX_AGENT_GITLAB_TOKEN", "   ")

        with pytest.raises(ValueError, match="CODEX_AGENT_GITLAB_TOKEN"):
            resolve_agent_token("codex")

    def test_all_default_agents_resolvable(self, monkeypatch):
        """All three default agents can resolve when tokens are set."""
        monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "c-pat")
        monkeypatch.setenv("GEMINI_AGENT_GITLAB_TOKEN", "g-pat")
        monkeypatch.setenv("CODEX_AGENT_GITLAB_TOKEN", "x-pat")

        assert resolve_agent_token("claude") == "c-pat"
        assert resolve_agent_token("gemini") == "g-pat"
        assert resolve_agent_token("codex") == "x-pat"

    def test_custom_agent_resolves_by_convention(self, monkeypatch):
        """A custom agent name resolves via naming convention."""
        monkeypatch.setenv("QWEN_CODE_AGENT_GITLAB_TOKEN", "qwen-pat")

        assert resolve_agent_token("qwen-code") == "qwen-pat"

    def test_hyphenated_agent_name_maps_to_underscore_env_var(self, monkeypatch):
        """Agent names with hyphens map to underscored env var names."""
        monkeypatch.setenv("KIMI_CLI_AGENT_GITLAB_TOKEN", "kimi-pat")

        assert resolve_agent_token("kimi-cli") == "kimi-pat"


class TestAgentTokenEnvVarConvention:
    """Tests for the _agent_token_env_var naming convention."""

    def test_simple_agent(self):
        assert _agent_token_env_var("claude") == "CLAUDE_AGENT_GITLAB_TOKEN"

    def test_hyphenated_agent(self):
        assert _agent_token_env_var("qwen-code") == "QWEN_CODE_AGENT_GITLAB_TOKEN"

    def test_multi_hyphen_agent(self):
        assert _agent_token_env_var("my-cool-agent") == "MY_COOL_AGENT_AGENT_GITLAB_TOKEN"

    def test_uppercase_input(self):
        assert _agent_token_env_var("CLAUDE") == "CLAUDE_AGENT_GITLAB_TOKEN"

    def test_mixed_case_input(self):
        assert _agent_token_env_var("Qwen-Code") == "QWEN_CODE_AGENT_GITLAB_TOKEN"


class TestNotifyBackupCreated:
    """Tests for notify_backup_created() helper."""

    @pytest.mark.asyncio
    async def test_posts_comment_on_issue(self, monkeypatch):
        """notify_backup_created posts a glab comment on an issue."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            result = await notify_backup_created(
                "namespace/project", 42, "issue", "claude",
                backup_branch="backup/claude/main-20260314-120000",
                backup_reason="uncommitted_changes",
            )

        assert result is True
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args[0]
        assert call_args[0] == "glab"
        assert call_args[1] == "issue"
        assert call_args[2] == "comment"
        assert call_args[3] == "42"
        assert "--repo" in call_args
        assert "namespace/project" in call_args

    @pytest.mark.asyncio
    async def test_posts_comment_on_mr(self, monkeypatch):
        """notify_backup_created posts a glab comment on a merge request."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            result = await notify_backup_created(
                "namespace/project", 99, "merge_request", "gemini",
                backup_branch="backup/gemini/feature-commits-20260314",
                backup_reason="local_commits",
            )

        assert result is True
        call_args = mock_exec.call_args[0]
        assert call_args[1] == "mr"

    @pytest.mark.asyncio
    async def test_returns_false_on_failure(self, monkeypatch):
        """notify_backup_created returns False on glab failure."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 1
        mock_process.communicate = AsyncMock(return_value=(b"", b"error"))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await notify_backup_created(
                "namespace/project", 42, "issue", "claude",
                backup_branch="backup/claude/main-20260314",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self, monkeypatch):
        """notify_backup_created returns False on timeout."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 1)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.kill = MagicMock()
        mock_process.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            result = await notify_backup_created(
                "namespace/project", 42, "issue", "claude",
                backup_branch="backup/claude/main-20260314",
            )

        assert result is False
        mock_process.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_glab_not_found(self, monkeypatch):
        """notify_backup_created returns False when glab is not installed."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            result = await notify_backup_created(
                "namespace/project", 42, "issue", "claude",
                backup_branch="backup/claude/main-20260314",
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_uses_agent_token(self, monkeypatch):
        """notify_backup_created uses the agent's PAT for authentication."""
        monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "claude-pat-123")
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            result = await notify_backup_created(
                "namespace/project", 42, "issue", "claude",
                backup_branch="backup/claude/main-20260314",
                backup_reason="uncommitted_changes",
            )

        assert result is True
        call_kwargs = mock_exec.call_args[1]
        assert call_kwargs["env"]["GITLAB_TOKEN"] == "claude-pat-123"

    @pytest.mark.asyncio
    async def test_uncommitted_changes_reason_in_message(self, monkeypatch):
        """Comment body includes uncommitted changes reason text."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await notify_backup_created(
                "namespace/project", 42, "issue", "claude",
                backup_branch="backup/claude/main-20260314-120000",
                backup_reason="uncommitted_changes",
            )

        call_args = mock_exec.call_args[0]
        message_idx = list(call_args).index("--message") + 1
        message = call_args[message_idx]
        assert "Auto-backup created" in message
        assert "@claude" in message
        assert "backup/claude/main-20260314-120000" in message
        assert "Uncommitted changes" in message
        assert "git fetch origin" in message

    @pytest.mark.asyncio
    async def test_local_commits_reason_in_message(self, monkeypatch):
        """Comment body includes local commits reason text."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await notify_backup_created(
                "namespace/project", 42, "issue", "claude",
                backup_branch="backup/claude/main-commits-20260314",
                backup_reason="local_commits",
            )

        call_args = mock_exec.call_args[0]
        message_idx = list(call_args).index("--message") + 1
        message = call_args[message_idx]
        assert "Local commits ahead of origin" in message

    @pytest.mark.asyncio
    async def test_fallback_reason_text(self, monkeypatch):
        """Comment body uses generic fallback when reason is empty or unknown."""
        monkeypatch.setattr(settings, "glab_timeout_seconds", 30)
        monkeypatch.setattr(settings, "glab_token", "app-token")
        monkeypatch.setattr(settings, "glab_host", "gitlab.example.com")

        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await notify_backup_created(
                "namespace/project", 42, "issue", "claude",
                backup_branch="backup/claude/main-20260314",
                backup_reason="",
            )

        call_args = mock_exec.call_args[0]
        message_idx = list(call_args).index("--message") + 1
        message = call_args[message_idx]
        assert "Changes were detected that needed to be preserved" in message
