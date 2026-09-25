"""Robot Dev Team Project
File: tests/test_webhooks.py
Description: Pytest coverage for webhook integrations.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    # Pin to deterministic dispatch by default so unrelated tests are not
    # coupled to @all shuffle behavior; tests that exercise issue #14 opt in.
    monkeypatch.setattr(settings, "randomize_all_mentions", False)
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


# ---------------------------------------------------------------------------
# Issue #14 (randomize @all dispatch order) and issue #16 (author-aware @all
# expansion, code-span stripping, self-mention filtering).
# ---------------------------------------------------------------------------


def _agent_route_resolver(mapping):
    """Build a resolver mapping single-mention tuples to per-mention routes."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        route_name = mapping.get(tuple(m.lower() for m in mentions))
        if not route_name:
            return None
        rule = SimpleNamespace(name=route_name, mentions=mentions, assignees=[], access="readonly")
        if rule_predicate and not rule_predicate(rule):
            return None
        agents = [AgentTask(agent=f"agent-{route_name}", task="review")]
        return RouteMatch(rule=rule, agents=agents)

    return resolver


async def _post_note(note, uuid, user=None):
    payload = {
        "object_kind": "note",
        "object_attributes": {"action": "create", "note": note},
    }
    if user is not None:
        payload["user"] = {"username": user}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Event-UUID": uuid,
            },
        )


_THREE_AGENT_ROUTES = {
    ("claude",): "claude-route",
    ("gemini",): "gemini-route",
    ("codex",): "codex-route",
}


@pytest.mark.asyncio
async def test_all_mention_randomizes_dispatch_when_enabled(monkeypatch):
    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(settings, "randomize_all_mentions", True)
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _agent_route_resolver(_THREE_AGENT_ROUTES))

    shuffle_calls = []

    def fake_shuffle(seq):
        shuffle_calls.append(list(seq))
        seq.reverse()

    monkeypatch.setattr(webhooks.random, "shuffle", fake_shuffle)

    response = await _post_note("@all please review", "uuid-rand-on")

    assert response.status_code == 200
    data = response.json()
    # Shuffle is invoked once, with the fully expanded mention list.
    assert shuffle_calls == [["claude", "gemini", "codex"]]
    # Dispatch order follows the shuffled (reversed) list.
    order = [trigger["route"] for trigger in data["triggers"]]
    assert order == ["codex-route", "gemini-route", "claude-route"]


@pytest.mark.asyncio
async def test_all_mention_deterministic_when_disabled(monkeypatch):
    setup_common_patches(monkeypatch)  # randomize_all_mentions pinned False
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _agent_route_resolver(_THREE_AGENT_ROUTES))

    def fail_shuffle(seq):
        raise AssertionError("shuffle must not run when RANDOMIZE_ALL_MENTIONS is false")

    monkeypatch.setattr(webhooks.random, "shuffle", fail_shuffle)

    response = await _post_note("@all please review", "uuid-rand-off")

    assert response.status_code == 200
    data = response.json()
    order = [trigger["route"] for trigger in data["triggers"]]
    assert order == ["claude-route", "gemini-route", "codex-route"]


@pytest.mark.asyncio
async def test_explicit_mention_list_not_shuffled(monkeypatch):
    """Explicit @claude @gemini @codex stays deterministic even with the flag on."""
    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(settings, "randomize_all_mentions", True)
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _agent_route_resolver(_THREE_AGENT_ROUTES))

    def fail_shuffle(seq):
        raise AssertionError("explicit mention lists must not be shuffled")

    monkeypatch.setattr(webhooks.random, "shuffle", fail_shuffle)

    response = await _post_note("@claude @gemini @codex please review", "uuid-explicit")

    assert response.status_code == 200
    data = response.json()
    order = [trigger["route"] for trigger in data["triggers"]]
    assert order == ["claude-route", "gemini-route", "codex-route"]


@pytest.mark.asyncio
async def test_agent_authored_all_mention_suppressed(monkeypatch):
    """An agent authoring @all does not fan out (issue #16 self-recursion guard)."""
    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(settings, "randomize_all_mentions", True)
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _agent_route_resolver(_THREE_AGENT_ROUTES))

    response = await _post_note("@all please review", "uuid-agent-all", user="gemini")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "no-routes"


