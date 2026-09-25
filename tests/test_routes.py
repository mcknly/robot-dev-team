"""Robot Dev Team Project
File: tests/test_routes.py
Description: Pytest coverage for routing registry behaviour.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import logging
import re
from pathlib import Path

import pytest
import yaml

from app.services.routes import PROMPT_ARG_PLACEHOLDER, RouteRegistry


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


def test_route_registry_raises_on_empty_model_placeholder(tmp_path):
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: empty-model
            match:
              event: "Issue Hook"
            agents:
              - agent: "gemini"
                task: "triage"
                options:
                  args: ["--model", "${GEMINI_MODEL}", "-p", "${PROMPT}"]
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="empty"):
        RouteRegistry(
            str(routes_yaml),
            reload_on_change=False,
            model_variables={"GEMINI_MODEL": ""},
        )


def test_route_registry_rejects_partial_prompt_placeholder(tmp_path):
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: partial-prompt-placeholder
            match:
              event: "Issue Hook"
            agents:
              - agent: "gemini"
                task: "triage"
                options:
                  args: ["--model", "${GEMINI_MODEL}", "-p=${PROMPT}"]
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"\$\{PROMPT\}.*own args element"):
        RouteRegistry(
            str(routes_yaml),
            reload_on_change=False,
            model_variables={"GEMINI_MODEL": "Gemini 3.1 Pro (High)"},
        )


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
    further input (see issue #61).

    The gemini agent is now backed by Google's Antigravity CLI (`agy`), which
    replaced the deprecated `@google/gemini-cli` npm package (see issue #10).
    `--yolo` / `--skip-trust` from the old CLI are gone; Antigravity exposes
    `--dangerously-skip-permissions` (verbatim Claude name) for the same
    purpose. Two `agy`-specific contracts are enforced here (see issue #19):
    `--model` is accepted as of `agy` 1.1.1 and must be followed by the
    `${GEMINI_MODEL}` placeholder the loader substitutes; and `-p`/`--print`
    takes the prompt as its *value* -- `agy` never reads stdin -- so it must be
    followed by the `${PROMPT}` placeholder that dispatch fills in.
    """
    routes_file = Path(__file__).resolve().parent.parent / "config" / "routes.yaml"
    data = yaml.safe_load(routes_file.read_text(encoding="utf-8"))
    routes = data.get("routes", [])
    assert routes, "routes.yaml should contain at least one route"

    for route in routes:
        for agent_entry in route.get("agents", []):
            agent_name = agent_entry.get("agent", "")
            command = agent_entry.get("options", {}).get("command", "")
            args = agent_entry.get("options", {}).get("args", [])

            if agent_name == "gemini":
                assert command == "agy", (
                    f"Route '{route['name']}' gemini agent should invoke the "
                    f"Antigravity CLI binary `agy`, got: {command!r}"
                )
                assert "--model" in args, (
                    f"Route '{route['name']}' gemini agent is missing the "
                    f"'--model' flag in args: {args}"
                )
                model_index = args.index("--model")
                assert (
                    model_index + 1 < len(args)
                    and args[model_index + 1] == "${GEMINI_MODEL}"
                ), (
                    f"Route '{route['name']}' gemini agent needs '--model' "
                    f"followed by the '${{GEMINI_MODEL}}' placeholder so the "
                    f"loader can substitute it: {args}"
                )
                assert "--dangerously-skip-permissions" in args, (
                    f"Route '{route['name']}' gemini agent needs "
                    f"'--dangerously-skip-permissions' so agy auto-approves "
                    f"tool calls in headless mode: {args}"
                )
                assert "-p" in args, (
                    f"Route '{route['name']}' gemini agent is missing the "
                    f"'-p' non-interactive flag in args: {args}"
                )
                p_index = args.index("-p")
                assert p_index + 1 < len(args) and args[p_index + 1] == "${PROMPT}", (
                    f"Route '{route['name']}' gemini agent needs '-p' followed "
                    f"by the '${{PROMPT}}' placeholder -- agy takes the prompt "
                    f"as the flag's value and never reads stdin: {args}"
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


def test_route_registry_author_string_matches(tmp_path):
    """Scalar `author` string matches the configured username (regression)."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: alice-issue
            match:
              event: "Issue Hook"
              author: "alice"
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
    assert agents[0].agent == "claude"

    # Different author should not match.
    agents = registry.resolve(
        event_name="Issue Hook",
        action="open",
        author="bob",
        labels=[],
        mentions=[],
    )
    assert agents == []


def test_route_registry_author_string_is_case_insensitive(tmp_path):
    """Scalar `author` matches across case (intentional behavior change)."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: cavin-issue
            match:
              event: "Issue Hook"
              author: "Cavin"
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
        author="cavin",
        labels=[],
        mentions=[],
    )
    assert len(agents) == 1


def test_route_registry_author_list_matches_any_member(tmp_path):
    """List `author` matches when the event author is one of the entries."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: multi-author
            match:
              event: "Issue Hook"
              author: ["cavin", "alice"]
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    for username in ("cavin", "alice"):
        agents = registry.resolve(
            event_name="Issue Hook",
            action="open",
            author=username,
            labels=[],
            mentions=[],
        )
        assert len(agents) == 1, f"expected match for {username}"


def test_route_registry_author_list_rejects_unlisted(tmp_path):
    """List `author` rejects authors not in the list."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: multi-author
            match:
              event: "Issue Hook"
              author: ["cavin", "alice"]
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
        author="mallory",
        labels=[],
        mentions=[],
    )
    assert agents == []


