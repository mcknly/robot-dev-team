"""Robot Dev Team Project
File: tests/test_routes.py
Description: Pytest coverage for routing registry behaviour.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import logging
from pathlib import Path

import pytest
import yaml

from app.services.routes import RouteRegistry


def test_route_registry_resolves_first_match(tmp_path):
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: labeled-issue
            match:
              event: "Issue Hook"
              labels: ["bug", "backend"]
            agents:
              - agent: "claude"
                task: "analyze"
          - name: fallback-issue
            match:
              event: "Issue Hook"
            agents:
              - agent: "codex"
                task: "triage"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    agents = registry.resolve(
        event_name="Issue Hook",
        action="open",
        author="alice",
        labels=["bug", "frontend", "backend"],
        mentions=["claude"],
    )

    assert len(agents) == 1
    assert agents[0].agent == "claude"
    assert agents[0].task == "analyze"


def test_route_registry_returns_empty_when_no_match(tmp_path):
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: merge-request-open
            match:
              event: "Merge Request Hook"
              action: "open"
            agents:
              - agent: "gemini"
                task: "review"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    agents = registry.resolve(
        event_name="Issue Hook",
        action="open",
        author="alice",
        labels=[],
        mentions=[],
    )

    assert agents == []


def test_route_registry_matches_mentions(tmp_path):
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: direct-mention
            match:
              event: "Note Hook"
              action: "comment"
              mentions: ["claude-bot"]
            agents:
              - agent: "claude"
                task: "note_followup"
          - name: fallback
            match:
              event: "Note Hook"
            agents:
              - agent: "codex"
                task: "note_followup"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    agents = registry.resolve(
        event_name="Note Hook",
        action="comment",
        author="bob",
        labels=[],
        mentions=["claude-bot"],
    )

    assert len(agents) == 1
    assert agents[0].agent == "claude"


def test_route_registry_matches_mentions_case_insensitive(tmp_path):
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: direct-mention
            match:
              event: "Note Hook"
              action: "comment"
              mentions: ["claude-bot"]
            agents:
              - agent: "claude"
                task: "note_followup"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    agents = registry.resolve(
        event_name="Note Hook",
        action="comment",
        author="bob",
        labels=[],
        mentions=["Claude-Bot"],
    )

    assert len(agents) == 1
    assert agents[0].agent == "claude"


def test_route_registry_substitutes_model_placeholder(tmp_path):
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: mention-model
            match:
              event: "Note Hook"
            agents:
              - agent: "claude"
                task: "note_followup"
                options:
                  args: ["--model", "${CLAUDE_MODEL}", "--dangerously-skip-permissions"]
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(
        str(routes_yaml),
        reload_on_change=False,
        model_variables={"CLAUDE_MODEL": "claude-test-model"},
    )

    agents = registry.resolve(
        event_name="Note Hook",
        action="comment",
        author="alice",
        labels=[],
        mentions=[],
    )

    assert agents[0].options["args"][1] == "claude-test-model"


def test_route_registry_leaves_literal_model_value(tmp_path):
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: literal-model
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "triage"
                options:
                  args: ["--model", "claude-override"]
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    agents = registry.resolve(
        event_name="Issue Hook",
        action="open",
        author="bob",
        labels=[],
        mentions=[],
    )

    assert agents[0].options["args"][1] == "claude-override"


def test_route_registry_raises_on_missing_model_placeholder(tmp_path, monkeypatch):
    monkeypatch.delenv("UNKNOWN_MODEL", raising=False)
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: missing-model
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "triage"
                options:
                  args: ["--model", "${UNKNOWN_MODEL}"]
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="UNKNOWN_MODEL"):
        RouteRegistry(str(routes_yaml), reload_on_change=False, model_variables={})


def test_model_substitution_warns_on_non_model_env_var(tmp_path, monkeypatch, caplog):
    """Warn when a --model placeholder references a non-*_MODEL env var."""
    monkeypatch.setenv("MY_SECRET", "leaked-value")
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: leaky-route
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "triage"
                options:
                  args: ["--model", "${MY_SECRET}"]
        """,
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="app.services.routes"):
        RouteRegistry(str(routes_yaml), reload_on_change=False, model_variables={})

    assert any("MY_SECRET" in record.message for record in caplog.records)
    assert any("non-MODEL" in record.message for record in caplog.records)