@pytest.mark.asyncio
async def test_agent_authored_all_keeps_explicit_co_mentions(monkeypatch):
    """@all suppression for agent authors still honors explicitly named agents."""
    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _agent_route_resolver(_THREE_AGENT_ROUTES))

    response = await _post_note("@all @claude take a look", "uuid-agent-all-explicit", user="gemini")

    assert response.status_code == 200
    data = response.json()
    order = [trigger["route"] for trigger in data["triggers"]]
    assert order == ["claude-route"]


@pytest.mark.asyncio
async def test_backticked_all_mention_not_expanded(monkeypatch):
    """A comment that merely discusses `@all` in backticks must not fan out.

    Mirrors the real incident in issue #16: a comment addressed to @codex that
    quotes `@all` while asking a question previously dispatched all three agents.
    """
    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _agent_route_resolver(_THREE_AGENT_ROUTES))

    response = await _post_note(
        "@codex - what did the agent `@all` mention ignore about my guard?",
        "uuid-backtick",
        user="cavin",
    )

    assert response.status_code == 200
    data = response.json()
    order = [trigger["route"] for trigger in data["triggers"]]
    assert order == ["codex-route"]


@pytest.mark.asyncio
async def test_self_mention_filtered_for_agent_author(monkeypatch):
    """An agent is never dispatched against a comment it authored (issue #16)."""
    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _agent_route_resolver(_THREE_AGENT_ROUTES))

    response = await _post_note("@gemini @claude let's sync", "uuid-self", user="gemini")

    assert response.status_code == 200
    data = response.json()
    order = [trigger["route"] for trigger in data["triggers"]]
    assert order == ["claude-route"]


def test_strip_code_spans_removes_backticked_mentions():
    text = "@codex see `@all` and ```\n@gemini\n``` but @claude stays"
    assert webhooks._parse_mentions_from_text(text) == ["codex", "claude"]


def test_strip_code_spans_removes_tilde_fenced_mentions():
    """Tilde-fenced code blocks (~~~...~~~) are stripped along with backtick fences."""
    text = "@codex see\n~~~\n@all should be code\n~~~\nbut @claude stays"
    assert webhooks._parse_mentions_from_text(text) == ["codex", "claude"]


def test_strip_code_spans_removes_multi_backtick_inline_mentions():
    """Multi-backtick inline spans (e.g. ``@all``) are stripped via paired-run matching."""
    text = "@codex compare ``@all`` to @claude"
    assert webhooks._parse_mentions_from_text(text) == ["codex", "claude"]


def test_parse_mentions_ignores_email_local_part():
    """An ``@`` glued to a preceding identifier (email local-part) is not a mention."""
    text = "Please forward to support@all and notify @claude"
    assert webhooks._parse_mentions_from_text(text) == ["claude"]


def test_parse_mentions_ignores_url_path_mentions():
    """An ``@`` after a URL path separator is not a mention."""
    text = "Context lives at https://example.com/@all -- ping @codex when ready"
    assert webhooks._parse_mentions_from_text(text) == ["codex"]


def test_parse_mentions_matches_real_mention_shapes():
    """Common real-mention prefixes still match after the boundary lookbehind."""
    # start-of-string, after space, after comma+space, inside parentheses, after dash+space
    text = "@cavin says: hi @claude, please ping (@gemini) - @codex confirm."
    assert webhooks._parse_mentions_from_text(text) == ["cavin", "claude", "gemini", "codex"]


def test_indented_code_block_strips_all_mention():
    """Issue #17 item 1: a 4-space-indented code block is stripped.

    Flips the former pin-current-behavior test: an @all shown as indented code
    (preceded by a blank line, per CommonMark) no longer parses as a live
    mention.
    """
    text = "Normal text\n\n    @all should be code\n\nback to text"
    assert "all" not in webhooks._parse_mentions_from_text(text)


def test_tab_indented_code_block_strips_all_mention():
    """A tab-indented line is treated as code just like four spaces."""
    text = "Normal text\n\n\t@all should be code\n\nback to text"
    assert "all" not in webhooks._parse_mentions_from_text(text)


def test_indented_line_after_paragraph_is_stripped():
    """Every qualifying indented line is stripped, without trying to tell an
    indented code block apart from a paragraph continuation. Dropping the
    blank-lead-in heuristic is the over-strip-biased trade-off (issue #17
    review): a per-line flag cannot model block containment, and the safe
    direction is to strip."""
    text = "Some paragraph\n    @all still indented"
    assert "all" not in webhooks._parse_mentions_from_text(text)