def test_route_registry_author_list_is_case_insensitive(tmp_path):
    """List `author` matches across case."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: multi-author
            match:
              event: "Issue Hook"
              author: ["Cavin", "ALICE"]
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    for username in ("cavin", "CAVIN", "Alice", "alice"):
        agents = registry.resolve(
            event_name="Issue Hook",
            action="open",
            author=username,
            labels=[],
            mentions=[],
        )
        assert len(agents) == 1, f"expected match for {username}"


def test_route_registry_author_missing_matches_any(tmp_path):
    """Missing `author` matches any event author (unchanged behavior)."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: anyone
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    for username in ("alice", "bob", None):
        agents = registry.resolve(
            event_name="Issue Hook",
            action="open",
            author=username,
            labels=[],
            mentions=[],
        )
        assert len(agents) == 1, f"expected match for author={username!r}"


def test_route_registry_author_empty_list_matches_any(tmp_path):
    """Explicit empty-list `author` is equivalent to omitted `author`."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: anyone-explicit
            match:
              event: "Issue Hook"
              author: []
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    for username in ("alice", "bob", None):
        agents = registry.resolve(
            event_name="Issue Hook",
            action="open",
            author=username,
            labels=[],
            mentions=[],
        )
        assert len(agents) == 1, f"expected match for author={username!r}"


def test_route_registry_raises_on_invalid_author_type(tmp_path):
    """A scalar non-string `author` raises ValueError naming the route."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: bad-author-scalar
            match:
              event: "Issue Hook"
              author: 123
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bad-author-scalar"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)


def test_route_registry_raises_on_invalid_author_list_entry(tmp_path):
    """A list `author` with non-string entries raises ValueError naming the route."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: bad-author-list
            match:
              event: "Issue Hook"
              author: [1, 2]
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bad-author-list"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)


def test_route_registry_raises_on_empty_author_string(tmp_path):
    """Scalar `author: ""` is rejected because it almost always indicates a typo."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: empty-author-scalar
            match:
              event: "Issue Hook"
              author: ""
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="empty-author-scalar"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)


def test_route_registry_randomize_omitted_preserves_order(tmp_path):
    """When `randomize` is omitted, agents are dispatched in YAML order."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: multi-agent
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "review"
              - agent: "gemini"
                task: "review"
              - agent: "codex"
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
    assert [task.agent for task in agents] == ["claude", "gemini", "codex"]


def test_route_registry_randomize_false_preserves_order(tmp_path):
    """Explicit `randomize: false` matches the default omitted behavior."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: multi-agent
            randomize: false
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "review"
              - agent: "gemini"
                task: "review"
              - agent: "codex"
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
    assert [task.agent for task in agents] == ["claude", "gemini", "codex"]


def test_route_registry_randomize_true_shuffles(tmp_path, monkeypatch):
    """`randomize: true` shuffles the agent list using `random.shuffle`."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: multi-agent
            randomize: true
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "review"
              - agent: "gemini"
                task: "review"
              - agent: "codex"
                task: "review"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    # Deterministic reorder for the assertion. Patching the symbol the module
    # actually uses (app.services.routes.random.shuffle) avoids depending on
    # global random module state in CI.
    monkeypatch.setattr(
        "app.services.routes.random.shuffle",
        lambda seq: seq.reverse(),
    )

    agents = registry.resolve(
        event_name="Issue Hook",
        action="open",
        author="alice",
        labels=[],
        mentions=[],
    )
    assert [task.agent for task in agents] == ["codex", "gemini", "claude"]


def test_route_registry_randomize_does_not_mutate_stored_agents(tmp_path, monkeypatch):
    """Repeated `resolve_match` calls must not reorder the stored rule.agents
    list. Without copy-then-shuffle, the registry would drift on each event.
    """
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: multi-agent
            randomize: true
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "review"
              - agent: "gemini"
                task: "review"
              - agent: "codex"
                task: "review"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)
    monkeypatch.setattr(
        "app.services.routes.random.shuffle",
        lambda seq: seq.reverse(),
    )

    # The registry holds the parsed RouteRule list internally; reach in to
    # snapshot the original agent order before any resolve call.
    stored_rule = registry._rules[0]
    original_order = [task.agent for task in stored_rule.agents]

    for _ in range(3):
        registry.resolve(
            event_name="Issue Hook",
            action="open",
            author="alice",
            labels=[],
            mentions=[],
        )

    assert [task.agent for task in stored_rule.agents] == original_order


def test_route_registry_randomize_noop_on_single_agent_route(tmp_path):
    """`randomize: true` on a single-agent route is a no-op (and logs a warning
    at load time, exercised by a separate test)."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: solo-agent
            randomize: true
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
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
    assert [task.agent for task in agents] == ["claude"]


def test_route_registry_randomize_warns_on_single_agent_route(tmp_path, caplog):
    """Load-time warning when `randomize: true` is set on a route with <= 1
    agent so operators discover misconfiguration without checking dispatch
    logs."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: solo-agent
            randomize: true
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "review"
        """,
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="app.services.routes"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)

    assert any(
        "solo-agent" in record.message and "randomize" in record.message
        for record in caplog.records
    )


