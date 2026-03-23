"""Robot Dev Team Project
File: tests/test_webhooks.py
Description: Pytest coverage for webhook integrations.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import webhooks
from app.core.config import settings
from app.main import app
from app.services.agents import AgentKilledError
from app.services.routes import AgentTask, RouteMatch


class DummyDeduplicator:
    def __init__(self, should_process=True):
        self.should_process = should_process

    async def mark(self, _key):
        return self.should_process


def setup_common_patches(monkeypatch, should_process=True):
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "top-secret")
    monkeypatch.setattr(webhooks, "_DEDUP", DummyDeduplicator(should_process))

    default_rule = SimpleNamespace(name="default-route", mentions=[], assignees=[], access="readonly")

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        assert event_name == "Merge Request Hook"
        if mentions or (rule_predicate and not rule_predicate(default_rule)):
            return None
        agents = [AgentTask(agent="claude", task="review", prompt="review.txt", options={})]
        return RouteMatch(rule=default_rule, agents=agents)

    monkeypatch.setattr(webhooks, "_ROUTES", _make_dummy_routes(resolver))
    _patch_build_context(monkeypatch)
    _patch_dispatch(monkeypatch)
    _patch_trigger_queue(monkeypatch)


def _make_dummy_routes(resolver):
    class DummyRoutes:
        def resolve(self, *args, **kwargs):
            match = self.resolve_match(*args, **kwargs)
            return match.agents if match else []

        def resolve_match(self, event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
            return resolver(event_name, action, author, labels, mentions, body=body, assignees=assignees, rule_predicate=rule_predicate)

    return DummyRoutes()


def _patch_build_context(monkeypatch):
    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)


def _patch_dispatch(monkeypatch):
    async def fake_dispatch(event_uuid, tasks, context):
        assert context["title"] == "Dummy"
        return [
            {
                "agent": task.agent,
                "status": "ok",
                "event_id": event_uuid,
            }
            for task in tasks
        ]

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch)


def _patch_trigger_queue(monkeypatch):
    class ImmediateQueue:
        async def enqueue_many(self, items):
            results = []
            for item in items:
                try:
                    agents = await item.handler()
                except AgentKilledError as exc:
                    result = {
                        "status": "killed",
                        "event_id": item.event_id,
                        "base_event_uuid": item.base_event_uuid,
                        "event": item.event_name,
                        "action": item.action,
                        "mentions": item.mentions,
                        "route": item.route_name,
                        "error": str(exc),
                        "agents": [],
                    }
                    if not item.future.done():
                        item.future.set_result(result)
                    results.append(result)
                    continue
                result = {
                    "status": "ok",
                    "event_id": item.event_id,
                    "base_event_uuid": item.base_event_uuid,
                    "event": item.event_name,
                    "action": item.action,
                    "mentions": item.mentions,
                    "route": item.route_name,
                    "agents": agents,
                }
                if not item.future.done():
                    item.future.set_result(result)
                results.append(result)
            return results

    monkeypatch.setattr(webhooks, "_TRIGGER_QUEUE", ImmediateQueue())


@pytest.mark.asyncio
async def test_webhook_happy_path(monkeypatch):
    setup_common_patches(monkeypatch, should_process=True)

    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "open"},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-123",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["agents"] == [
        {"agent": "claude", "status": "ok", "event_id": "uuid-123"}
    ]
    assert len(data["triggers"]) == 1
    assert data["triggers"][0]["route"] == "default-route"


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_secret(monkeypatch):
    setup_common_patches(monkeypatch, should_process=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json={"object_kind": "merge_request"},
            headers={
                "X-Gitlab-Token": "wrong",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-123",
            },
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_skips_duplicates(monkeypatch):
    setup_common_patches(monkeypatch, should_process=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json={"object_kind": "merge_request"},
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-123",
            },
        )

    assert response.status_code == 200
    assert response.json()["reason"] == "duplicate"


@pytest.mark.asyncio
async def test_webhook_splits_multiple_mentions(monkeypatch):
    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        mappings = {
            tuple(): None,
            ("alice",): SimpleNamespace(name="alice-route", mentions=["alice"], assignees=[], access="readonly"),
            ("bob",): SimpleNamespace(name="bob-route", mentions=["bob"], assignees=[], access="readonly"),
        }
        rule = mappings.get(tuple(mentions))
        if rule is None:
            return None
        if rule_predicate and not rule_predicate(rule):
            return None
        agents = [AgentTask(agent=f"agent-{rule.name}", task="review", prompt=None, options={})]
        return RouteMatch(rule=rule, agents=agents)

    setup_common_patches(monkeypatch)

    def resolve(*args, **kwargs):
        match = resolver(*args, **kwargs)
        return match.agents if match else []

    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)
    monkeypatch.setattr(webhooks._ROUTES, "resolve", resolve)

    payload = {
        "object_kind": "merge_request",
        "object_attributes": {
            "action": "open",
            "description": "@alice thanks! @bob please review",
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-456",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert len(data["triggers"]) == 2
    routes = {trigger["route"] for trigger in data["triggers"]}
    assert routes == {"alice-route", "bob-route"}
    mentions = {tuple(trigger["mentions"]) for trigger in data["triggers"]}
    assert mentions == {("alice",), ("bob",)}
    assert {agent["agent"] for agent in data["agents"]} == {"agent-alice-route", "agent-bob-route"}


def test_parse_mentions_strips_trailing_period():
    text = "Update acknowledged, @codex. Please proceed."
    assert webhooks._parse_mentions_from_text(text) == ["codex"]


class TestExtractAssignees:
    """Tests for the _extract_assignees() helper function."""

    def test_unassign_returns_empty_when_changes_current_is_empty(self):
        """When changes.assignees.current is empty (unassign), return []
        even if top-level payload.assignees still contains the agent."""
        payload = {
            "assignees": [{"username": "claude"}],
            "changes": {
                "assignees": {
                    "previous": [{"username": "claude"}],
                    "current": [],
                }
            },
        }
        assert webhooks._extract_assignees(payload) == []

    def test_prefers_changes_over_top_level(self):
        """When both sources exist, only changes.assignees.current is used."""
        payload = {
            "assignees": [{"username": "claude"}, {"username": "gemini"}],
            "changes": {
                "assignees": {
                    "previous": [{"username": "claude"}],
                    "current": [{"username": "gemini"}],
                }
            },
        }
        result = webhooks._extract_assignees(payload)
        assert result == ["gemini"]

    def test_fallback_when_no_changes(self):
        """When changes.assignees is absent, falls back to payload.assignees."""
        payload = {
            "assignees": [{"username": "claude"}, {"username": "gemini"}],
        }
        result = webhooks._extract_assignees(payload)
        assert result == ["claude", "gemini"]

    def test_fallback_when_changes_block_has_no_assignees(self):
        """When changes block exists but has no assignees key, uses fallback."""
        payload = {
            "assignees": [{"username": "codex"}],
            "changes": {"labels": {"previous": [], "current": []}},
        }
        result = webhooks._extract_assignees(payload)
        assert result == ["codex"]

    def test_returns_empty_when_no_sources(self):
        """Returns empty list when neither source provides assignees."""
        payload = {"object_kind": "merge_request"}
        assert webhooks._extract_assignees(payload) == []

    def test_handles_string_usernames(self):
        """Handles assignees provided as plain strings."""
        payload = {
            "changes": {
                "assignees": {
                    "previous": [],
                    "current": ["claude"],
                }
            },
        }
        result = webhooks._extract_assignees(payload)
        assert result == ["claude"]

    def test_handles_none_in_current(self):
        """Handles None value in changes.assignees.current gracefully."""
        payload = {
            "assignees": [{"username": "claude"}],
            "changes": {
                "assignees": {
                    "previous": [{"username": "claude"}],
                    "current": None,
                }
            },
        }
        # changes.assignees block is present, so top-level is NOT used
        result = webhooks._extract_assignees(payload)
        assert result == []

    def test_assign_event_returns_new_assignees(self):
        """When an agent is assigned, changes.assignees.current includes them."""
        payload = {
            "assignees": [],  # stale top-level
            "changes": {
                "assignees": {
                    "previous": [],
                    "current": [{"username": "claude"}],
                }
            },
        }
        result = webhooks._extract_assignees(payload)
        assert result == ["claude"]

    def test_deduplicates_changes_current(self):
        """Duplicate usernames in changes.assignees.current are deduplicated."""
        payload = {
            "changes": {
                "assignees": {
                    "previous": [],
                    "current": [
                        {"username": "claude"},
                        {"username": "claude"},
                        {"username": "gemini"},
                    ],
                }
            },
        }
        result = webhooks._extract_assignees(payload)
        assert result == ["claude", "gemini"]

    def test_deduplicates_fallback_assignees(self):
        """Duplicate usernames in top-level payload.assignees are deduplicated."""
        payload = {
            "assignees": [
                {"username": "claude"},
                {"username": "gemini"},
                {"username": "claude"},
            ],
        }
        result = webhooks._extract_assignees(payload)
        assert result == ["claude", "gemini"]


@pytest.mark.asyncio
async def test_unassign_webhook_does_not_retrigger_agent(monkeypatch):
    """An unassign event must NOT match assignment routes.

    Reproduces the bug from issue #63: auto-unassign fires a webhook
    where payload.assignees still lists the removed agent, but
    changes.assignees.current is empty.  The route resolver should
    receive an empty assignees list and therefore not match.
    """

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-mr-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="assign_work", prompt="assign_work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    # Simulate unassign webhook: payload.assignees still has claude (stale),
    # but changes.assignees.current is empty (authoritative post-change state)
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "update", "iid": 32},
        "user": {"username": "testuser"},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [{"username": "claude"}],
                "current": [],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-unassign-no-retrigger",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "no-routes"


class TestExtractNewlyAssignedAgent:
    """Tests for the _extract_newly_assigned_agent() helper function."""

    def test_detects_newly_assigned_agent(self, monkeypatch):
        """Detects when a known agent was newly assigned."""
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")

        payload = {
            "changes": {
                "assignees": {
                    "previous": [{"username": "human-user"}],
                    "current": [{"username": "human-user"}, {"username": "claude"}],
                }
            }
        }
        result = webhooks._extract_newly_assigned_agent(payload)
        assert result == "claude"

    def test_returns_none_when_no_agent_assigned(self, monkeypatch):
        """Returns None when a non-agent user is assigned."""
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")

        payload = {
            "changes": {
                "assignees": {
                    "previous": [],
                    "current": [{"username": "human-user"}],
                }
            }
        }
        result = webhooks._extract_newly_assigned_agent(payload)
        assert result is None

    def test_returns_none_when_no_changes(self, monkeypatch):
        """Returns None when payload has no changes.assignees."""
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")

        payload = {"object_kind": "issue"}
        result = webhooks._extract_newly_assigned_agent(payload)
        assert result is None

    def test_returns_none_when_agent_was_already_assigned(self, monkeypatch):
        """Returns None when agent was already in previous assignees."""
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")

        payload = {
            "changes": {
                "assignees": {
                    "previous": [{"username": "claude"}],
                    "current": [{"username": "claude"}, {"username": "human-user"}],
                }
            }
        }
        result = webhooks._extract_newly_assigned_agent(payload)
        assert result is None

    def test_detects_agent_case_insensitive(self, monkeypatch):
        """Agent detection is case-insensitive."""
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")

        payload = {
            "changes": {
                "assignees": {
                    "previous": [],
                    "current": [{"username": "Claude"}],
                }
            }
        }
        result = webhooks._extract_newly_assigned_agent(payload)
        assert result == "claude"

    def test_returns_none_on_unassign_event(self, monkeypatch):
        """Returns None when an agent is removed (unassigned)."""
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")

        payload = {
            "changes": {
                "assignees": {
                    "previous": [{"username": "claude"}],
                    "current": [],
                }
            }
        }
        result = webhooks._extract_newly_assigned_agent(payload)
        assert result is None

    def test_falls_back_to_top_level_assignees(self, monkeypatch):
        """Falls back to top-level assignees when changes.assignees is absent."""
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")

        payload = {
            "object_kind": "merge_request",
            "assignees": [{"username": "claude"}],
        }
        result = webhooks._extract_newly_assigned_agent(payload)
        assert result == "claude"

    def test_fallback_returns_none_for_non_agent(self, monkeypatch):
        """Fallback returns None when assignee is not a known agent."""
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")

        payload = {
            "object_kind": "merge_request",
            "assignees": [{"username": "human-user"}],
        }
        result = webhooks._extract_newly_assigned_agent(payload)
        assert result is None

    def test_fallback_returns_none_on_empty_assignees(self, monkeypatch):
        """Fallback returns None when top-level assignees is empty."""
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")

        payload = {
            "object_kind": "merge_request",
            "assignees": [],
        }
        result = webhooks._extract_newly_assigned_agent(payload)
        assert result is None

    def test_fallback_case_insensitive(self, monkeypatch):
        """Fallback detection is case-insensitive."""
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")

        payload = {
            "object_kind": "issue",
            "assignees": [{"username": "Claude"}],
        }
        result = webhooks._extract_newly_assigned_agent(payload)
        assert result == "claude"


@pytest.mark.asyncio
async def test_webhook_expands_all_mention(monkeypatch):
    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        mapping = {
            ("claude",): "claude-route",
            ("gemini",): "gemini-route",
            ("codex",): "codex-route",
        }
        route_name = mapping.get(tuple(mentions))
        if not route_name:
            return None

        rule = SimpleNamespace(name=route_name, mentions=mentions, assignees=[], access="readonly")
        if rule_predicate and not rule_predicate(rule):
            return None

        agents = [AgentTask(agent=f"agent-{route_name}", task="review")]
        return RouteMatch(rule=rule, agents=agents)

    setup_common_patches(monkeypatch)

    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    payload = {
        "object_kind": "note",
        "object_attributes": {
            "action": "create",
            "note": "Hello @All team",
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-all",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"

    assert len(data["triggers"]) == 3

    routes = {trigger["route"] for trigger in data["triggers"]}
    assert routes == {"claude-route", "gemini-route", "codex-route"}

    agents = {agent["agent"] for agent in data["agents"]}
    assert agents == {"agent-claude-route", "agent-gemini-route", "agent-codex-route"}


@pytest.mark.asyncio
async def test_webhook_expands_agents_alias(monkeypatch):
    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        mapping = {
            ("claude",): "claude-route",
            ("codex",): "codex-route",
        }
        route_name = mapping.get(tuple(mentions))
        if not route_name:
            return None

        rule = SimpleNamespace(name=route_name, mentions=mentions, assignees=[], access="readonly")
        if rule_predicate and not rule_predicate(rule):
            return None

        agents = [AgentTask(agent=f"agent-{route_name}", task="review")]
        return RouteMatch(rule=rule, agents=agents)

    setup_common_patches(monkeypatch)

    monkeypatch.setattr(settings, "all_mentions_agents", "claude,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    payload = {
        "object_kind": "note",
        "object_attributes": {
            "action": "create",
            "note": "Ping @Agents for help",
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-agents",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"

    assert len(data["triggers"]) == 2

    routes = {trigger["route"] for trigger in data["triggers"]}
    assert routes == {"claude-route", "codex-route"}

    agents = {agent["agent"] for agent in data["agents"]}
    assert agents == {"agent-claude-route", "agent-codex-route"}


@pytest.mark.asyncio
async def test_auto_unassign_on_successful_completion(monkeypatch):
    """Auto-unassign is called when enabled and agent completes successfully."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    # Override build_context to include project in context
    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-auto-unassign",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"

    mock_unassign.assert_called_once_with("namespace/project", 42, "issue", "claude")