def test_list_item_continuation_mention_over_stripped():
    """A 4-space-indented list-item continuation is over-stripped along with
    real indented code. Per the review consensus this dropped mention is the
    accepted, recoverable cost of remaining GLFM-parser-free while never
    under-stripping a quoted ``@all`` (issue #17)."""
    text = "- first item\n    @all continuation"
    assert "all" not in webhooks._parse_mentions_from_text(text)


def test_mixed_space_tab_indent_stripped():
    """Up to three spaces followed by a tab reaches a CommonMark tab stop and
    opens an indented code block, so ``@all`` there is stripped (issue #17
    review, codex finding #1)."""
    text = "intro\n\n  \t@all shown as code"
    assert "all" not in webhooks._parse_mentions_from_text(text)


def test_indented_code_after_heading_stripped():
    """Indented code following a heading (a non-paragraph block) is stripped;
    the old blank-lead-in heuristic under-stripped this fan-out shape (issue #17
    review, codex finding #2)."""
    text = "# Example\n    @all shown as code"
    assert "all" not in webhooks._parse_mentions_from_text(text)


def test_indented_code_after_blockquote_stripped():
    """Indented code following a blockquote line is stripped rather than parsed
    as a live mention (issue #17 review, codex finding #2)."""
    text = "> context\n    @all shown as code"
    assert "all" not in webhooks._parse_mentions_from_text(text)


def test_lazy_blockquote_continuation_stripped():
    """A bare mention line immediately following a ``>`` line (no blank
    separator) is a lazy blockquote continuation -- GitLab renders it as still
    quoted, so it is stripped too (issue #17 review, claude finding)."""
    text = "> quoted @foo\nstill quoted @all"
    assert webhooks._parse_mentions_from_text(text) == []


def test_blank_line_closes_lazy_blockquote_continuation():
    """A blank line ends the quoted paragraph, so a mention after the blank
    still parses (guards the over-strip from spilling past the quote)."""
    text = "> quoted @foo\n\n@claude real request"
    assert webhooks._parse_mentions_from_text(text) == ["claude"]


def test_single_line_fence_preserves_word_boundary():
    """A single-line ``` fence spans no newline, so it collapses to a space
    (not the empty string) to keep surrounding words separated (issue #17
    review, gemini finding #1)."""
    assert webhooks._strip_code_spans("a```b```c") == "a c"


def test_blockquoted_all_mention_stripped():
    """Issue #17 item 2: a blockquoted line (``> ...``) is stripped.

    Flips the former pin-current-behavior test: a reply quoting a prior ``@all``
    no longer re-expands on receipt.
    """
    text = "> Earlier: @all please review\n\nMy reply: thanks"
    assert "all" not in webhooks._parse_mentions_from_text(text)


def test_blockquote_three_space_prefix_stripped():
    """Up to three leading spaces before ``>`` still counts as a blockquote."""
    text = "   > quoted @all please review\n\nplain reply"
    assert "all" not in webhooks._parse_mentions_from_text(text)


def test_nested_blockquote_stripped():
    """A nested blockquote (``> > ...``) is stripped."""
    text = "> > deeply quoted @all\n\nplain reply"
    assert "all" not in webhooks._parse_mentions_from_text(text)


def test_gitlab_multiline_blockquote_stripped():
    """A GitLab ``>>> ... >>>`` multiline blockquote strips its interior even
    though the interior lines carry no ``>`` prefix."""
    text = ">>>\nEarlier someone said @all please review\n>>>\n\nMy reply"
    assert "all" not in webhooks._parse_mentions_from_text(text)


def test_unclosed_multiline_blockquote_strips_to_end():
    """An unclosed ``>>>`` region fails safe by stripping to end-of-text."""
    text = ">>>\nquoted @all with no closing marker"
    assert "all" not in webhooks._parse_mentions_from_text(text)


def test_redirect_operator_not_treated_as_blockquote():
    """A ``>`` appearing mid-line (shell redirect) is not a blockquote, so a
    real mention on that line is preserved."""
    text = "run build > out.txt and ping @claude"
    assert webhooks._parse_mentions_from_text(text) == ["claude"]


def test_mention_after_blockquote_block_preserved():
    """A live mention immediately after a quoted block still parses."""
    text = "> quoted @all please review\n\n@claude real request"
    mentions = webhooks._parse_mentions_from_text(text)
    assert "all" not in mentions
    assert "claude" in mentions