def test_route_registry_raises_on_non_boolean_randomize(tmp_path):
    """A non-boolean `randomize` value raises ValueError naming the route.

    YAML truthy strings like ``"yes"`` are rejected on purpose so a config
    typo fails loudly rather than silently behaving as ``False``.
    """
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: bad-randomize
            randomize: "yes"
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "review"
              - agent: "gemini"
                task: "review"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bad-randomize"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)


def test_route_registry_raises_on_empty_author_list_entry(tmp_path):
    """A list `author` with an empty-string entry is rejected for the same reason."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: empty-author-list-entry
            match:
              event: "Issue Hook"
              author: ["alice", ""]
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="empty-author-list-entry"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)


# ---------------------------------------------------------------------------
# action-as-list support (issue #31)
# ---------------------------------------------------------------------------


def test_route_registry_action_scalar_matches(tmp_path):
    """Scalar `action` string matches the configured action (regression)."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: issue-open
            match:
              event: "Issue Hook"
              action: "open"
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

    # A different action must not match.
    agents = registry.resolve(
        event_name="Issue Hook",
        action="update",
        author="alice",
        labels=[],
        mentions=[],
    )
    assert agents == []


def test_route_registry_action_list_matches_any_member(tmp_path):
    """List `action` matches when the event action is one of the entries."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: assign-issue
            match:
              event: "Issue Hook"
              action: ["open", "update"]
            agents:
              - agent: "claude"
                task: "assign_work"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    for action in ("open", "update"):
        agents = registry.resolve(
            event_name="Issue Hook",
            action=action,
            author="alice",
            labels=[],
            mentions=[],
        )
        assert len(agents) == 1, f"expected match for action={action}"


def test_route_registry_action_list_rejects_unlisted(tmp_path):
    """List `action` rejects an action not in the list."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: assign-issue
            match:
              event: "Issue Hook"
              action: ["open", "update"]
            agents:
              - agent: "claude"
                task: "assign_work"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    agents = registry.resolve(
        event_name="Issue Hook",
        action="close",
        author="alice",
        labels=[],
        mentions=[],
    )
    assert agents == []


def test_route_registry_action_missing_matches_any(tmp_path):
    """Missing `action` matches any event action (unchanged behavior)."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: any-issue
            match:
              event: "Issue Hook"
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    for action in ("open", "update", "close", None):
        agents = registry.resolve(
            event_name="Issue Hook",
            action=action,
            author="alice",
            labels=[],
            mentions=[],
        )
        assert len(agents) == 1, f"expected match for action={action!r}"


def test_route_registry_action_empty_list_matches_any(tmp_path):
    """Explicit empty-list `action` is equivalent to omitted `action`."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: any-issue-explicit
            match:
              event: "Issue Hook"
              action: []
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    registry = RouteRegistry(str(routes_yaml), reload_on_change=False)

    for action in ("open", "update", None):
        agents = registry.resolve(
            event_name="Issue Hook",
            action=action,
            author="alice",
            labels=[],
            mentions=[],
        )
        assert len(agents) == 1, f"expected match for action={action!r}"


def test_route_registry_raises_on_invalid_action_type(tmp_path):
    """A scalar non-string `action` raises ValueError naming the route."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: bad-action-scalar
            match:
              event: "Issue Hook"
              action: 123
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bad-action-scalar"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)


def test_route_registry_raises_on_invalid_action_list_entry(tmp_path):
    """A list `action` with non-string entries raises ValueError naming the route."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: bad-action-list
            match:
              event: "Issue Hook"
              action: ["open", 5]
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bad-action-list"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)


def test_route_registry_raises_on_empty_action_string(tmp_path):
    """Scalar `action: ""` is rejected because it almost always indicates a typo."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: empty-action-scalar
            match:
              event: "Issue Hook"
              action: ""
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="empty-action-scalar"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)


def test_route_registry_raises_on_empty_action_list_entry(tmp_path):
    """A list `action` with an empty-string entry is rejected for the same reason."""
    routes_yaml = tmp_path / "routes.yaml"
    routes_yaml.write_text(
        """
        routes:
          - name: empty-action-list-entry
            match:
              event: "Issue Hook"
              action: ["open", ""]
            agents:
              - agent: "claude"
                task: "triage"
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="empty-action-list-entry"):
        RouteRegistry(str(routes_yaml), reload_on_change=False)


# ---------------------------------------------------------------------------
# Shipped-config precedence: assign-on-issue-creation (issue #31)
# ---------------------------------------------------------------------------


def _shipped_registry():
    """Build a RouteRegistry over the real shipped config/routes.yaml."""
    return RouteRegistry(
        str(SHIPPED_ROUTES_YAML),
        reload_on_change=False,
        model_variables={
            "CLAUDE_MODEL": "claude-model",
            "GEMINI_MODEL": "Gemini 3.1 Pro (High)",
            "CODEX_MODEL": "codex-model",
        },
    )


def test_shipped_config_assign_issue_wins_on_create():
    """An Issue Hook/open with an agent assignee must resolve the read-write
    assign route, NOT issue-triage.

    Parser-only and mocked-resolver tests can both pass while a later YAML
    reorder silently restores triage precedence, so this loads the shipped file
    and asserts on route order end-to-end (codex's review ask on issue #31).
    """
    registry = _shipped_registry()

    match = registry.resolve_match(
        event_name="Issue Hook",
        action="open",
        author="your-username",
        labels=[],
        mentions=[],
        assignees=["claude"],
    )
    assert match is not None
    assert match.rule.name == "assign-issue-claude"
    assert match.rule.access == "readwrite"
    assert [a.task for a in match.agents] == ["assign_work"]