@pytest.mark.asyncio
async def test_auto_unassign_on_successful_mr_completion(monkeypatch):
    """Auto-unassign is called for merge requests when enabled and agent completes successfully."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "update", "iid": 26},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-auto-unassign-mr",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"

    mock_unassign.assert_called_once_with("namespace/project", 26, "merge_request", "claude")


@pytest.mark.asyncio
async def test_auto_unassign_on_note_event(monkeypatch):
    """Auto-unassign resolves parent issue IID from a note event payload."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    payload = {
        "object_kind": "note",
        "object_attributes": {"action": "create", "note": "Working on it"},
        "issue": {"iid": 55},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-auto-unassign-note",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"

    mock_unassign.assert_called_once_with("namespace/project", 55, "issue", "claude")


@pytest.mark.asyncio
async def test_auto_unassign_mr_without_changes_assignees(monkeypatch):
    """Auto-unassign works via fallback when changes.assignees is absent from MR payload."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    # MR payload with top-level assignees but NO changes.assignees
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "update", "iid": 26},
        "assignees": [{"username": "claude"}],
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-auto-unassign-mr-fallback",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"

    mock_unassign.assert_called_once_with("namespace/project", 26, "merge_request", "claude")


@pytest.mark.asyncio
async def test_auto_unassign_disabled_by_default(monkeypatch):
    """Auto-unassign is NOT called when the setting is disabled."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", False)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-no-unassign",
            },
        )

    assert response.status_code == 200
    mock_unassign.assert_not_called()