def test_model_substitution_no_warning_for_model_env_var(tmp_path, monkeypatch, caplog):
    """No warning when a --model placeholder follows the *_MODEL convention."""
    monkeypatch.setenv("CLAUDE_MODEL", "test-model")
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: good-route
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "triage"
                options:
                  args: ["--model", "${CLAUDE_MODEL}"]
        """,
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="app.services.routes"):
        RouteRegistry(str(routes_yaml), reload_on_change=False, model_variables={})

    assert not any("non-MODEL" in record.message for record in caplog.records)


def test_route_registry_matches_pattern(tmp_path):
    """Test that routes can match on body text using regex pattern."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: assign-claude
            match:
              event: "Note Hook"
              action: "create"
              mentions: ["claude"]
              pattern: "^\\\\s*/assign\\\\s+@claude\\\\b"
            agents:
              - agent: "claude"
                task: "assign_work"
          - name: mention-claude
            match:
              event: "Note Hook"
              action: "create"
              mentions: ["claude"]
            agents:
              - agent: "claude"
                task: "note_followup"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    # Pattern match should select assign route
    agents = registry.resolve(
        event_name="Note Hook",
        action="create",
        author="testuser",
        labels=[],
        mentions=["claude"],
        body="/assign @claude",
    )
    assert len(agents) == 1
    assert agents[0].task == "assign_work"

    # Without pattern match, should fall through to mention route
    agents = registry.resolve(
        event_name="Note Hook",
        action="create",
        author="testuser",
        labels=[],
        mentions=["claude"],
        body="@claude what do you think?",
    )
    assert len(agents) == 1
    assert agents[0].task == "note_followup"


def test_route_registry_pattern_with_leading_whitespace(tmp_path):
    """Test that pattern anchored with \\s* matches leading whitespace."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: assign-claude
            match:
              event: "Note Hook"
              pattern: "^\\\\s*/assign\\\\s+@claude\\\\b"
            agents:
              - agent: "claude"
                task: "assign_work"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    # Should match with leading whitespace
    agents = registry.resolve(
        event_name="Note Hook",
        action="create",
        author="testuser",
        labels=[],
        mentions=[],
        body="  /assign @claude",
    )
    assert len(agents) == 1
    assert agents[0].task == "assign_work"


def test_route_registry_pattern_no_match_without_body(tmp_path):
    """Test that pattern routes don't match when body is None."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: assign-claude
            match:
              event: "Note Hook"
              pattern: "^\\\\s*/assign\\\\s+@claude\\\\b"
            agents:
              - agent: "claude"
                task: "assign_work"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    # Should not match when body is None
    agents = registry.resolve(
        event_name="Note Hook",
        action="create",
        author="testuser",
        labels=[],
        mentions=[],
        body=None,
    )
    assert agents == []


def test_route_registry_raises_on_invalid_pattern(tmp_path):
    """Test that invalid regex patterns raise ValueError during load."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: bad-pattern
            match:
              event: "Note Hook"
              pattern: "[invalid regex"
            agents:
              - agent: "claude"
                task: "test"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid regex pattern"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)


def test_route_registry_pattern_word_boundary(tmp_path):
    """Test that word boundary prevents partial username matches."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: assign-claude
            match:
              event: "Note Hook"
              pattern: "^\\\\s*/assign\\\\s+@claude\\\\b"
            agents:
              - agent: "claude"
                task: "assign_work"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    # Should match exact username
    agents = registry.resolve(
        event_name="Note Hook",
        action="create",
        author="testuser",
        labels=[],
        mentions=[],
        body="/assign @claude",
    )
    assert len(agents) == 1

    # Should NOT match partial username like @claudebot
    agents = registry.resolve(
        event_name="Note Hook",
        action="create",
        author="testuser",
        labels=[],
        mentions=[],
        body="/assign @claudebot",
    )
    assert agents == []


def test_route_registry_matches_assignees(tmp_path):
    """Test that routes can match on assignees list."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: assign-claude
            access: readwrite
            match:
              event: "Issue Hook"
              action: "update"
              assignees: ["claude"]
            agents:
              - agent: "claude"
                task: "assign_work"
          - name: issue-update-fallback
            match:
              event: "Issue Hook"
              action: "update"
            agents:
              - agent: "codex"
                task: "fallback"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    # Should match assign-claude when claude is in assignees
    agents = registry.resolve(
        event_name="Issue Hook",
        action="update",
        author="testuser",
        labels=[],
        mentions=[],
        assignees=["claude"],
    )
    assert len(agents) == 1
    assert agents[0].agent == "claude"
    assert agents[0].task == "assign_work"

    # Should fall through to fallback when no agent in assignees
    agents = registry.resolve(
        event_name="Issue Hook",
        action="update",
        author="testuser",
        labels=[],
        mentions=[],
        assignees=["some-user"],
    )
    assert len(agents) == 1
    assert agents[0].agent == "codex"
    assert agents[0].task == "fallback"


def test_route_registry_assignees_case_insensitive(tmp_path):
    """Test that assignee matching is case-insensitive."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: assign-claude
            match:
              event: "Issue Hook"
              assignees: ["claude"]
            agents:
              - agent: "claude"
                task: "assign_work"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    # Should match with different case
    agents = registry.resolve(
        event_name="Issue Hook",
        action="update",
        author="testuser",
        labels=[],
        mentions=[],
        assignees=["Claude"],
    )
    assert len(agents) == 1
    assert agents[0].agent == "claude"