def test_shipped_config_open_without_assignee_still_triages():
    """An Issue Hook/open with no agent assignee falls through to issue-triage."""
    registry = _shipped_registry()

    match = registry.resolve_match(
        event_name="Issue Hook",
        action="open",
        author="your-username",
        labels=[],
        mentions=[],
        assignees=[],
    )
    assert match is not None
    assert match.rule.name == "issue-triage"
    assert match.rule.access == "readonly"


def test_shipped_config_assign_issue_still_matches_update():
    """The /assign-on-existing-issue path (action=update) still resolves the
    assign route after the open+update change."""
    registry = _shipped_registry()

    match = registry.resolve_match(
        event_name="Issue Hook",
        action="update",
        author="your-username",
        labels=[],
        mentions=[],
        assignees=["claude"],
    )
    assert match is not None
    assert match.rule.name == "assign-issue-claude"


# ---------------------------------------------------------------------------
# OpenCode "commented, but reversible" guard
# ---------------------------------------------------------------------------
#
# The shipped `config/routes.yaml` documents an optional `opencode-kimi` agent
# by embedding every OpenCode line as a column-0 `#` comment. The MR
# description promises two things:
#   (1) the shipped file exposes only claude/gemini/codex, and
#   (2) stripping the leading `#` from those blocks yields valid YAML that
#       wires opencode-kimi across all six route families.
#
# These tests lock both directions so a future edit that breaks the
# reversibility (e.g. accidental re-indent, stray non-column-0 `#`, or an
# uncomment-and-forget-to-re-comment) fails loudly.

SHIPPED_ROUTES_YAML = Path(__file__).resolve().parents[1] / "config" / "routes.yaml"

# A commented YAML line looks like `#      - agent: ...` or `#  - name: ...`:
# column-0 `#` immediately followed by 2+ spaces of YAML indent. In-body
# prose comments in the file use a single space (`# OpenCode ...`), so
# within the routes body this pattern is unambiguous. Header prose above
# the `routes:` line uses multi-space indented `#   - ...` bullets for
# access-mode docs, so we scope the transform to the routes body only.
_COMMENTED_YAML_LINE = re.compile(r"^#(?=  )")


def _uncomment_optional_agent_blocks(text: str) -> str:
    """Strip the leading `#` from column-0 commented YAML lines in the body.

    The transform is intentionally scoped to lines at/after the top-level
    ``routes:`` marker so header prose bullets (which also start with
    ``#   -``) are untouched.

    Note this enables *every* optional harness shipped commented-out (opencode
    and goose today), not just one -- which is the point: it proves the shipped
    file is uncomment-reversible as a whole.
    """

    lines = text.splitlines()
    in_body = False
    out: list[str] = []
    for line in lines:
        if not in_body:
            out.append(line)
            if line.rstrip() == "routes:":
                in_body = True
            continue
        out.append(_COMMENTED_YAML_LINE.sub("", line))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def test_shipped_routes_yaml_does_not_expose_opencode():
    """Baseline guard: the shipped config must not wire opencode-kimi.

    OpenCode is optional and disabled by default. If this test starts failing
    because the shipped file now exposes opencode-kimi, the "disabled by
    default" promise in the MR description is broken -- either intentionally
    (in which case update the test) or by accident.
    """

    raw = yaml.safe_load(SHIPPED_ROUTES_YAML.read_text(encoding="utf-8"))
    routes = raw.get("routes", [])

    for rule in routes:
        assert "opencode" not in (rule.get("name") or "").lower(), (
            f"shipped routes.yaml exposes an opencode route: {rule.get('name')}"
        )
        for agent in rule.get("agents", []):
            assert "opencode" not in (agent.get("agent") or "").lower(), (
                f"shipped routes.yaml wires opencode agent in route "
                f"{rule.get('name')!r}: {agent.get('agent')}"
            )