@pytest.mark.asyncio
async def test_auto_unassign_not_called_on_failure(monkeypatch):
    """Auto-unassign is NOT called when agent dispatch fails."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    # Mock dispatch to return failure status
    async def fake_dispatch_failure(event_uuid, tasks, context):
        return [{"agent": task.agent, "status": "error", "event_id": event_uuid} for task in tasks]

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch_failure)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-fail-no-unassign",
            },
        )

    assert response.status_code == 200
    mock_unassign.assert_not_called()


@pytest.mark.asyncio
async def test_auto_unassign_logs_warning_on_glab_failure(monkeypatch):
    """A warning is logged when unassign_agent returns False (glab call failure)."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    # Mock unassign to return False (glab call failed)
    mock_unassign = AsyncMock(return_value=False)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-unassign-glab-fail",
            },
        )

    assert response.status_code == 200
    # unassign was attempted but returned failure
    mock_unassign.assert_called_once_with("namespace/project", 42, "issue", "claude")


@pytest.mark.asyncio
async def test_auto_unassign_logs_skip_on_agent_failure(monkeypatch):
    """Auto-unassign is skipped with info log when agent status is not 'ok'."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    # Dispatch returns error status with non-zero returncode
    async def fake_dispatch_error(event_uuid, tasks, context):
        return [{"agent": task.agent, "status": "error", "returncode": 1, "event_id": event_uuid} for task in tasks]

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch_error)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-agent-error-skip",
            },
        )

    assert response.status_code == 200
    mock_unassign.assert_not_called()


@pytest.mark.asyncio
async def test_auto_unassign_on_kill_with_unassign_enabled(monkeypatch):
    """Auto-unassign and termination comment are triggered when agent is killed and setting is enabled."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    # dispatch_agents raises AgentKilledError
    async def fake_dispatch_killed(event_uuid, tasks, context):
        raise AgentKilledError(event_uuid)

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch_killed)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-kill-unassign",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["triggers"][0]["status"] == "killed"

    mock_notify.assert_called_once_with(
        "namespace/project", 42, "issue", "claude",
        reason="Manual Kill",
        details="Operator terminated the agent via the dashboard.",
    )
    mock_unassign.assert_called_once_with("namespace/project", 42, "issue", "claude")