def test_mention_after_indented_code_block_preserved():
    """A live mention immediately after an indented code block still parses."""
    text = "intro\n\n    @all in code\n\n@claude real request"
    mentions = webhooks._parse_mentions_from_text(text)
    assert "all" not in mentions
    assert "claude" in mentions


def test_fenced_block_then_indented_line_both_stripped():
    """Fence interaction: a fenced block and a following indented code block are
    both stripped, and a live mention after them is preserved."""
    text = "```\n@gemini in fence\n```\n\n    @all indented\n\n@claude stays"
    mentions = webhooks._parse_mentions_from_text(text)
    assert "gemini" not in mentions
    assert "all" not in mentions
    assert mentions == ["claude"]


def test_filter_self_mention_drops_author_username():
    """An agent author is removed from their own dispatch list."""
    assert webhooks._filter_self_mention("claude", ["claude", "gemini"]) == ["gemini"]


def test_filter_self_mention_case_insensitive():
    """Author-vs-mention match is case-insensitive."""
    assert webhooks._filter_self_mention("Claude", ["claude", "gemini"]) == ["gemini"]


def test_filter_self_mention_noop_for_human_author():
    """A human author leaves the mention list untouched."""
    assert webhooks._filter_self_mention("cavin", ["claude", "gemini"]) == ["claude", "gemini"]


def test_filter_self_mention_noop_for_empty_author():
    """An empty author short-circuits before any filtering."""
    assert webhooks._filter_self_mention("", ["claude", "gemini"]) == ["claude", "gemini"]


@pytest.mark.asyncio
async def test_base_match_receives_unshuffled_mention_list(monkeypatch):
    """The multi-agent base_match sees the unshuffled list even when the
    per-mention dispatch is shuffled. Locks the diagnostic contract called out
    in MR #10 review (#14 randomization should not leak into base_match logs).
    """
    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(settings, "randomize_all_mentions", True)

    base_observed = {"mentions": None}
    per_mention_observed = []

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if rule_predicate is webhooks._exclude_single_mention_rules:
            # The base_match path. Record what was handed in and decline to
            # match so we do not need to mock a multi-agent route.
            base_observed["mentions"] = list(mentions)
            return None
        per_mention_observed.append(list(mentions))
        route_name = _THREE_AGENT_ROUTES.get(tuple(m.lower() for m in mentions))
        if not route_name:
            return None
        rule = SimpleNamespace(name=route_name, mentions=mentions, assignees=[], access="readonly")
        agents = [AgentTask(agent=f"agent-{route_name}", task="review")]
        return RouteMatch(rule=rule, agents=agents)

    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", resolver)
    monkeypatch.setattr(webhooks.random, "shuffle", lambda seq: seq.reverse())

    response = await _post_note("@all please review", "uuid-base-unshuffled")

    assert response.status_code == 200
    # base_match resolver call observed the unshuffled, fully expanded list.
    assert base_observed["mentions"] == ["claude", "gemini", "codex"]
    # Per-mention dispatch followed the shuffled (reversed) order.
    assert per_mention_observed == [["codex"], ["gemini"], ["claude"]]


def test_expand_all_mention_reports_expansion_flag():
    mentions, expanded = webhooks._expand_all_mention(["all"], author="cavin")
    assert expanded is True
    mentions2, expanded2 = webhooks._expand_all_mention(["claude", "gemini"], author="cavin")
    assert expanded2 is False
    assert mentions2 == ["claude", "gemini"]


def test_expand_all_mention_suppressed_for_agent_author():
    mentions, expanded = webhooks._expand_all_mention(["all", "claude"], author="gemini")
    assert expanded is False
    assert mentions == ["claude"]


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


# ---- Issue #1: Auto-unassign on timeout (parity with manual-kill path) ----


def _make_timeout_assigned_resolver():
    """Return a resolver that produces an 'assigned claude' route for tests."""

    def resolver(event_name, action, author, labels, mentions, body=None, assignees=None, rule_predicate=None):
        if assignees and "claude" in assignees:
            rule = SimpleNamespace(name="assign-claude", mentions=[], assignees=["claude"], access="readwrite")
            agents = [AgentTask(agent="claude", task="work", prompt="work.txt", options={})]
            return RouteMatch(rule=rule, agents=agents)
        return None

    return resolver