def test_route_registry_assignees_no_match_empty(tmp_path):
    """Test that assignee routes don't match when assignees is empty."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: assign-claude
            match:
              event: "Issue Hook"
              action: "update"
              assignees: ["claude"]
            agents:
              - agent: "claude"
                task: "assign_work"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    # Should not match when assignees is empty
    agents = registry.resolve(
        event_name="Issue Hook",
        action="update",
        author="testuser",
        labels=[],
        mentions=[],
        assignees=[],
    )
    assert agents == []


def test_route_registry_assignees_with_multiple(tmp_path):
    """Test matching when multiple users are assigned."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: assign-claude
            match:
              event: "Issue Hook"
              assignees: ["claude"]
            agents:
              - agent: "claude"
                task: "assign_work"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    # Should match when claude is among multiple assignees
    agents = registry.resolve(
        event_name="Issue Hook",
        action="update",
        author="testuser",
        labels=[],
        mentions=[],
        assignees=["alice", "claude", "bob"],
    )
    assert len(agents) == 1
    assert agents[0].agent == "claude"


def test_default_routes_enforce_non_interactive_flags():
    """Verify that the shipped routes.yaml includes non-interactive flags for
    Claude and Gemini so agents exit after processing instead of waiting for
    further input (see issue #61)."""
    routes_file = Path(__file__).resolve().parent.parent / "config" / "routes.yaml"
    data = yaml.safe_load(routes_file.read_text(encoding="utf-8"))
    routes = data.get("routes", [])
    assert routes, "routes.yaml should contain at least one route"

    for route in routes:
        for agent_entry in route.get("agents", []):
            agent_name = agent_entry.get("agent", "")
            args = agent_entry.get("options", {}).get("args", [])

            if agent_name == "gemini":
                assert "-p" in args, (
                    f"Route '{route['name']}' gemini agent is missing the "
                    f"'-p' non-interactive flag in args: {args}"
                )
                p_index = args.index("-p")
                assert p_index + 1 < len(args) and args[p_index + 1] == "", (
                    f"Route '{route['name']}' gemini agent needs '-p' "
                    f"followed by empty string for headless mode: {args}"
                )

            if agent_name == "claude":
                assert "-p" in args, (
                    f"Route '{route['name']}' claude agent is missing the "
                    f"'-p' non-interactive flag in args: {args}"
                )


def test_route_registry_parses_timeout_overrides(tmp_path):
    """Test that per-route timeout overrides are parsed correctly."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: long-running-task
            access: readwrite
            max_wall_clock_seconds: 14400
            max_inactivity_seconds: 1800
            match:
              event: "Issue Hook"
              action: "update"
            agents:
              - agent: "claude"
                task: "assign_work"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    agents = registry.resolve(
        event_name="Issue Hook",
        action="update",
        author="alice",
        labels=[],
        mentions=[],
    )

    assert len(agents) == 1
    assert agents[0].max_wall_clock_seconds == 14400
    assert agents[0].max_inactivity_seconds == 1800


def test_route_registry_timeout_defaults_to_none(tmp_path):
    """Test that timeout fields default to None when not specified."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: simple-route
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    agents = registry.resolve(
        event_name="Issue Hook",
        action="open",
        author="alice",
        labels=[],
        mentions=[],
    )

    assert len(agents) == 1
    assert agents[0].max_wall_clock_seconds is None
    assert agents[0].max_inactivity_seconds is None


def test_route_registry_raises_on_invalid_timeout(tmp_path):
    """Test that invalid timeout values raise ValueError."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: bad-timeout
            max_wall_clock_seconds: -5
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be a positive integer"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)


def test_route_registry_raises_on_non_numeric_timeout(tmp_path):
    """Test that non-numeric timeout values raise ValueError."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: bad-timeout-type
            max_inactivity_seconds: "not-a-number"
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be a positive integer"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)