@pytest.mark.asyncio
async def test_auto_unassign_on_kill_with_unassign_disabled(monkeypatch):
    """Termination comment is posted but unassign is NOT called when setting is disabled."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", False)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    async def fake_dispatch_killed(event_uuid, tasks, context):
        raise AgentKilledError(event_uuid)

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch_killed)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-kill-no-unassign",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["triggers"][0]["status"] == "killed"

    # Termination comment should still be posted
    mock_notify.assert_called_once_with(
        "namespace/project", 42, "issue", "claude",
        reason="Manual Kill",
        details="Operator terminated the agent via the dashboard.",
    )
    # But unassign should NOT be called
    mock_unassign.assert_not_called()


@pytest.mark.asyncio
async def test_kill_no_unassign_without_assigned_agent(monkeypatch):
    """Neither unassign nor notification happens on kill when no agent was assigned."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if mentions and "claude" in mentions:
            rule = SimpleNamespace(name="mention-claude", mentions=["claude"], assignees=[], access="readonly")
            agents = [AgentTask(agent="claude", task="review", prompt="review.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    async def fake_dispatch_killed(event_uuid, tasks, context):
        raise AgentKilledError(event_uuid)

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch_killed)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)

    # Mention payload without assignment
    payload = {
        "object_kind": "note",
        "object_attributes": {"action": "create", "note": "@claude please review"},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-kill-no-agent",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["triggers"][0]["status"] == "killed"

    # No assigned agent, so no unassign or notify
    mock_unassign.assert_not_called()
    mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_kill_unassign_proceeds_when_notify_fails(monkeypatch):
    """Auto-unassign still fires when notify_agent_termination() returns False."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    async def fake_dispatch_killed(event_uuid, tasks, context):
        raise AgentKilledError(event_uuid)

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch_killed)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    # notify returns False (glab comment failed)
    mock_notify = AsyncMock(return_value=False)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-kill-notify-fail",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["triggers"][0]["status"] == "killed"

    # Notify was called but failed
    mock_notify.assert_called_once()
    # Unassign should still proceed despite notify failure
    mock_unassign.assert_called_once_with("namespace/project", 42, "issue", "claude")


@pytest.mark.asyncio
async def test_kill_unassign_on_mr_resource(monkeypatch):
    """Kill-path unassign and notification work for merge request resources."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    async def fake_dispatch_killed(event_uuid, tasks, context):
        raise AgentKilledError(event_uuid)

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch_killed)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)

    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "update", "iid": 99},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-kill-mr",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["triggers"][0]["status"] == "killed"

    mock_notify.assert_called_once_with(
        "namespace/project", 99, "merge_request", "claude",
        reason="Manual Kill",
        details="Operator terminated the agent via the dashboard.",
    )
    mock_unassign.assert_called_once_with("namespace/project", 99, "merge_request", "claude")