def _make_timed_out_dispatch(reason="wall_clock", agent="claude", returncode=-1):
    async def fake_dispatch(event_uuid, tasks, context):
        return [
            {
                "agent": agent,
                "task": "work",
                "status": "error",
                "returncode": returncode,
                "timed_out": reason,
                "log_file": "/tmp/run-logs/timeout.out.json",
                "event_id": event_uuid,
            }
        ]

    return fake_dispatch


def _assigned_issue_payload(iid=42):
    return {
        "object_kind": "issue",
        "object_attributes": {"action": "update", "iid": iid},
        "assignees": [{"username": "claude"}],
        "changes": {
            "assignees": {
                "previous": [],
                "current": [{"username": "claude"}],
            }
        },
    }


@pytest.mark.asyncio
async def test_timeout_auto_unassign_when_enabled(monkeypatch):
    """Timed-out assigned agent is unassigned when enable_auto_unassign=True."""
    webhooks._RECENT_UNASSIGNS.clear()

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _make_timeout_assigned_resolver())

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)
    monkeypatch.setattr(webhooks, "dispatch_agents", _make_timed_out_dispatch())

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)
    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=_assigned_issue_payload(42),
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-timeout-unassign-enabled",
            },
        )

    assert response.status_code == 200
    mock_notify.assert_called_once()
    assert mock_notify.call_args.kwargs["reason"] == "Timeout"
    mock_unassign.assert_called_once_with("namespace/project", 42, "issue", "claude")
    # Self-unassign suppression should be recorded so the echo webhook is ignored.
    assert ("namespace/project", 42, "claude") in webhooks._RECENT_UNASSIGNS


@pytest.mark.asyncio
async def test_timeout_auto_unassign_when_disabled(monkeypatch):
    """When enable_auto_unassign=False, timeout notification is posted but no unassign occurs."""
    webhooks._RECENT_UNASSIGNS.clear()

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", False)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _make_timeout_assigned_resolver())

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)
    monkeypatch.setattr(webhooks, "dispatch_agents", _make_timed_out_dispatch())

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)
    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=_assigned_issue_payload(43),
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-timeout-unassign-disabled",
            },
        )

    assert response.status_code == 200
    mock_notify.assert_called_once()
    mock_unassign.assert_not_called()
    assert ("namespace/project", 43, "claude") not in webhooks._RECENT_UNASSIGNS


@pytest.mark.asyncio
async def test_timeout_auto_unassign_inactivity_reason(monkeypatch):
    """Inactivity timeouts trigger unassign as well as wall_clock timeouts."""
    webhooks._RECENT_UNASSIGNS.clear()

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _make_timeout_assigned_resolver())

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)
    monkeypatch.setattr(webhooks, "dispatch_agents", _make_timed_out_dispatch(reason="inactivity"))

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)
    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=_assigned_issue_payload(44),
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-timeout-unassign-inactivity",
            },
        )

    assert response.status_code == 200
    assert "inactivity" in mock_notify.call_args.kwargs["details"]
    mock_unassign.assert_called_once_with("namespace/project", 44, "issue", "claude")


@pytest.mark.asyncio
async def test_timeout_does_not_unassign_non_assigned_agent(monkeypatch):
    """A timed-out agent that is not the originally-assigned agent must not be unassigned."""
    webhooks._RECENT_UNASSIGNS.clear()

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _make_timeout_assigned_resolver())

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)
    # dispatch returns a timed-out result for a *different* agent (codex)
    monkeypatch.setattr(webhooks, "dispatch_agents", _make_timed_out_dispatch(agent="codex"))

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)
    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=_assigned_issue_payload(45),
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-timeout-mismatched-agent",
            },
        )

    assert response.status_code == 200
    mock_notify.assert_called_once()
    mock_unassign.assert_not_called()
    assert ("namespace/project", 45, "claude") not in webhooks._RECENT_UNASSIGNS


@pytest.mark.asyncio
async def test_timeout_unassign_still_fires_when_notification_fails(monkeypatch):
    """If notify_agent_termination returns False, unassign still runs."""
    webhooks._RECENT_UNASSIGNS.clear()

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _make_timeout_assigned_resolver())

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)
    monkeypatch.setattr(webhooks, "dispatch_agents", _make_timed_out_dispatch())

    mock_notify = AsyncMock(return_value=False)  # notification posting failed
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)
    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=_assigned_issue_payload(46),
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-timeout-notify-failed",
            },
        )

    assert response.status_code == 200
    mock_notify.assert_called_once()
    mock_unassign.assert_called_once_with("namespace/project", 46, "issue", "claude")
    assert ("namespace/project", 46, "claude") in webhooks._RECENT_UNASSIGNS


