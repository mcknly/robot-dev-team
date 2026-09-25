"""Robot Dev Team Project
File: tests/test_review_prompts.py
Description: Regression coverage for panel-aware language in multi-agent review prompts.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from pathlib import Path

import pytest

from app.core.config import settings
from app.services import context_builder

REVIEW_PROMPTS = ("issue_review.txt", "merge_request_review.txt")

PANEL_MARKERS = (
    "Review panel context",
    "marginal value",
    "Endorse concisely",
    "Disagree explicitly",
    "Concurrence is a valid response",
    "first reviewer",
    "$CURRENT_AGENT",
    "Branch sanity check",
    "git branch --show-current",
)

STRUCTURE_MARKERS = (
    "Response structure",
    "New findings",
    "Endorsements and disagreements",
)


@pytest.mark.parametrize("prompt_file", REVIEW_PROMPTS)
def test_review_prompt_contains_panel_directives(prompt_file):
    """Each multi-agent review prompt must keep the panel-awareness language.

    These markers encode the differentiation strategy described in
    docs/SYSTEM_DESIGN.md ("Panel-aware review prompts"). Dropping any of
    them silently degrades multi-agent review quality back to the
    groupthink baseline, so this test is a regression guard rather than a
    behavioural test.
    """

    path = Path(settings.prompt_dir) / prompt_file
    body = path.read_text(encoding="utf-8")
    for marker in PANEL_MARKERS:
        assert marker in body, f"{prompt_file} is missing panel marker: {marker!r}"
    for marker in STRUCTURE_MARKERS:
        assert marker in body, f"{prompt_file} is missing structure marker: {marker!r}"


@pytest.mark.parametrize("prompt_file", REVIEW_PROMPTS)
def test_review_prompt_renders_without_unresolved_required_vars(prompt_file):
    """Sanity-check that the shipped templates render via render_prompt().

    Guards against accidental introduction of an unknown ${VAR} that
    Template.safe_substitute would leave unrendered, or syntax errors that
    would raise at runtime when an actual review fires.
    """

    rendered = context_builder.render_prompt(
        prompt_file,
        {
            "project": "group/project",
            "title": "Example title",
            "description": "Example description",
            "web_url": "https://example.test/group/project/-/issues/42",
            "extra_context": "{}",
            "current_branch": "main",
            "payload": {"object_kind": "issue"},
        },
    )

    for token in ("${PROJECT}", "${TITLE}", "${DESCRIPTION}", "${URL}", "${EXTRA}"):
        assert token not in rendered, f"{prompt_file} left {token} unrendered"
    assert "group/project" in rendered
    assert "Example title" in rendered