class TestFilterAssignedMentions:
    """Tests for _filter_assigned_mentions() suppression logic."""

    def test_suppresses_mention_when_same_agent_assigned(self):
        """@agent + /assign @agent in same note -> agent removed from mentions."""
        body = "@claude, I am assigning you this work.\n/assign @claude"
        result = webhooks._filter_assigned_mentions("Note Hook", body, ["claude"])
        assert result == []

    def test_preserves_mention_for_different_agent(self):
        """@agent1 + /assign @agent2 -> agent1 remains in mentions."""
        body = "@claude please review this.\n/assign @codex"
        result = webhooks._filter_assigned_mentions("Note Hook", body, ["claude", "codex"])
        assert result == ["claude"]

    def test_no_assign_preserves_all_mentions(self):
        """Note without /assign -> all mentions preserved."""
        body = "@claude @gemini please review"
        result = webhooks._filter_assigned_mentions("Note Hook", body, ["claude", "gemini"])
        assert result == ["claude", "gemini"]

    def test_non_note_event_unchanged(self):
        """Non-Note Hook events are never filtered."""
        body = "/assign @claude"
        result = webhooks._filter_assigned_mentions("Issue Hook", body, ["claude"])
        assert result == ["claude"]

    def test_empty_body_unchanged(self):
        """None body returns mentions unchanged."""
        result = webhooks._filter_assigned_mentions("Note Hook", None, ["claude"])
        assert result == ["claude"]

    def test_empty_mentions_unchanged(self):
        """Empty mentions list returns empty list."""
        body = "/assign @claude"
        result = webhooks._filter_assigned_mentions("Note Hook", body, [])
        assert result == []

    def test_case_insensitive_matching(self):
        """Mention suppression is case-insensitive."""
        body = "/assign @Claude"
        result = webhooks._filter_assigned_mentions("Note Hook", body, ["claude"])
        assert result == []

    def test_assign_at_start_of_body(self):
        """/assign at the very start of the body (no preceding newline)."""
        body = "/assign @gemini"
        result = webhooks._filter_assigned_mentions("Note Hook", body, ["gemini"])
        assert result == []

    def test_assign_with_leading_whitespace(self):
        """/assign with leading spaces on its line."""
        body = "@claude check this\n  /assign @claude"
        result = webhooks._filter_assigned_mentions("Note Hook", body, ["claude"])
        assert result == []

    def test_inline_assign_not_matched(self):
        """Text like 'please /assign @agent' mid-line should not match as a quick action."""
        body = "please /assign @claude to this task"
        result = webhooks._filter_assigned_mentions("Note Hook", body, ["claude"])
        # /assign must be at the start of a line (with optional leading whitespace)
        assert result == ["claude"]

    def test_trailing_punctuation_not_suppressed(self):
        """'/assign @agent.' with trailing dot treats 'agent.' as the username, not 'agent'."""
        body = "/assign @claude."
        result = webhooks._filter_assigned_mentions("Note Hook", body, ["claude"])
        # The regex captures 'claude.' (with dot) which does not match 'claude'
        assert result == ["claude"]


@pytest.mark.asyncio
async def test_note_with_assign_suppresses_mention_route(monkeypatch):
    """A Note Hook with both @agent and /assign @agent should NOT trigger the mention route."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        # Only the mention route would match a Note Hook with mentions
        if event_name == "Note Hook" and mentions:
            rule = SimpleNamespace(name="mention-claude", mentions=mentions, assignees=[], access="readonly")
            if rule_predicate and not rule_predicate(rule):
                return None
            agents = [AgentTask(agent="claude", task="note_followup", prompt="note_followup.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    payload = {
        "object_kind": "note",
        "object_attributes": {
            "action": "create",
            "note": "@claude, I am assigning you this work.\n/assign @claude",
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-assign-suppress",
            },
        )

    assert response.status_code == 200
    data = response.json()
    # Mentions were filtered out, so no route should match -> ignored
    assert data["status"] == "ignored"
    assert data["reason"] == "no-routes"


@pytest.mark.asyncio
async def test_note_with_assign_preserves_other_mentions(monkeypatch):
    """A Note Hook with @agent1 and /assign @agent2 should still trigger for agent1."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if event_name == "Note Hook" and mentions:
            agent_name = mentions[0]
            rule = SimpleNamespace(
                name=f"mention-{agent_name}",
                mentions=mentions,
                assignees=[],
                access="readonly",
            )
            if rule_predicate and not rule_predicate(rule):
                return None
            agents = [AgentTask(agent=agent_name, task="note_followup", prompt="note_followup.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    payload = {
        "object_kind": "note",
        "object_attributes": {
            "action": "create",
            "note": "@gemini please review.\n/assign @claude",
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-assign-preserve-other",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    # Only gemini should be triggered (claude was suppressed by /assign)
    assert len(data["agents"]) == 1
    assert data["agents"][0]["agent"] == "gemini"


@pytest.mark.asyncio
async def test_timeout_notification_posted_on_agent_timeout(monkeypatch):
    """Verify that a timeout notification is posted when an agent times out."""
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "top-secret")
    monkeypatch.setattr(webhooks, "_DEDUP", DummyDeduplicator(True))

    notify_calls = []

    async def mock_notify(project_path, iid, resource_type, agent_name, reason="", details=""):
        notify_calls.append({
            "project_path": project_path,
            "iid": iid,
            "resource_type": resource_type,
            "agent_name": agent_name,
            "reason": reason,
            "details": details,
        })
        return True

    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)

    default_rule = SimpleNamespace(name="default-route", mentions=[], assignees=[], access="readonly")

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if event_name != "Issue Hook":
            return None
        agents = [AgentTask(agent="claude", task="review", prompt="review.txt", options={})]
        return RouteMatch(rule=default_rule, agents=agents)

    monkeypatch.setattr(webhooks, "_ROUTES", _make_dummy_routes(resolver))

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "group/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    async def fake_dispatch(event_uuid, tasks, context):
        return [
            {
                "agent": "claude",
                "task": "review",
                "status": "error",
                "returncode": -1,
                "timed_out": "wall_clock",
                "log_file": "/tmp/run-logs/test.out.json",
                "event_id": event_uuid,
            }
        ]

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch)
    _patch_trigger_queue(monkeypatch)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "open", "iid": 42},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-timeout-notify",
            },
        )

    assert response.status_code == 200
    assert len(notify_calls) == 1
    assert notify_calls[0]["reason"] == "Timeout"
    assert notify_calls[0]["agent_name"] == "claude"
    assert "wall-clock" in notify_calls[0]["details"]
    assert "test.out.json" in notify_calls[0]["details"]