@pytest.mark.asyncio
async def test_timeout_failed_unassign_does_not_record_self_unassign(monkeypatch):
    """If unassign_agent returns False during the timeout path, no self-unassign is recorded."""
    webhooks._RECENT_UNASSIGNS.clear()

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _make_timeout_assigned_resolver())

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)
    monkeypatch.setattr(webhooks, "dispatch_agents", _make_timed_out_dispatch())

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)
    mock_unassign = AsyncMock(return_value=False)  # glab call failed
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=_assigned_issue_payload(47),
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-timeout-unassign-failed",
            },
        )

    assert response.status_code == 200
    mock_unassign.assert_called_once_with("namespace/project", 47, "issue", "claude")
    assert ("namespace/project", 47, "claude") not in webhooks._RECENT_UNASSIGNS


@pytest.mark.asyncio
async def test_timeout_unassign_suppresses_success_block_skip_log(monkeypatch, caplog):
    """Once the timeout path unassigns, the success-completion block must not log its skip message."""
    import logging

    webhooks._RECENT_UNASSIGNS.clear()

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _make_timeout_assigned_resolver())

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)
    monkeypatch.setattr(webhooks, "dispatch_agents", _make_timed_out_dispatch())

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)
    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    # Capture INFO logs from the webhook logger (which uses structlog->stdlib bridge).
    monkeypatch.setattr(webhooks.LOGGER, "propagate", True)
    caplog.set_level(logging.INFO, logger=webhooks.LOGGER.name)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=_assigned_issue_payload(48),
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-timeout-suppresses-skip-log",
            },
        )

    assert response.status_code == 200
    mock_unassign.assert_called_once()  # exactly one call -- success block did not re-run
    skip_msg = "Auto-unassign skipped: agent task did not succeed"
    for record in caplog.records:
        assert skip_msg not in record.getMessage(), (
            "Success block should not log the skip message when timeout path already unassigned"
        )


@pytest.mark.asyncio
async def test_timeout_unassign_only_once_for_multiple_timed_out_results(monkeypatch):
    """If multiple results time out for the assigned agent, only one unassign call should fire."""
    webhooks._RECENT_UNASSIGNS.clear()

    setup_common_patches(monkeypatch)
    monkeypatch.setattr(settings, "enable_auto_unassign", True)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks._ROUTES, "resolve_match", _make_timeout_assigned_resolver())

    async def fake_build_context(payload):
        return {"payload": payload, "title": "Dummy", "project": "namespace/project"}

    monkeypatch.setattr(webhooks, "build_context", fake_build_context)

    async def fake_dispatch(event_uuid, tasks, context):
        return [
            {
                "agent": "claude",
                "task": "first",
                "status": "error",
                "returncode": -1,
                "timed_out": "wall_clock",
                "log_file": "/tmp/run-logs/a.out.json",
                "event_id": event_uuid,
            },
            {
                "agent": "claude",
                "task": "second",
                "status": "error",
                "returncode": -1,
                "timed_out": "inactivity",
                "log_file": "/tmp/run-logs/b.out.json",
                "event_id": event_uuid,
            },
        ]

    monkeypatch.setattr(webhooks, "dispatch_agents", fake_dispatch)

    mock_notify = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "notify_agent_termination", mock_notify)
    mock_unassign = AsyncMock(return_value=True)
    monkeypatch.setattr(webhooks, "unassign_agent", mock_unassign)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/gitlab",
            json=_assigned_issue_payload(49),
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": "uuid-timeout-multiple-results",
            },
        )

    assert response.status_code == 200
    # Both timeout comments should still be posted (one per timed-out result)
    assert mock_notify.call_count == 2
    # But only one unassign call for the assigned agent
    mock_unassign.assert_called_once_with("namespace/project", 49, "issue", "claude")