def test_shipped_routes_yaml_uncomments_to_valid_opencode_config(tmp_path):
    """Reversibility guard: stripping column-0 `#` restores OpenCode wiring.

    Every OpenCode block ships behind a column-0 `#`; the MR description
    promises stripping it yields valid YAML that RouteRegistry accepts and
    wires opencode-kimi across all six route families (2 review + 2
    assignment + 1 mention + 1 already-present agent-in-issue-triage; the
    default-merge-request review makes six). Confirm both parse and wire.
    """

    original = SHIPPED_ROUTES_YAML.read_text(encoding="utf-8")
    uncommented = _uncomment_optional_agent_blocks(original)

    # Sanity: the transform actually did something (at least one commented
    # YAML line existed). Guards against a future edit that accidentally
    # removes the OpenCode blocks entirely while leaving the test in place.
    assert uncommented != original, (
        "no column-0 commented YAML lines found in shipped routes.yaml -- "
        "did the OpenCode blocks get removed?"
    )

    patched = tmp_path / "routes.yaml"
    patched.write_text(uncommented, encoding="utf-8")

    registry = RouteRegistry(
        str(patched),
        reload_on_change=False,
        model_variables={
            "CLAUDE_MODEL": "claude-model",
            "GEMINI_MODEL": "Gemini 3.1 Pro (High)",
            "CODEX_MODEL": "codex-model",
            "OPENCODE_KIMI_MODEL": "openrouter/moonshotai/kimi-k2.6",
            "GOOSE_GEMMA_MODEL": "local-llama-cpp",
            "GROK_MODEL": "grok-4.5",
            "PI_NEMOTRON_MODEL": "nvidia/nemotron-3-ultra-550b-a55b",
        },
    )

    raw = yaml.safe_load(patched.read_text(encoding="utf-8"))
    rule_names = [r.get("name") for r in raw.get("routes", [])]

    # Standalone OpenCode routes across all three families (assign-issue,
    # assign-mr, mention).
    for expected in (
        "assign-issue-opencode-kimi",
        "assign-mr-opencode-kimi",
        "mention-opencode-kimi",
    ):
        assert expected in rule_names, (
            f"uncommented routes.yaml is missing OpenCode route {expected!r}"
        )

    # Multi-agent review routes gain an opencode-kimi entry alongside the
    # existing claude/gemini/codex agents.
    for review_route in ("issue-triage", "default-merge-request"):
        route = next(r for r in raw.get("routes", []) if r.get("name") == review_route)
        agents = [a.get("agent") for a in route.get("agents", [])]
        assert "opencode-kimi" in agents, (
            f"uncommented {review_route} does not wire opencode-kimi; got {agents}"
        )

    # Registry resolves the opencode-kimi mention route -- confirms the
    # ${OPENCODE_KIMI_MODEL} placeholder substituted correctly and the
    # readonly access mode parsed.
    mention_agents = registry.resolve(
        event_name="Note Hook",
        action="create",
        author="your-username",
        labels=[],
        mentions=["opencode-kimi"],
    )
    assert [a.agent for a in mention_agents] == ["opencode-kimi"], (
        f"mention-opencode-kimi did not resolve after uncomment; got {mention_agents}"
    )


def test_shipped_routes_yaml_does_not_expose_goose():
    """Baseline guard: the shipped config must not wire goose (issue #13).

    Goose is optional and disabled by default -- enabling it requires a host
    Goose config to bind-mount, so a shipped-enabled route would hard-fail the
    preflight for every operator who has never installed Goose.
    """

    raw = yaml.safe_load(SHIPPED_ROUTES_YAML.read_text(encoding="utf-8"))

    for rule in raw.get("routes", []):
        assert "goose" not in (rule.get("name") or "").lower(), (
            f"shipped routes.yaml exposes a goose route: {rule.get('name')}"
        )
        for agent in rule.get("agents", []):
            assert "goose" not in (agent.get("agent") or "").lower(), (
                f"shipped routes.yaml wires goose in route {rule.get('name')!r}"
            )