@pytest.mark.asyncio
async def test_no_timeout_notification_for_normal_completion(monkeypatch):
    """Verify that no timeout notification is posted when agent completes normally."""
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "top-secret")
    monkeypatch.setattr(webhooks, "_DEDUP", DummyDeduplicator(True))

    notify_calls = []

    async def mock_notify(project_path, iid, resource_type, agent_name, reason="", details=""):
        notify_calls.append(True)
        return True

    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)

    default_rule = SimpleNamespace(name="default-route", mentions=[], assignees=[], access="readonly")

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if event_name != "Issue Hook":
            return None
        agents = [AgentTask(agent="claude", task="review", prompt="review.txt", options={})]
        return RouteMatch(rule=default_rule, agents=agents)

    monkeypatch.setattr(webhooks, "_ROUTES", _make_dummy_routes(resolver))

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "group/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    async def fake_dispatch(event_uuid, tasks, context):
        return [
            {
                "agent": "claude",
                "task": "review",
                "status": "ok",
                "returncode": 0,
                "event_id": event_uuid,
            }
        ]

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch)
    _patch_trigger_queue(monkeypatch)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "open", "iid": 42},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-no-timeout",
            },
        )

    assert response.status_code == 200
    # No timeout notification should have been posted
    assert len(notify_calls) == 0


class TestSelfUnassignSuppression:
    """Tests for the self-unassign echo suppression mechanism."""

    def test_record_and_detect(self):
        """Recording a self-unassign makes _is_self_unassign return True."""
        webhooks._RECENT_UNASSIGNS.clear()
        webhooks._record_self_unassign("group/proj", 10, "claude")
        assert webhooks._is_self_unassign("group/proj", 10, ["claude"])

    def test_detect_consumes_entry(self):
        """After detection, the entry is consumed (one-shot)."""
        webhooks._RECENT_UNASSIGNS.clear()
        webhooks._record_self_unassign("group/proj", 10, "claude")
        assert webhooks._is_self_unassign("group/proj", 10, ["claude"])
        assert not webhooks._is_self_unassign("group/proj", 10, ["claude"])

    def test_no_false_positive_different_project(self):
        """Different project does not match."""
        webhooks._RECENT_UNASSIGNS.clear()
        webhooks._record_self_unassign("group/proj-a", 10, "claude")
        assert not webhooks._is_self_unassign("group/proj-b", 10, ["claude"])

    def test_no_false_positive_different_iid(self):
        """Different IID does not match."""
        webhooks._RECENT_UNASSIGNS.clear()
        webhooks._record_self_unassign("group/proj", 10, "claude")
        assert not webhooks._is_self_unassign("group/proj", 11, ["claude"])

    def test_no_false_positive_different_agent(self):
        """Different agent does not match."""
        webhooks._RECENT_UNASSIGNS.clear()
        webhooks._record_self_unassign("group/proj", 10, "claude")
        assert not webhooks._is_self_unassign("group/proj", 10, ["gemini"])

    def test_case_insensitive(self):
        """Agent matching is case-insensitive."""
        webhooks._RECENT_UNASSIGNS.clear()
        webhooks._record_self_unassign("group/proj", 10, "Claude")
        assert webhooks._is_self_unassign("group/proj", 10, ["claude"])

    def test_expired_entries_pruned(self):
        """Entries older than TTL are pruned and do not match."""
        webhooks._RECENT_UNASSIGNS.clear()
        import time
        webhooks._RECENT_UNASSIGNS[("group/proj", 10, "claude")] = time.monotonic() - 120
        assert not webhooks._is_self_unassign("group/proj", 10, ["claude"])
        assert len(webhooks._RECENT_UNASSIGNS) == 0


@pytest.mark.asyncio
async def test_self_unassign_suppresses_echo_webhook(monkeypatch):
    """An unassign webhook triggered by the app's own auto-unassign is suppressed.

    This is the core regression test for issue #63: after auto-unassign fires,
    the resulting webhook must NOT retrigger the agent, regardless of whether
    the webhook payload includes changes.assignees or not.
    """
    webhooks._RECENT_UNASSIGNS.clear()

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-mr-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="assign_work", prompt="assign_work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    # Simulate the app recording a self-unassign (as it would after auto-unassign)
    webhooks._record_self_unassign("example-org/robot-dev-team", 32, "claude")

    # Simulate the echo webhook GitLab sends -- NO changes.assignees block,
    # only stale top-level payload.assignees (the problematic case)
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "update", "iid": 32},
        "user": {"username": "testuser"},
        "project": {"path_with_namespace": "example-org/robot-dev-team"},
        "assignees": [{"username": "claude"}],
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-self-unassign-echo",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "self-unassign"


@pytest.mark.asyncio
async def test_self_unassign_does_not_suppress_genuine_assign(monkeypatch):
    """A genuine assignment webhook is NOT suppressed by the self-unassign tracker."""
    webhooks._RECENT_UNASSIGNS.clear()

    dispatch_called = []

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-mr-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="assign_work", prompt="assign_work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    # No self-unassign recorded -- this is a genuine new assignment
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "update", "iid": 32},
        "user": {"username": "testuser"},
        "project": {"path_with_namespace": "example-org/robot-dev-team"},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-genuine-assign",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_auto_unassign_records_self_unassign(monkeypatch):
    """Verify that auto-unassign on successful completion records the self-unassign."""
    webhooks._RECENT_UNASSIGNS.clear()

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "update", "iid": 37},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-record-self-unassign",
            },
        )

    assert response.status_code == 200
    mock_unassign.assert_called_once()

    # Verify the self-unassign was recorded
    assert ("namespace/project", 37, "claude") in webhooks._RECENT_UNASSIGNS


@pytest.mark.asyncio
async def test_failed_unassign_does_not_record_self_unassign(monkeypatch):
    """When unassign_agent fails, no self-unassign entry should be recorded.

    This prevents a stale suppression entry from incorrectly blocking a
    legitimate webhook for the same (project, iid, agent) tuple.
    """
    webhooks._RECENT_UNASSIGNS.clear()

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    # unassign_agent returns False (failure)
    mock_unassign = AsyncMock(return_value=False)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "update", "iid": 37},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-failed-unassign",
            },
        )

    assert response.status_code == 200
    mock_unassign.assert_called_once()

    # Verify NO self-unassign was recorded since unassign failed
    assert ("namespace/project", 37, "claude") not in webhooks._RECENT_UNASSIGNS