# ---------------------------------------------------------------------------
# Assign on issue creation (issue #31)
# ---------------------------------------------------------------------------
#
# These tests drive the webhook end-to-end against the REAL shipped
# config/routes.yaml (not a mocked resolver), so they exercise both the
# action-list matching and the route ordering that make assign-on-creation
# work, plus the ENABLE_ASSIGN_ON_ISSUE_CREATION gate in handle_event.


def _setup_real_shipped_registry(monkeypatch):
    """Patch webhook dependencies but keep a real RouteRegistry over the shipped
    config so route order and action-list matching are exercised for real."""
    from pathlib import Path

    from app.services.routes import RouteRegistry

    monkeypatch.setattr(settings, "gitlab_webhook_secret", "top-secret")
    monkeypatch.setattr(settings, "randomize_all_mentions", False)
    monkeypatch.setattr(settings, "all_mentions_agents", "claude,gemini,codex")
    monkeypatch.setattr(webhooks, "_DEDUP", DummyDeduplicator(True))

    shipped = Path(__file__).resolve().parents[1] / "config" / "routes.yaml"
    registry = RouteRegistry(
        str(shipped),
        reload_on_change=False,
        model_variables={
            "CLAUDE_MODEL": "claude-model",
            "GEMINI_MODEL": "Gemini 3.1 Pro (High)",
            "CODEX_MODEL": "codex-model",
        },
    )
    monkeypatch.setattr(webhooks, "_ROUTES", registry)
    _patch_build_context(monkeypatch)
    _patch_dispatch(monkeypatch)
    _patch_trigger_queue(monkeypatch)
    return registry


def _open_issue_assignee_payload(iid=71):
    """Issue Hook/open with the shipped author and an agent pre-assigned via the
    top-level assignees list (the shape GitLab sends on creation)."""
    return {
        "object_kind": "issue",
        "user": {"username": "your-username"},
        "object_attributes": {"action": "open", "iid": iid},
        "assignees": [{"username": "claude"}],
        "project": {"path_with_namespace": "namespace/project"},
    }


def _update_issue_assignee_payload(iid=72):
    """Issue Hook/update (the /assign-on-existing-issue path)."""
    return {
        "object_kind": "issue",
        "user": {"username": "your-username"},
        "object_attributes": {"action": "update", "iid": iid},
        "assignees": [{"username": "claude"}],
        "changes": {"assignees": {"previous": [], "current": [{"username": "claude"}]}},
        "project": {"path_with_namespace": "namespace/project"},
    }


async def _post_issue(payload, uuid_suffix):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/webhooks/gitlab",
            json=payload,
            headers={
                "X-Gitlab-Token": "top-secret",
                "X-Gitlab-Event": "Issue Hook",
                "X-Gitlab-Event-UUID": f"uuid-{uuid_suffix}",
            },
        )


@pytest.mark.asyncio
async def test_assign_on_issue_creation_enabled_dispatches_assign_route(monkeypatch):
    """Default (enabled): creating an issue with claude assigned dispatches the
    read-write assign route, not readonly triage."""
    webhooks._RECENT_UNASSIGNS.clear()
    _setup_real_shipped_registry(monkeypatch)
    monkeypatch.setattr(settings, "enable_assign_on_issue_creation", True)

    response = await _post_issue(_open_issue_assignee_payload(71), "assign-create-on")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["triggers"][0]["route"] == "assign-issue-claude"


@pytest.mark.asyncio
async def test_assign_on_issue_creation_disabled_falls_through_to_triage(monkeypatch):
    """Toggle off: the same create-with-assignee event falls through to
    issue-triage (pre-#31 behavior)."""
    webhooks._RECENT_UNASSIGNS.clear()
    _setup_real_shipped_registry(monkeypatch)
    monkeypatch.setattr(settings, "enable_assign_on_issue_creation", False)

    response = await _post_issue(_open_issue_assignee_payload(73), "assign-create-off")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["triggers"][0]["route"] == "issue-triage"


@pytest.mark.asyncio
async def test_assign_on_existing_issue_unaffected_by_toggle(monkeypatch):
    """The /assign-on-existing-issue path (action=update) resolves the assign
    route regardless of the toggle -- the gate only applies to open events."""
    webhooks._RECENT_UNASSIGNS.clear()
    _setup_real_shipped_registry(monkeypatch)
    monkeypatch.setattr(settings, "enable_assign_on_issue_creation", False)

    response = await _post_issue(_update_issue_assignee_payload(75), "assign-update-off")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["triggers"][0]["route"] == "assign-issue-claude"