def test_shipped_routes_yaml_uncomments_to_valid_goose_config(tmp_path):
    """Reversibility guard: stripping column-0 `#` restores Goose wiring.

    Also pins the two CLI contracts that are easy to get wrong, and that every
    reviewer on issue #13 initially got wrong:

    1. Goose reads the prompt from stdin via `-i -`. `--text` takes a literal
       string, not `-`, so `--text -` would send Goose the prompt "-".
    2. There is NO `--provider` flag. The loader only substitutes ${VAR} in the
       argument immediately following `--model` (see _expand_model_placeholders),
       so a `--provider "${GOOSE_PROVIDER}"` pair would reach the CLI as the
       literal string `${GOOSE_PROVIDER}` and fail to resolve a provider. The
       provider comes from the mounted host config instead.
    """

    original = SHIPPED_ROUTES_YAML.read_text(encoding="utf-8")
    patched = tmp_path / "routes.yaml"
    patched.write_text(_uncomment_optional_agent_blocks(original), encoding="utf-8")

    registry = RouteRegistry(
        str(patched),
        reload_on_change=False,
        model_variables={
            "CLAUDE_MODEL": "claude-model",
            "GEMINI_MODEL": "Gemini 3.1 Pro (High)",
            "CODEX_MODEL": "codex-model",
            "OPENCODE_KIMI_MODEL": "openrouter/moonshotai/kimi-k2.6",
            "GOOSE_GEMMA_MODEL": "local-llama-cpp",
            "GROK_MODEL": "grok-4.5",
            "PI_NEMOTRON_MODEL": "nvidia/nemotron-3-ultra-550b-a55b",
        },
    )

    raw = yaml.safe_load(patched.read_text(encoding="utf-8"))
    rules = raw.get("routes", [])
    rule_names = [r.get("name") for r in rules]

    for expected in (
        "assign-issue-goose-gemma",
        "assign-mr-goose-gemma",
        "mention-goose-gemma",
    ):
        assert expected in rule_names, (
            f"uncommented routes.yaml is missing Goose route {expected!r}"
        )

    for review_route in ("issue-triage", "default-merge-request"):
        route = next(r for r in rules if r.get("name") == review_route)
        agents = [a.get("agent") for a in route.get("agents", [])]
        assert "goose-gemma" in agents, (
            f"uncommented {review_route} does not wire goose; got {agents}"
        )

    # The trigger is the GitLab username; the agent slug carries the harness
    # prefix. If these are ever collapsed into one string, either the mention
    # stops matching a real GitLab user or the agent stops resolving its
    # GOOSE_GEMMA_* credentials -- and the harness silently never fires.
    for rule_name in ("assign-issue-goose-gemma", "assign-mr-goose-gemma"):
        rule = next(r for r in rules if r.get("name") == rule_name)
        assert rule["match"]["assignees"] == ["gemma"], (
            f"{rule_name} must trigger on the GitLab username 'gemma', not the "
            f"agent slug: {rule['match']['assignees']}"
        )
    mention_rule = next(r for r in rules if r.get("name") == "mention-goose-gemma")
    assert mention_rule["match"]["mentions"] == ["gemma"], (
        f"mention-goose-gemma must trigger on the GitLab username 'gemma', not "
        f"the agent slug: {mention_rule['match']['mentions']}"
    )

    for rule in rules:
        for agent in rule.get("agents", []):
            if agent.get("agent") != "goose-gemma":
                continue
            args = agent.get("options", {}).get("args", [])
            assert agent["options"]["command"] == "goose"
            assert args[-2:] == ["-i", "-"], (
                f"goose in route {rule['name']!r} must take the prompt on stdin "
                f"via a trailing '-i -'; got {args}"
            )
            assert "--text" not in args, (
                f"goose in route {rule['name']!r} uses '--text', which takes a "
                f"literal prompt string and does not read stdin: {args}"
            )
            assert "--provider" not in args, (
                f"goose in route {rule['name']!r} passes '--provider'; only the "
                f"argument after '--model' gets ${{VAR}} substitution, so this "
                f"reaches the CLI as a literal placeholder: {args}"
            )
            model_index = args.index("--model")
            assert args[model_index + 1] == "${GOOSE_GEMMA_MODEL}", (
                f"goose in route {rule['name']!r} needs '--model' followed by "
                f"the '${{GOOSE_GEMMA_MODEL}}' placeholder: {args}"
            )

    # Registry resolves the mention route -- confirms ${GOOSE_GEMMA_MODEL}
    # substituted and the readonly access mode parsed. Note the mention is the
    # GitLab username `gemma`, NOT the agent slug `goose-gemma`: routes match on
    # the username in the webhook payload, and the agent slug only selects which
    # GOOSE_GEMMA_* credentials the dispatch runs under (same split as
    # opencode-kimi / `kimi`). Mentioning `@goose-gemma` matches nothing.
    mention_agents = registry.resolve(
        event_name="Note Hook",
        action="create",
        author="your-username",
        labels=[],
        mentions=["gemma"],
    )
    assert [a.agent for a in mention_agents] == ["goose-gemma"], (
        f"mention-goose-gemma did not resolve after uncomment; got {mention_agents}"
    )
    assert "${GOOSE_GEMMA_MODEL}" not in mention_agents[0].options["args"], (
        "the ${GOOSE_GEMMA_MODEL} placeholder survived into the dispatched argv"
    )


def test_shipped_routes_yaml_does_not_expose_grok():
    """Baseline guard: the shipped config must not wire grok (issue #18).

    Grok Build is optional and disabled by default. Enabling it requires a Grok
    GitLab account and a host `grok login`, so a shipped-enabled route would
    hard-fail the preflight for every operator who has neither.
    """

    raw = yaml.safe_load(SHIPPED_ROUTES_YAML.read_text(encoding="utf-8"))

    for rule in raw.get("routes", []):
        assert "grok" not in (rule.get("name") or "").lower(), (
            f"shipped routes.yaml exposes a grok route: {rule.get('name')}"
        )
        for agent in rule.get("agents", []):
            assert "grok" not in (agent.get("agent") or "").lower(), (
                f"shipped routes.yaml wires grok in route {rule.get('name')!r}"
            )