# ---- Issue #72: Self-unassign suppression with empty assignees ----


@pytest.mark.asyncio
async def test_self_unassign_suppresses_when_assignees_empty(monkeypatch):
    """An unassign webhook with empty assignees list is suppressed when the
    agent was recently self-unassigned.

    This is the core regression test for issue #72: when auto-unassign fires,
    the resulting webhook has assignees=[] (via changes.assignees.current=[]).
    The guard must still detect the removed agent from changes.assignees.previous.
    """
    webhooks._RECENT_UNASSIGNS.clear()

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    # Simulate the app recording a self-unassign
    webhooks._record_self_unassign("example-org/robot-dev-team", 40, "claude")

    # Webhook from GitLab after unassign: assignees is empty, but
    # changes.assignees shows claude was removed
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "update", "iid": 40},
        "user": {"username": "testuser"},
        "project": {"path_with_namespace": "example-org/robot-dev-team"},
        "assignees": [],
        "changes": {
            "assignees": {
                "previous": [{"username": "claude"}],
                "current": [],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-empty-assignees-unassign",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "self-unassign"


@pytest.mark.asyncio
async def test_self_unassign_no_false_positive_on_unrelated_update(monkeypatch):
    """An update webhook with empty assignees but NO self-unassign record
    should NOT be suppressed."""
    webhooks._RECENT_UNASSIGNS.clear()

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    # No self-unassign recorded -- changes show claude removed by someone else
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "update", "iid": 40},
        "user": {"username": "testuser"},
        "project": {"path_with_namespace": "example-org/robot-dev-team"},
        "assignees": [],
        "changes": {
            "assignees": {
                "previous": [{"username": "claude"}],
                "current": [],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-no-false-positive",
            },
        )

    assert response.status_code == 200
    data = response.json()
    # Should fall through to no-routes (resolver returns None), not self-unassign
    assert data["status"] == "ignored"
    assert data["reason"] == "no-routes"


# ---- Issue #72: System note suppression for unassign actions ----


@pytest.mark.asyncio
async def test_system_unassign_note_suppressed(monkeypatch):
    """A GitLab system note 'unassigned @claude' should be suppressed.

    When auto-unassign fires, GitLab emits a Note Hook with system=true
    and text like 'unassigned @claude'. This note contains @claude which
    would match the mention-claude route and trigger an erroneous dispatch.
    """
    webhooks._RECENT_UNASSIGNS.clear()

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if mentions and "claude" in mentions:
            rule = SimpleNamespace(name="mention-claude", mentions=["claude"], assignees=[], access="readonly")
            agents = [AgentTask(agent="claude", task="note_followup", prompt="note_followup.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    payload = {
        "object_kind": "note",
        "object_attributes": {
            "action": "create",
            "note": "unassigned @claude",
            "system": True,
        },
        "user": {"username": "testuser"},
        "project": {"path_with_namespace": "example-org/robot-dev-team"},
        "merge_request": {"iid": 40},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-system-unassign-note",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "system-unassign-note"


@pytest.mark.asyncio
async def test_system_unassign_note_with_registry_match(monkeypatch):
    """System unassign note suppressed even via self-unassign registry match."""
    webhooks._RECENT_UNASSIGNS.clear()

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")

    # Record the self-unassign so the registry check also matches
    webhooks._record_self_unassign("example-org/robot-dev-team", 40, "claude")

    payload = {
        "object_kind": "note",
        "object_attributes": {
            "action": "create",
            "note": "unassigned @claude",
            "system": True,
        },
        "user": {"username": "testuser"},
        "project": {"path_with_namespace": "example-org/robot-dev-team"},
        "merge_request": {"iid": 40},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-system-note-registry",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "system-unassign-note"


@pytest.mark.asyncio
async def test_non_system_note_not_suppressed(monkeypatch):
    """A regular (non-system) note mentioning @claude is NOT suppressed."""
    webhooks._RECENT_UNASSIGNS.clear()

    dispatch_called = []

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if mentions and "claude" in mentions:
            rule = SimpleNamespace(name="mention-claude", mentions=["claude"], assignees=[], access="readonly")
            agents = [AgentTask(agent="claude", task="note_followup", prompt="note_followup.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    payload = {
        "object_kind": "note",
        "object_attributes": {
            "action": "create",
            "note": "Hey @claude, can you review this?",
            "system": False,
        },
        "user": {"username": "testuser"},
        "project": {"path_with_namespace": "example-org/robot-dev-team"},
        "merge_request": {"iid": 40},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-normal-note",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_system_note_non_unassign_not_suppressed(monkeypatch):
    """A system note that is NOT an unassign (e.g. 'assigned @claude') passes through."""
    webhooks._RECENT_UNASSIGNS.clear()

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    payload = {
        "object_kind": "note",
        "object_attributes": {
            "action": "create",
            "note": "assigned @claude",
            "system": True,
        },
        "user": {"username": "testuser"},
        "project": {"path_with_namespace": "example-org/robot-dev-team"},
        "merge_request": {"iid": 40},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": "uuid-system-assign-note",
            },
        )

    assert response.status_code == 200
    data = response.json()
    # Should NOT be suppressed - falls through to no-routes
    assert data["status"] == "ignored"
    assert data["reason"] == "no-routes"


# ---- Issue #72: Unit tests for _is_unassign_system_note ----


class TestIsUnassignSystemNote:
    def test_unassign_note_with_known_agent(self, monkeypatch):
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
        payload = {
            "object_attributes": {"note": "unassigned @claude", "system": True},
            "project": {"path_with_namespace": "group/proj"},
            "merge_request": {"iid": 10},
        }
        assert webhooks._is_unassign_system_note(payload) is True

    def test_unassign_note_with_unknown_user(self, monkeypatch):
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
        payload = {
            "object_attributes": {"note": "unassigned @randomuser", "system": True},
            "project": {"path_with_namespace": "group/proj"},
            "merge_request": {"iid": 10},
        }
        assert webhooks._is_unassign_system_note(payload) is False

    def test_assign_note_not_matched(self, monkeypatch):
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
        payload = {
            "object_attributes": {"note": "assigned @claude", "system": True},
            "project": {"path_with_namespace": "group/proj"},
            "merge_request": {"iid": 10},
        }
        assert webhooks._is_unassign_system_note(payload) is False

    def test_registry_match_takes_priority(self, monkeypatch):
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
        webhooks._RECENT_UNASSIGNS.clear()
        webhooks._record_self_unassign("group/proj", 10, "claude")
        payload = {
            "object_attributes": {"note": "unassigned @claude", "system": True},
            "project": {"path_with_namespace": "group/proj"},
            "merge_request": {"iid": 10},
        }
        assert webhooks._is_unassign_system_note(payload) is True
        # Registry entry should be consumed
        assert ("group/proj", 10, "claude") not in webhooks._RECENT_UNASSIGNS

    def test_issue_iid_resolved(self, monkeypatch):
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
        payload = {
            "object_attributes": {"note": "unassigned @gemini", "system": True},
            "project": {"path_with_namespace": "group/proj"},
            "issue": {"iid": 5},
        }
        assert webhooks._is_unassign_system_note(payload) is True


# ---- Issue #85: Webhook secret bypass when secret is empty/unset ----


@pytest.mark.asyncio
async def test_webhook_allows_request_when_secret_is_empty(monkeypatch):
    """When gitlab_webhook_secret is empty, requests without a token are allowed."""
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "")
    monkeypatch.setattr(webhooks, "_DEDUP", DummyDeduplicator(should_process=True))
    _patch_build_context(monkeypatch)
    _patch_dispatch(monkeypatch)
    _patch_trigger_queue(monkeypatch)

    default_rule = SimpleNamespace(name="default-route", mentions=[], assignees=[], access="readonly")

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        agents = [AgentTask(agent="claude", task="review", prompt="review.txt", options={})]
        return RouteMatch(rule=default_rule, agents=agents)

    monkeypatch.setattr(webhooks, "_ROUTES", _make_dummy_routes(resolver))

    payload = {
        "object_kind": "merge_request",
        "object_attributes": {"action": "open"},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Event-UUID": "uuid-no-secret",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


# ---- Issue #85: /health endpoint smoke test ----


@pytest.mark.asyncio
async def test_health_endpoint():
    """The /health endpoint returns 200 with status ok."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---- Issue #80: Backup notification tests ----


@pytest.mark.asyncio
async def test_backup_notification_posted_when_enabled(monkeypatch):
    """When dispatch returns a result with backup_branch and notifications are enabled, a comment is posted."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", False)
    monkeypatch.setattr(settings, "enable_backup_notifications", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    async def fake_dispatch(event_uuid, tasks, context):
        return [
            {
                "agent": "claude",
                "status": "ok",
                "backups": [
                    {"branch": "backup/claude/main-20260314-120000", "reason": "uncommitted_changes"},
                ],
            }
        ]

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch)

    mock_backup_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_backup_created", mock_backup_notify)

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-backup-notify",
            },
        )

    assert response.status_code == 200
    mock_backup_notify.assert_called_once_with(
        "namespace/project", 42, "issue", "claude",
        "backup/claude/main-20260314-120000",
        backup_reason="uncommitted_changes",
    )


@pytest.mark.asyncio
async def test_backup_notification_skipped_when_disabled(monkeypatch):
    """When enable_backup_notifications is False, no backup comment is posted."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", False)
    monkeypatch.setattr(settings, "enable_backup_notifications", False)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    async def fake_dispatch(event_uuid, tasks, context):
        return [
            {
                "agent": "claude",
                "status": "ok",
                "backups": [
                    {"branch": "backup/claude/main-20260314-120000", "reason": "uncommitted_changes"},
                ],
            }
        ]

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch)

    mock_backup_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_backup_created", mock_backup_notify)

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-backup-notify-disabled",
            },
        )

    assert response.status_code == 200
    mock_backup_notify.assert_not_called()


@pytest.mark.asyncio
async def test_backup_notification_skipped_when_no_backup(monkeypatch):
    """When dispatch result has no backup_branch, no backup comment is posted."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", False)
    monkeypatch.setattr(settings, "enable_backup_notifications", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    async def fake_dispatch(event_uuid, tasks, context):
        return [
            {
                "agent": "claude",
                "status": "ok",
            }
        ]

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch)

    mock_backup_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_backup_created", mock_backup_notify)

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-backup-no-backup",
            },
        )

    assert response.status_code == 200
    mock_backup_notify.assert_not_called()


@pytest.mark.asyncio
async def test_backup_notification_posts_for_each_backup(monkeypatch):
    """When dispatch returns multiple backups, a comment is posted for each one."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", False)
    monkeypatch.setattr(settings, "enable_backup_notifications", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    async def fake_dispatch(event_uuid, tasks, context):
        return [
            {
                "agent": "claude",
                "status": "ok",
                "backups": [
                    {"branch": "backup/claude/main-20260314-120000", "reason": "uncommitted_changes"},
                    {"branch": "backup/claude/main-commits-20260314-120000", "reason": "local_commits"},
                ],
            }
        ]

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch)

    mock_backup_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_backup_created", mock_backup_notify)

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)

    payload = {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": 42},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-backup-dual",
            },
        )

    assert response.status_code == 200
    assert mock_backup_notify.call_count == 2
    mock_backup_notify.assert_any_call(
        "namespace/project", 42, "issue", "claude",
        "backup/claude/main-20260314-120000",
        backup_reason="uncommitted_changes",
    )
    mock_backup_notify.assert_any_call(
        "namespace/project", 42, "issue", "claude",
        "backup/claude/main-commits-20260314-120000",
        backup_reason="local_commits",
    )
