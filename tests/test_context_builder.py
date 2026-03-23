"""Robot Dev Team Project
File: tests/test_context_builder.py
Description: Pytest coverage for context builder enrichment.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import json

import pytest

from app.core.config import settings
from app.services import context_builder, glab


@pytest.mark.asyncio
async def test_build_context_with_merge_request_enrichment(monkeypatch):
    event = {
        "object_kind": "merge_request",
        "object_attributes": {
            "iid": 42,
            "title": "Add new feature",
            "description": "Implements feature",
            "url": "https://gitlab.example.com/group/project/-/merge_requests/42",
        },
        "project": {"path_with_namespace": "group/project"},
        "user": {"username": "alice"},
    }

    async def fake_fetch_merge_request(project_path, iid):
        assert project_path == "group/project"
        assert iid == 42
        return {"state": "opened", "diff_stats": {"changes": 12}}

    async def fake_fetch_issue(*_args, **_kwargs):  # pragma: no cover - should not be called
        raise AssertionError("fetch_issue should not be invoked for merge_request events")

    monkeypatch.setattr(glab, "fetch_merge_request", fake_fetch_merge_request)
    monkeypatch.setattr(glab, "fetch_issue", fake_fetch_issue)

    context = await context_builder.build_context(event)

    assert context["project"] == "group/project"
    assert context["title"] == "Add new feature"
    assert context["author"] == "alice"
    assert "extra_context" in context
    extra_loaded = json.loads(context["extra_context"])
    assert extra_loaded["state"] == "opened"


def test_render_prompt_uses_context(tmp_path, monkeypatch):
    template = tmp_path / "example.txt"
    template.write_text("Title: ${TITLE}\nPayload: ${JSON}", encoding="utf-8")
    monkeypatch.setattr(settings, "prompt_dir", str(tmp_path))

    rendered = context_builder.render_prompt(
        "example.txt",
        {
            "title": "Test MR",
            "payload": {"field": "value"},
        },
    )

    assert "Title: Test MR" in rendered
    assert "\nPayload: {" in rendered


@pytest.mark.asyncio
async def test_build_context_for_note_uses_comment_body(monkeypatch):
    event = {
        "object_kind": "note",
        "object_attributes": {
            "note": "@claude-bot please check",
            "action": "comment",
        },
        "project": {"path_with_namespace": "group/project"},
        "user": {"username": "bob"},
    }

    async def fake_fetch_issue(*_args, **_kwargs):
        return None

    async def fake_fetch_merge_request(*_args, **_kwargs):
        return None

    monkeypatch.setattr(glab, "fetch_issue", fake_fetch_issue)
    monkeypatch.setattr(glab, "fetch_merge_request", fake_fetch_merge_request)

    context = await context_builder.build_context(event)

    assert context["description"] == "@claude-bot please check"


@pytest.mark.asyncio
async def test_note_on_merge_request_populates_enrichment(monkeypatch):
    event = {
        "object_kind": "note",
        "object_attributes": {
            "note": "Looks good, please merge",
            "noteable_type": "MergeRequest",
        },
        "merge_request": {"iid": 10, "source_branch": "feature/x"},
        "project": {"path_with_namespace": "group/project"},
        "user": {"username": "alice"},
    }

    async def fake_fetch_merge_request(project_path, iid):
        assert project_path == "group/project"
        assert iid == 10
        return {"state": "opened", "title": "Feature X"}

    async def fake_fetch_issue(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("fetch_issue should not be invoked for note on MR")

    monkeypatch.setattr(glab, "fetch_merge_request", fake_fetch_merge_request)
    monkeypatch.setattr(glab, "fetch_issue", fake_fetch_issue)

    context = await context_builder.build_context(event)

    assert "extra_context" in context
    extra = json.loads(context["extra_context"])
    assert extra["state"] == "opened"
    assert extra["title"] == "Feature X"


@pytest.mark.asyncio
async def test_note_on_issue_populates_enrichment(monkeypatch):
    event = {
        "object_kind": "note",
        "object_attributes": {
            "note": "Can you investigate this?",
            "noteable_type": "Issue",
        },
        "issue": {"iid": 5, "title": "Bug report"},
        "project": {"path_with_namespace": "group/project"},
        "user": {"username": "bob"},
    }

    async def fake_fetch_issue(project_path, iid):
        assert project_path == "group/project"
        assert iid == 5
        return {"state": "opened", "title": "Bug report"}

    async def fake_fetch_merge_request(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("fetch_merge_request should not be invoked for note on issue")

    monkeypatch.setattr(glab, "fetch_issue", fake_fetch_issue)
    monkeypatch.setattr(glab, "fetch_merge_request", fake_fetch_merge_request)

    context = await context_builder.build_context(event)

    assert "extra_context" in context
    extra = json.loads(context["extra_context"])
    assert extra["state"] == "opened"
    assert extra["title"] == "Bug report"


@pytest.mark.asyncio
async def test_note_on_commit_returns_no_enrichment(monkeypatch):
    event = {
        "object_kind": "note",
        "object_attributes": {
            "note": "Nice commit",
            "noteable_type": "Commit",
        },
        "project": {"path_with_namespace": "group/project"},
        "user": {"username": "carol"},
    }

    async def fake_fetch_issue(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("fetch_issue should not be invoked for note on commit")

    async def fake_fetch_merge_request(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("fetch_merge_request should not be invoked for note on commit")

    monkeypatch.setattr(glab, "fetch_issue", fake_fetch_issue)
    monkeypatch.setattr(glab, "fetch_merge_request", fake_fetch_merge_request)

    context = await context_builder.build_context(event)

    assert "extra_context" not in context


@pytest.mark.asyncio
async def test_note_on_mr_missing_parent_object_returns_no_enrichment(monkeypatch):
    """Note with noteable_type=MergeRequest but no merge_request key should not crash."""
    event = {
        "object_kind": "note",
        "object_attributes": {
            "note": "Comment on MR",
            "noteable_type": "MergeRequest",
        },
        "project": {"path_with_namespace": "group/project"},
        "user": {"username": "alice"},
    }

    async def fake_fetch_merge_request(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("fetch_merge_request should not be invoked without iid")

    async def fake_fetch_issue(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("fetch_issue should not be invoked for note on MR")

    monkeypatch.setattr(glab, "fetch_merge_request", fake_fetch_merge_request)
    monkeypatch.setattr(glab, "fetch_issue", fake_fetch_issue)

    context = await context_builder.build_context(event)

    assert "extra_context" not in context


@pytest.mark.asyncio
async def test_note_on_issue_missing_parent_object_returns_no_enrichment(monkeypatch):
    """Note with noteable_type=Issue but no issue key should not crash."""
    event = {
        "object_kind": "note",
        "object_attributes": {
            "note": "Comment on issue",
            "noteable_type": "Issue",
        },
        "project": {"path_with_namespace": "group/project"},
        "user": {"username": "bob"},
    }

    async def fake_fetch_issue(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("fetch_issue should not be invoked without iid")

    async def fake_fetch_merge_request(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("fetch_merge_request should not be invoked for note on issue")

    monkeypatch.setattr(glab, "fetch_issue", fake_fetch_issue)
    monkeypatch.setattr(glab, "fetch_merge_request", fake_fetch_merge_request)

    context = await context_builder.build_context(event)

    assert "extra_context" not in context


def test_render_prompt_includes_system_prompt(tmp_path, monkeypatch):
    (tmp_path / "system_prompt.txt").write_text("System context for ${PROJECT}", encoding="utf-8")
    (tmp_path / "example.txt").write_text("Body prompt for ${TITLE}", encoding="utf-8")
    monkeypatch.setattr(settings, "prompt_dir", str(tmp_path))

    rendered = context_builder.render_prompt(
        "example.txt",
        {
            "project": "group/project",
            "title": "Example",
            "payload": {},
        },
    )

    assert rendered.startswith("System context for group/project")
    assert "Body prompt for Example" in rendered


def test_render_prompt_includes_current_branch(tmp_path, monkeypatch):
    """CURRENT_BRANCH should be substituted from context."""
    template = tmp_path / "branch_template.txt"
    template.write_text("Branch: ${CURRENT_BRANCH}", encoding="utf-8")
    monkeypatch.setattr(settings, "prompt_dir", str(tmp_path))

    rendered = context_builder.render_prompt(
        "branch_template.txt",
        {
            "current_branch": "feature/my-branch",
            "payload": {},
        },
    )

    assert "Branch: feature/my-branch" in rendered


def test_render_prompt_current_branch_empty_when_not_set(tmp_path, monkeypatch):
    """CURRENT_BRANCH should be empty string when not in context."""
    template = tmp_path / "branch_template.txt"
    template.write_text("Branch: [${CURRENT_BRANCH}]", encoding="utf-8")
    monkeypatch.setattr(settings, "prompt_dir", str(tmp_path))

    rendered = context_builder.render_prompt(
        "branch_template.txt",
        {
            "payload": {},
        },
    )

    assert "Branch: []" in rendered