def test_shipped_routes_yaml_uncomments_to_valid_grok_config(tmp_path):
    """Reversibility guard: stripping column-0 `#` restores Grok wiring.

    Also pins the prompt-transport contract, which is the one thing about this
    harness that looks wrong and is right.

    Grok does not read stdin: `-p` / `--single` takes the prompt as the flag's
    value, so the routes use the ${PROMPT} argv placeholder, exactly like `agy`.
    The tempting alternative is `--prompt-file /dev/stdin`, which puts the prompt
    back on the unbounded stdin path -- and it genuinely works from a shell
    (`grok --prompt-file /dev/stdin < prompt.txt`). It does *not* work under the
    dispatcher: the child gets a pipe (not a file), the parent closes the write
    end as soon as the prompt is written, and grok re-opens fd 0 **by path** --
    reopening a pipe that has no writers left fails with ENXIO ("Failed to read
    '/dev/stdin': No such device or address"). Verified against the live
    container: every dispatch failed this way.

    So this test pins `-p ${PROMPT}` and explicitly rejects `--prompt-file`.
    """

    original = SHIPPED_ROUTES_YAML.read_text(encoding="utf-8")
    patched = tmp_path / "routes.yaml"
    patched.write_text(_uncomment_optional_agent_blocks(original), encoding="utf-8")

    registry = RouteRegistry(
        str(patched),
        reload_on_change=False,
        model_variables={
            "CLAUDE_MODEL": "claude-model",
            "GEMINI_MODEL": "Gemini 3.1 Pro (High)",
            "CODEX_MODEL": "codex-model",
            "OPENCODE_KIMI_MODEL": "openrouter/moonshotai/kimi-k2.6",
            "GOOSE_GEMMA_MODEL": "local-llama-cpp",
            "GROK_MODEL": "grok-4.5",
            "PI_NEMOTRON_MODEL": "nvidia/nemotron-3-ultra-550b-a55b",
        },
    )

    raw = yaml.safe_load(patched.read_text(encoding="utf-8"))
    rules = raw.get("routes", [])
    rule_names = [r.get("name") for r in rules]

    for expected in ("assign-issue-grok", "assign-mr-grok", "mention-grok"):
        assert expected in rule_names, (
            f"uncommented routes.yaml is missing Grok route {expected!r}"
        )

    for review_route in ("issue-triage", "default-merge-request"):
        route = next(r for r in rules if r.get("name") == review_route)
        agents = [a.get("agent") for a in route.get("agents", [])]
        assert "grok" in agents, (
            f"uncommented {review_route} does not wire grok; got {agents}"
        )

    # Unlike opencode-* and goose-*, one grok binary backs one logical agent, so
    # the GitLab username and the agent slug are the same string.
    for rule_name in ("assign-issue-grok", "assign-mr-grok"):
        rule = next(r for r in rules if r.get("name") == rule_name)
        assert rule["match"]["assignees"] == ["grok"]
    mention_rule = next(r for r in rules if r.get("name") == "mention-grok")
    assert mention_rule["match"]["mentions"] == ["grok"]

    for rule in rules:
        for agent in rule.get("agents", []):
            if agent.get("agent") != "grok":
                continue
            args = agent.get("options", {}).get("args", [])
            assert agent["options"]["command"] == "grok"
            assert args[-2:] == ["-p", PROMPT_ARG_PLACEHOLDER], (
                f"grok in route {rule['name']!r} must take the prompt as an argv "
                f"value via a trailing '-p {PROMPT_ARG_PLACEHOLDER}'; grok does "
                f"not read stdin: {args}"
            )
            assert "--prompt-file" not in args, (
                f"grok in route {rule['name']!r} uses '--prompt-file', but the "
                f"dispatcher has no prompt-file transport: it either writes the "
                f"prompt to stdin or substitutes it into argv. Pointing the flag at "
                f"/dev/stdin to reach the stdin path works from a shell and fails "
                f"under the dispatcher with ENXIO (grok re-opens fd 0 by path; the "
                f"pipe has no writer left). Use {PROMPT_ARG_PLACEHOLDER}: {args}"
            )
            assert "--always-approve" in args, (
                f"grok in route {rule['name']!r} must auto-approve tool use -- an "
                f"unattended run has no terminal to answer a prompt: {args}"
            )
            # Not a version pin: install-grok.sh still fetches latest every boot.
            # This stops the *running agent* from updating itself into the
            # bind-mounted ~/.grok, where it would leave a ~150 MB binary and
            # repoint the symlink the operator's host grok resolves through.
            assert "--no-auto-update" in args, (
                f"grok in route {rule['name']!r} omits '--no-auto-update'; a "
                f"container-side self-update writes into the host's ~/.grok: {args}"
            )
            model_index = args.index("--model")
            assert args[model_index + 1] == "${GROK_MODEL}", (
                f"grok in route {rule['name']!r} needs '--model' followed by the "
                f"'${{GROK_MODEL}}' placeholder: {args}"
            )

    # Every route that dispatches grok must widen the inactivity watchdog, or
    # stream output continuously. In the default `plain` format grok prints
    # nothing until the run ends, and only stdout resets the timer -- so on the
    # shared review routes (issue-triage, default-merge-request) the route-level
    # override has to be uncommented alongside the agent entry, or a long, quiet
    # review is killed at the 900s default.
    for rule in rules:
        agents = [a.get("agent") for a in rule.get("agents", [])]
        if "grok" not in agents:
            continue
        grok_entry = next(a for a in rule["agents"] if a.get("agent") == "grok")
        streams = "streaming-json" in grok_entry.get("options", {}).get("args", [])
        assert rule.get("max_inactivity_seconds") == 1800 or streams, (
            f"route {rule['name']!r} dispatches grok but neither raises "
            f"max_inactivity_seconds to 1800 nor streams output; grok's default "
            f"`plain` format emits no stdout until the run ends, so the 900s "
            f"watchdog default would kill a long review mid-run"
        )

    mention_agents = registry.resolve(
        event_name="Note Hook",
        action="create",
        author="your-username",
        labels=[],
        mentions=["grok"],
    )
    assert [a.agent for a in mention_agents] == ["grok"], (
        f"mention-grok did not resolve after uncomment; got {mention_agents}"
    )
    assert "${GROK_MODEL}" not in mention_agents[0].options["args"], (
        "the ${GROK_MODEL} placeholder survived into the dispatched argv"
    )


def test_shipped_routes_yaml_does_not_expose_pi():
    """Baseline guard: the shipped config must not wire pi (issue #32).

    Pi is optional and disabled by default. Enabling it requires a host
    `~/.pi/agent` config to bind-mount and reintroduces a user-local Node
    runtime at boot, so a shipped-enabled route would hard-fail the preflight
    for every operator who has never installed Pi.
    """

    raw = yaml.safe_load(SHIPPED_ROUTES_YAML.read_text(encoding="utf-8"))

    for rule in raw.get("routes", []):
        assert "pi-nemotron" not in (rule.get("name") or "").lower(), (
            f"shipped routes.yaml exposes a pi route: {rule.get('name')}"
        )
        for agent in rule.get("agents", []):
            assert not (agent.get("agent") or "").lower().startswith("pi-"), (
                f"shipped routes.yaml wires a pi agent in route "
                f"{rule.get('name')!r}: {agent.get('agent')}"
            )


def test_shipped_routes_yaml_uncomments_to_valid_pi_config(tmp_path):
    """Reversibility guard: stripping column-0 `#` restores Pi wiring.

    Unlike opencode/goose/grok, the issue scopes Pi to a *single* commented
    mention route as an example, so this test only expects `mention-pi-nemotron`
    -- there are deliberately no assign/review Pi routes to find.

    Also pins the prompt-transport contract: Pi print mode (`-p`) reads the
    prompt from stdin, so the route carries NO ${PROMPT} argv placeholder. If a
    future edit adds one, the dispatcher would stop piping the prompt and Pi
    would run against an empty initial prompt. `--no-session` and `--no-approve`
    are pinned too: the former keeps webhook runs from writing session state into
    the bind-mounted ~/.pi/agent, the latter stops an untrusted repo from opting
    itself into executable Pi extensions.
    """

    original = SHIPPED_ROUTES_YAML.read_text(encoding="utf-8")
    patched = tmp_path / "routes.yaml"
    patched.write_text(_uncomment_optional_agent_blocks(original), encoding="utf-8")

    registry = RouteRegistry(
        str(patched),
        reload_on_change=False,
        model_variables={
            "CLAUDE_MODEL": "claude-model",
            "GEMINI_MODEL": "Gemini 3.1 Pro (High)",
            "CODEX_MODEL": "codex-model",
            "OPENCODE_KIMI_MODEL": "openrouter/moonshotai/kimi-k2.6",
            "GOOSE_GEMMA_MODEL": "local-llama-cpp",
            "GROK_MODEL": "grok-4.5",
            "PI_NEMOTRON_MODEL": "nvidia/nemotron-3-ultra-550b-a55b",
        },
    )

    raw = yaml.safe_load(patched.read_text(encoding="utf-8"))
    rules = raw.get("routes", [])
    rule_names = [r.get("name") for r in rules]

    assert "mention-pi-nemotron" in rule_names, (
        "uncommented routes.yaml is missing Pi route 'mention-pi-nemotron'"
    )

    # The trigger is the GitLab username; the agent slug carries the harness
    # prefix (same split as goose-gemma / `gemma`). If these are ever collapsed,
    # either the mention stops matching a real GitLab user or the agent stops
    # resolving its PI_NEMOTRON_* credentials.
    mention_rule = next(r for r in rules if r.get("name") == "mention-pi-nemotron")
    assert mention_rule["match"]["mentions"] == ["nemotron"], (
        f"mention-pi-nemotron must trigger on the GitLab username 'nemotron', "
        f"not the agent slug: {mention_rule['match']['mentions']}"
    )

    for rule in rules:
        for agent in rule.get("agents", []):
            if agent.get("agent") != "pi-nemotron":
                continue
            args = agent.get("options", {}).get("args", [])
            assert agent["options"]["command"] == "pi"
            # Pi reads the prompt on stdin -- no ${PROMPT} argv placeholder.
            assert PROMPT_ARG_PLACEHOLDER not in args, (
                f"pi in route {rule['name']!r} carries a {PROMPT_ARG_PLACEHOLDER} "
                f"placeholder, but Pi print mode reads the prompt from stdin; the "
                f"placeholder would suppress the piped prompt: {args}"
            )
            assert "--no-session" in args, (
                f"pi in route {rule['name']!r} omits '--no-session'; webhook runs "
                f"would accumulate session state under the mounted ~/.pi/agent: {args}"
            )
            assert "--no-approve" in args, (
                f"pi in route {rule['name']!r} omits '--no-approve'; an untrusted "
                f"repo could opt itself into executable Pi extensions: {args}"
            )
            model_index = args.index("--model")
            assert args[model_index + 1] == "${PI_NEMOTRON_MODEL}", (
                f"pi in route {rule['name']!r} needs '--model' followed by the "
                f"'${{PI_NEMOTRON_MODEL}}' placeholder: {args}"
            )

    # A 550B model can prefill silently and Pi only resets the watchdog on
    # stdout, so the mention route must widen the inactivity window past 900s.
    assert mention_rule.get("max_inactivity_seconds") == 1800, (
        "mention-pi-nemotron must raise max_inactivity_seconds to 1800; a large "
        "model can go quiet during prefill and the 900s default would kill it"
    )

    # Registry resolves the mention route -- confirms ${PI_NEMOTRON_MODEL}
    # substituted and the readonly access mode parsed. The mention is the GitLab
    # username `nemotron`, NOT the agent slug `pi-nemotron`.
    mention_agents = registry.resolve(
        event_name="Note Hook",
        action="create",
        author="your-username",
        labels=[],
        mentions=["nemotron"],
    )
    assert [a.agent for a in mention_agents] == ["pi-nemotron"], (
        f"mention-pi-nemotron did not resolve after uncomment; got {mention_agents}"
    )
    assert "${PI_NEMOTRON_MODEL}" not in mention_agents[0].options["args"], (
        "the ${PI_NEMOTRON_MODEL} placeholder survived into the dispatched argv"
    )
