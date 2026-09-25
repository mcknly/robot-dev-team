"""Robot Dev Team Project
File: app/api/webhooks.py
Description: FastAPI router handling GitLab webhook callbacks.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import asyncio
import random
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, status

from app.core.config import settings
from app.core.logging import get_logger
from app.services.agents import AgentKilledError, dispatch_agents
from app.services.context_builder import build_context
from app.services.deduplication import create_deduplicator
from app.services.glab import notify_agent_termination, notify_backup_created, unassign_agent
from app.services.routes import RouteMatch, RouteRegistry
from app.services.trigger_queue import TriggerQueue, TriggerWorkItem

router = APIRouter()
LOGGER = get_logger(__name__)
_DEDUP = create_deduplicator()
_ROUTES = RouteRegistry(settings.route_config_path, reload_on_change=settings.debug_reload_routes)
_TRIGGER_QUEUE = TriggerQueue(hold_seconds=settings.mention_hold_seconds)

# Self-unassign suppression: tracks (project, iid, agent) tuples recently
# unassigned by the app itself, so the resulting webhook does not retrigger
# the agent.  Entries expire after _UNASSIGN_TTL_SECONDS.
_RECENT_UNASSIGNS: Dict[Tuple[str, int, str], float] = {}
_UNASSIGN_TTL_SECONDS = 60


def _record_self_unassign(project_path: str, iid: int, agent: str) -> None:
    """Record that the app just unassigned an agent (to suppress the echo webhook)."""
    _RECENT_UNASSIGNS[(project_path, iid, agent.lower())] = time.monotonic()


def _is_self_unassign(project_path: str, iid: int, agents: List[str]) -> bool:
    """Check if any agent in the list was recently self-unassigned from this resource."""
    now = time.monotonic()
    # Prune expired entries
    expired = [k for k, t in _RECENT_UNASSIGNS.items() if now - t > _UNASSIGN_TTL_SECONDS]
    for k in expired:
        del _RECENT_UNASSIGNS[k]
    # Check for match
    for agent in agents:
        key = (project_path, iid, agent.lower())
        if key in _RECENT_UNASSIGNS:
            del _RECENT_UNASSIGNS[key]
            return True
    return False


@router.post("/webhooks/gitlab")
async def gitlab_webhook(request: Request) -> Dict[str, Any]:
    """Process GitLab webhook payloads and trigger matching agents."""

    await _check_secret(request)

    event_uuid = request.headers.get("X-Gitlab-Event-UUID") or str(uuid.uuid4())
    if not await _DEDUP.mark(event_uuid):
        return {"status": "ignored", "reason": "duplicate", "event_uuid": event_uuid}

    payload = await _extract_json(request)
    event_name = request.headers.get("X-Gitlab-Event") or payload.get("object_kind") or "unknown"
    action = _extract_action(payload)
    author = _extract_author(payload)
    labels = _extract_labels(payload)
    body = _extract_body(payload)
    assignees = _extract_assignees(payload)

    # Suppress webhooks that are echoes of the app's own actions (self-unassign
    # updates and the system notes GitLab emits for them).
    suppression = _suppression_reason(event_name, action, payload, assignees, event_uuid)
    if suppression is not None:
        return {"status": "ignored", "reason": suppression, "event_uuid": event_uuid}

    mentions = _extract_mentions(payload)
    mentions, expanded_from_all = _expand_all_mention(mentions, author)
    mentions = _filter_assigned_mentions(event_name, body, mentions)
    mentions = _filter_self_mention(author, mentions)

    work_items, ignored_triggers = await _resolve_work_items(
        event_uuid=event_uuid,
        event_name=event_name,
        action=action,
        author=author,
        labels=labels,
        body=body,
        assignees=assignees,
        mentions=mentions,
        expanded_from_all=expanded_from_all,
        payload=payload,
    )

    if not work_items:
        LOGGER.info(
            "No matching routes: event=%s action=%s author=%s labels=%s mentions=%s assignees=%s",
            event_name,
            action,
            author,
            labels,
            mentions,
            assignees,
        )
        return {"status": "ignored", "reason": "no-routes", "event_uuid": event_uuid}

    processed_triggers = await _TRIGGER_QUEUE.enqueue_many(work_items)
    agents_flat: List[Dict[str, Any]] = [
        agent
        for trigger in processed_triggers
        for agent in trigger.get("agents", [])
    ]

    all_triggers = processed_triggers + ignored_triggers

    return {
        "status": "ok",
        "event_uuid": event_uuid,
        "event": event_name,
        "action": action,
        "agents": agents_flat,
        "triggers": all_triggers,
    }


def _is_self_unassign_echo(payload: Dict[str, Any], assignees: List[str]) -> bool:
    """True when an update webhook is an echo of the app's own unassign action.

    Checks both the current assignees and any agents removed in this change event
    (the post-change assignees list is empty on a full unassign).
    """
    project = (payload.get("project") or {}).get("path_with_namespace") or ""
    attrs = payload.get("object_attributes") or {}
    iid = attrs.get("iid")
    if not (project and iid is not None):
        return False

    # Build a combined list of agents to check: current assignees plus any
    # agents removed in this change event.
    agents_to_check = list(assignees)
    changes = payload.get("changes") or {}
    assignee_changes = changes.get("assignees") or {}
    previous = assignee_changes.get("previous") or []
    current = assignee_changes.get("current") or []
    current_set = {_coerce_username(a).lower() for a in current if _coerce_username(a)}
    for prev_assignee in previous:
        username = _coerce_username(prev_assignee)
        if username and username.lower() not in current_set and username not in agents_to_check:
            agents_to_check.append(username)

    if agents_to_check and _is_self_unassign(project, int(iid), agents_to_check):
        LOGGER.info(
            "Suppressed self-unassign echo webhook",
            extra={"project": project, "iid": iid, "agents_checked": agents_to_check},
        )
        return True
    return False


def _suppression_reason(
    event_name: str,
    action: Optional[str],
    payload: Dict[str, Any],
    assignees: List[str],
    event_uuid: str,
) -> Optional[str]:
    """Return an ignore reason if this event echoes one of the app's own actions.

    Covers (1) update webhooks caused by the app's own unassign, and (2) the
    system notes GitLab emits for an unassign ("unassigned @claude"), whose text
    would otherwise match mention routes erroneously.
    """
    if action == "update" and _is_self_unassign_echo(payload, assignees):
        return "self-unassign"

    if event_name == "Note Hook":
        note_attrs = payload.get("object_attributes") or {}
        if note_attrs.get("system") and _is_unassign_system_note(payload):
            LOGGER.info(
                "Suppressed system note from unassign action",
                extra={"event_uuid": event_uuid, "note": note_attrs.get("note", "")[:120]},
            )
            return "system-unassign-note"

    return None


async def _resolve_work_items(
    *,
    event_uuid: str,
    event_name: str,
    action: Optional[str],
    author: Optional[str],
    labels: List[str],
    body: Optional[str],
    assignees: List[str],
    mentions: List[str],
    expanded_from_all: bool,
    payload: Dict[str, Any],
) -> Tuple[List[TriggerWorkItem], List[Dict[str, Any]]]:
    """Resolve routes for the event and build the per-trigger work items.

    Handles the single-mention (<=1) path and the multi-mention path (a base
    match plus one split trigger per mention, with @all-origin randomization).
    Returns ``(work_items, ignored_triggers)``; context is built at most once.
    """
    context: Optional[Dict[str, Any]] = None
    work_items: List[TriggerWorkItem] = []
    ignored_triggers: List[Dict[str, Any]] = []

    # Assign-on-issue-creation gate (issue #31): when the feature is disabled,
    # skip assignment routes for Issue-open events so a create-with-assignee
    # falls through to issue-triage (the pre-#31 behavior). Enabled by default,
    # in which case `gate` is None and resolution is unfiltered.
    assign_gate: Optional[Callable[[Any], bool]] = (
        _exclude_assignee_rules
        if (
            not settings.enable_assign_on_issue_creation
            and event_name == "Issue Hook"
            and action == "open"
        )
        else None
    )

    async def ensure_context() -> Dict[str, Any]:
        nonlocal context
        if context is None:
            context = await build_context(payload)
        return context

    if len(mentions) <= 1:
        match = _ROUTES.resolve_match(
            event_name, action, author, labels, mentions,
            body=body, assignees=assignees, rule_predicate=assign_gate,
        )
        if match:
            ctx = await ensure_context()
            _log_route_match(
                match=match, event_id=event_uuid, event_name=event_name,
                action=action, author=author, labels=labels,
                mentions=mentions, assignees=assignees,
            )
            work_items.append(
                _create_work_item(
                    event_id=event_uuid, base_event_uuid=event_uuid,
                    event_name=event_name, action=action, author=author,
                    labels=labels, mentions=mentions, match=match,
                    context=ctx, payload=payload,
                )
            )
        return work_items, ignored_triggers

    base_match = _ROUTES.resolve_match(
        event_name, action, author, labels, mentions,
        body=body, assignees=assignees,
        rule_predicate=_compose(_exclude_single_mention_rules, assign_gate),
    )
    if base_match:
        ctx = await ensure_context()
        _log_route_match(
            match=base_match, event_id=event_uuid, event_name=event_name,
            action=action, author=author, labels=labels,
            mentions=mentions, assignees=assignees,
        )
        work_items.append(
            _create_work_item(
                event_id=event_uuid, base_event_uuid=event_uuid,
                event_name=event_name, action=action, author=author,
                labels=labels, mentions=mentions, match=base_match,
                context=ctx, payload=payload,
            )
        )

    # Issue #14: randomize the per-mention dispatch order, but only when the
    # trigger originated from an @all/@agents expansion. Explicit lists like
    # "@claude @gemini @codex" stay deterministic so authors can pin order.
    # base_match above keeps the unshuffled list so its diagnostics are stable.
    dispatch_mentions = list(mentions)
    if (
        expanded_from_all
        and settings.randomize_all_mentions
        and len(dispatch_mentions) > 1
    ):
        random.shuffle(dispatch_mentions)
        LOGGER.info(
            "Randomized @all dispatch order",
            extra={
                "event_uuid": event_uuid,
                "original": mentions,
                "dispatched": dispatch_mentions,
            },
        )

    for mention in dispatch_mentions:
        trigger_id = _format_trigger_event_id(event_uuid, mention)
        work_item = await _resolve_mention_trigger(
            mention=mention,
            trigger_id=trigger_id,
            event_uuid=event_uuid,
            event_name=event_name,
            action=action,
            author=author,
            labels=labels,
            body=body,
            assignees=assignees,
            assign_gate=assign_gate,
            payload=payload,
            ensure_context=ensure_context,
        )
        if work_item is None:
            ignored_triggers.append(
                {
                    "status": "ignored",
                    "reason": "no-routes",
                    "event_id": trigger_id,
                    "mentions": [mention],
                }
            )
            continue
        work_items.append(work_item)

    return work_items, ignored_triggers


async def _resolve_mention_trigger(
    *,
    mention: str,
    trigger_id: str,
    event_uuid: str,
    event_name: str,
    action: Optional[str],
    author: Optional[str],
    labels: List[str],
    body: Optional[str],
    assignees: List[str],
    assign_gate: Optional[Callable[[Any], bool]],
    payload: Dict[str, Any],
    ensure_context: Callable[[], Any],
) -> Optional[TriggerWorkItem]:
    """Resolve a single split mention into a work item, or None if no route matches."""
    split_mentions = [mention]
    match = _ROUTES.resolve_match(
        event_name, action, author, labels, split_mentions,
        body=body, assignees=assignees,
        rule_predicate=_compose(_only_single_mention_rules, assign_gate),
    )
    if not match:
        return None

    ctx = await ensure_context()
    _log_route_match(
        match=match, event_id=trigger_id, event_name=event_name,
        action=action, author=author, labels=labels,
        mentions=split_mentions, assignees=assignees,
    )
    return _create_work_item(
        event_id=trigger_id, base_event_uuid=event_uuid,
        event_name=event_name, action=action, author=author,
        labels=labels, mentions=split_mentions, match=match,
        context=ctx, payload=payload,
    )


async def _check_secret(request: Request) -> None:
    secret = settings.gitlab_webhook_secret
    if not secret:
        return
    token = request.headers.get("X-Gitlab-Token")
    if token != secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook token")


async def _extract_json(request: Request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:  # pragma: no cover - FastAPI handles detailed error response
        LOGGER.error("Failed to parse webhook payload", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from exc

    if isinstance(data, dict):
        return data

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid JSON payload",
    )


def _extract_action(payload: Dict[str, Any]) -> str:
    attrs = payload.get("object_attributes") or {}
    return attrs.get("action") or attrs.get("state") or payload.get("event_type") or ""


def _extract_author(payload: Dict[str, Any]) -> str:
    user = payload.get("user")
    if isinstance(user, dict):
        return user.get("username") or user.get("name") or ""
    if isinstance(user, str):
        return user
    return payload.get("user_username") or ""


def _extract_labels(payload: Dict[str, Any]) -> List[str]:
    labels: List[str] = []
    payload_labels = payload.get("labels") or []
    attrs = payload.get("object_attributes") or {}
    attr_labels = attrs.get("labels") or []
    for collection in (payload_labels, attr_labels):
        for label in collection:
            if isinstance(label, dict):
                title = label.get("title") or label.get("name")
                if title:
                    labels.append(title)
            elif isinstance(label, str):
                labels.append(label)
    return labels


def _extract_body(payload: Dict[str, Any]) -> Optional[str]:
    """Extract the body/comment text from the webhook payload."""
    attrs = payload.get("object_attributes") or {}
    for field in ("note", "description", "body"):
        value = attrs.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_assignees(payload: Dict[str, Any]) -> List[str]:
    """Extract assignee usernames from the webhook payload.

    When ``changes.assignees`` is present (indicating an assignee-change
    event), only ``changes.assignees.current`` is used -- it reflects the
    authoritative post-change state.  The top-level ``payload.assignees``
    field is used only as a fallback when the ``changes`` block is absent.

    This prevents unassign events from incorrectly reporting the removed
    user: a stale top-level ``payload.assignees`` can still list the agent
    that was just removed, but ``changes.assignees.current`` will not.
    """
    assignees: List[str] = []

    changes = payload.get("changes") or {}
    assignee_changes = changes.get("assignees") or {}

    if assignee_changes:
        # Authoritative: use only post-change state
        current_assignees = assignee_changes.get("current") or []
        for assignee in current_assignees:
            username = _coerce_username(assignee)
            if username and username not in assignees:
                assignees.append(username)
        LOGGER.debug(
            "Assignees extracted from changes.assignees.current",
            extra={"assignees": assignees},
        )
        return assignees

    # Fallback: no detailed change info available
    payload_assignees = payload.get("assignees") or []
    for assignee in payload_assignees:
        username = _coerce_username(assignee)
        if username and username not in assignees:
            assignees.append(username)

    LOGGER.debug(
        "Assignees extracted from top-level payload.assignees (fallback)",
        extra={"assignees": assignees},
    )
    return assignees


def _extract_newly_assigned_agent(payload: Dict[str, Any]) -> Optional[str]:
    """Detect if an agent was newly assigned via assignment action.

    Checks two sources in order:
    1. changes.assignees (current vs previous) -- preferred when available
    2. Top-level payload.assignees -- fallback when changes block is missing

    Returns the username of the assigned agent, or None if no agent was assigned.
    """
    agent_usernames = _parse_all_mentions_agents(settings.all_mentions_agents)

    # Primary: use changes.assignees when available
    changes = payload.get("changes") or {}
    assignee_changes = changes.get("assignees") or {}
    current_assignees = assignee_changes.get("current") or []
    previous_assignees = assignee_changes.get("previous") or []

    if current_assignees:
        current_usernames = {_coerce_username(a).lower() for a in current_assignees if _coerce_username(a)}
        previous_usernames = {_coerce_username(a).lower() for a in previous_assignees if _coerce_username(a)}
        newly_assigned = current_usernames - previous_usernames
        for username in newly_assigned:
            if username in agent_usernames:
                return username
        return None

    # Fallback: check top-level assignees when changes.assignees is absent
    payload_assignees = payload.get("assignees") or []
    for assignee in payload_assignees:
        username = (_coerce_username(assignee) or "").lower()
        if username and username in agent_usernames:
            LOGGER.debug(
                "Assignment detected via top-level assignees fallback",
                extra={"agent": username},
            )
            return username

    return None


ALL_MENTION_ALIASES = frozenset(["all", "agents"])


def _parse_all_mentions_agents(raw_value: str) -> List[str]:
    if not raw_value:
        return []
    parts = [item.strip() for item in raw_value.split(",")]
    return [item.lower() for item in parts if item]


def _expand_all_mention(mentions: List[str], author: str = "") -> Tuple[List[str], bool]:
    """Expand @all/@agents into individual agent mentions.

    Returns ``(mentions, expanded_from_all)``. ``expanded_from_all`` is True
    only when an alias was present *and* expansion actually occurred; it gates
    dispatch-order randomization (see ``RANDOMIZE_ALL_MENTIONS``).

    Expansion is suppressed when the comment author is itself a known agent
    (issue #16): an agent writing @all must not fan out to the whole roster
    (including itself), which would create a self-trigger loop. The alias tokens
    are still stripped so they do not match anything downstream.
    """
    lower_mentions = [m.lower() for m in mentions]
    if not any(alias in lower_mentions for alias in ALL_MENTION_ALIASES):
        return mentions, False

    # Drop the alias tokens themselves; they are never real agent usernames.
    stripped = [m for m in mentions if m.lower() not in ALL_MENTION_ALIASES]

    agent_usernames = set(_parse_all_mentions_agents(settings.all_mentions_agents))
    if (author or "").lower() in agent_usernames:
        LOGGER.info(
            "Suppressed @all expansion: author is a known agent",
            extra={"author": author},
        )
        return stripped, False

    expanded = list(stripped)
    existing_lower = {m.lower() for m in expanded}

    for agent in _parse_all_mentions_agents(settings.all_mentions_agents):
        if agent not in existing_lower:
            expanded.append(agent)

    return expanded, True


def _filter_self_mention(author: str, mentions: List[str]) -> List[str]:
    """Drop the author's own agent username from the mention list (issue #16).

    Prevents an agent from being dispatched against a comment it authored
    itself. Only applies when the author is a known agent; human authors are
    left untouched.
    """
    author_lower = (author or "").lower()
    if not author_lower:
        return mentions
    agent_usernames = set(_parse_all_mentions_agents(settings.all_mentions_agents))
    if author_lower not in agent_usernames:
        return mentions
    filtered = [m for m in mentions if m.lower() != author_lower]
    if len(filtered) != len(mentions):
        LOGGER.info(
            "Filtered self-mention from dispatch",
            extra={"author": author, "mentions": mentions},
        )
    return filtered


def _extract_mentions(payload: Dict[str, Any]) -> List[str]:
    attrs = payload.get("object_attributes") or {}

    structured_sources = [
        payload.get("mentions"),
        attrs.get("mentions"),
        attrs.get("mentioned_users"),
    ]
    structured_mentions = _collect_structured_mentions(structured_sources)

    text_fields = [
        attrs.get("note"),
        attrs.get("description"),
        attrs.get("body"),
    ]
    textual_mentions = _collect_textual_mentions(text_fields)

    return _deduplicate_usernames(structured_mentions + textual_mentions)


def _collect_structured_mentions(collections: List[Any]) -> List[str]:
    results: List[str] = []
    for collection in collections:
        if not collection:
            continue
        for item in collection:
            username = _coerce_username(item)
            if username:
                results.append(username)
    return results


def _collect_textual_mentions(texts: List[Any]) -> List[str]:
    mentions: List[str] = []
    for text in texts:
        if isinstance(text, str) and text:
            mentions.extend(_parse_mentions_from_text(text))
    return mentions


def _coerce_username(value: Any) -> str:
    if isinstance(value, dict):
        return value.get("username") or value.get("name") or ""
    if isinstance(value, str):
        return value
    return ""


def _deduplicate_usernames(candidates: List[str]) -> List[str]:
    seen = set()
    ordered_unique: List[str] = []
    for username in candidates:
        if username and username not in seen:
            seen.add(username)
            ordered_unique.append(username)
    return ordered_unique


# Line-level container markers used by ``_strip_line_containers``. These are
# anchored at line start (allowing up to three leading spaces, per CommonMark)
# so a ``>`` or a redirect ``>`` appearing mid-line is never mistaken for a
# blockquote (issue #17). ``>>>`` on its own line toggles a GitLab-flavored
# multiline blockquote whose interior lines need not be prefixed with ``>``.
_BLOCKQUOTE_RE = re.compile(r"^ {0,3}>")
_MULTILINE_QUOTE_RE = re.compile(r"^ {0,3}>>>\s*$")
# Indented code: four or more leading spaces, or up to three spaces followed by
# a tab. CommonMark reckons indentation by four-column tab stops, so a tab reached
# after 0-3 leading spaces still opens an indented code block, not only a tab in
# column 1 (CommonMark 0.31.2, Tabs).
_INDENTED_CODE_RE = re.compile(r"^(?: {4,}| {0,3}\t)")


def _blank_fence(match: "re.Match[str]") -> str:
    """Replace a fenced block with as many newlines as it spanned so the
    line structure survives for the subsequent line-level scan. A single-line
    fence spans no newline, so fall back to a space to keep the surrounding
    words separated (avoids ``word```code```word`` -> ``wordword``)."""
    return "\n" * match.group(0).count("\n") or " "


def _strip_line_containers(text: str) -> str:
    """Blank out blockquoted and indented-code lines so literal @mentions
    quoted or shown as code do not parse as live mentions (issue #17).

    Conservative, over-strip-biased subset (see docs/ROUTES.md): under-stripping
    a quoted ``@all`` re-fans it out to the whole roster (the #16 incident),
    while over-stripping merely drops a recoverable live mention. When in doubt
    we strip. Covered:

    - Blockquote lines: ``^ {0,3}>`` (nested ``> >`` and up-to-three-space
      prefixes included).
    - Lazy blockquote continuations: a non-blank line with no ``>`` prefix that
      immediately follows a blockquote line (no blank separator) is still part
      of the quoted paragraph as GitLab/CommonMark renders it, so it is stripped
      too. A blank line closes the open quote.
    - GitLab multiline blockquotes: a bare ``>>>`` line toggles a region whose
      interior lines are stripped even without a ``>`` prefix; an unclosed
      region strips to end-of-text.
    - Indented code blocks: four-space (or up-to-three-space + tab) indentation.
      Every qualifying indented line is stripped, without trying to distinguish a
      code block from a paragraph/list continuation. A per-line blank/not-blank
      flag cannot model block containment (it under-strips indented code after a
      heading or blockquote -- both fan-out shapes), so following the over-strip
      contract we strip all of them and accept dropping the occasional indented
      list-continuation mention as the recoverable, safe trade-off.

    Fenced blocks are handled by ``_strip_code_spans`` before this runs, so the
    lines seen here are already outside any fence.
    """
    out: List[str] = []
    in_multiline_quote = False
    in_blockquote = False
    for line in text.split("\n"):
        if _MULTILINE_QUOTE_RE.match(line):
            in_multiline_quote = not in_multiline_quote
            in_blockquote = False
            out.append("")
            continue
        if in_multiline_quote:
            out.append("")
            continue
        if line.strip() == "":
            # A blank line closes an open blockquote paragraph (lazy
            # continuation only spans consecutive non-blank lines).
            in_blockquote = False
            out.append(line)
            continue
        if _BLOCKQUOTE_RE.match(line):
            in_blockquote = True
            out.append("")
            continue
        if in_blockquote:
            out.append("")
            continue
        if _INDENTED_CODE_RE.match(line):
            out.append("")
            continue
        out.append(line)
    return "\n".join(out)


def _strip_code_spans(text: str) -> str:
    """Remove fenced, blockquoted, indented, and inline code so literal
    @mentions discussed in code examples or quoted as terms (e.g. ``@all`` in
    backticks) are not parsed as live mentions (issues #16, #17).

    Order matters: fenced blocks (``` and ~~~) are blanked first -- preserving
    line count -- so the line-level scan for blockquotes and indented code runs
    outside any open fence. Inline backtick spans are removed last."""
    text = re.sub(r"```.*?```", _blank_fence, text, flags=re.DOTALL)
    text = re.sub(r"~~~.*?~~~", _blank_fence, text, flags=re.DOTALL)
    text = _strip_line_containers(text)
    # Paired N-backtick inline spans (N >= 1): the backreference ensures the
    # closing run is the same length as the opening run, covering ``@all``
    # and `` ``@all`` `` alike.
    text = re.sub(r"(`+)(.+?)\1", " ", text)
    return text


# Mention boundary: refuse to treat ``@`` as a mention when the preceding
# character is part of an email local-part, URL path, or hostname-like token
# (issue #16 follow-up). This excludes ``support@all``, ``https://x/@all``,
# and similar embedded ``@`` sequences while still matching at start-of-string,
# after whitespace, or after typical punctuation like ``(`` or ``,``.
_MENTION_PATTERN = re.compile(r"(?<![A-Za-z0-9_./-])@([A-Za-z0-9_][A-Za-z0-9_.-]*)")


def _parse_mentions_from_text(text: str) -> List[str]:
    text = _strip_code_spans(text)
    mentions: List[str] = []
    for match in _MENTION_PATTERN.finditer(text):
        username = match.group(1).rstrip(".")
        if username:
            mentions.append(username)
    return mentions


def _is_unassign_system_note(payload: Dict[str, Any]) -> bool:
    """Detect GitLab system notes generated by unassign actions.

    When an agent is unassigned, GitLab emits a Note Hook with
    ``object_attributes.system == true`` and a note like "unassigned @agent".
    These notes contain agent @mentions that would otherwise match mention
    routes and trigger erroneous dispatches.

    Additionally checks the self-unassign registry: if the app recently
    unassigned the agent, the system note is definitely an echo and should
    be suppressed.
    """
    attrs = payload.get("object_attributes") or {}
    note_text = attrs.get("note") or ""
    # Match GitLab system notes for unassignment (e.g. "unassigned @claude")
    if not re.match(r"unassigned\s+@", note_text, re.IGNORECASE):
        return False
    # Extract which agents were mentioned in the unassign note
    mentioned = _parse_mentions_from_text(note_text)
    if not mentioned:
        return False
    # Check against the self-unassign registry for a tighter match
    project = (payload.get("project") or {}).get("path_with_namespace") or ""
    # Resolve the parent resource iid (issue or MR the note belongs to)
    iid = None
    if payload.get("issue"):
        iid = payload["issue"].get("iid")
    elif payload.get("merge_request"):
        iid = payload["merge_request"].get("iid")
    if project and iid is not None:
        if _is_self_unassign(project, int(iid), mentioned):
            return True
    # Even without a registry match, suppress unassign system notes that
    # mention known agents to prevent erroneous dispatches
    agent_usernames = set(_parse_all_mentions_agents(settings.all_mentions_agents))
    return any(m.lower() in agent_usernames for m in mentioned)


def _filter_assigned_mentions(
    event_name: str,
    body: Optional[str],
    mentions: List[str],
) -> List[str]:
    """Remove agents from mentions when they are also /assign targets in the same note.

    When a Note Hook contains both ``@agent`` and ``/assign @agent``, GitLab
    sends two separate webhooks: one Note Hook (matching mention routes) and
    one Issue/MR Hook (matching assignment routes).  Suppressing the mention
    for the assigned agent avoids a noisy read-only reply that precedes the
    real read-write assignment trigger.

    Only applies to Note Hook events.  Non-note events and notes without
    ``/assign`` are returned unchanged.
    """
    if event_name != "Note Hook" or not body or not mentions:
        return mentions

    assign_pattern = re.compile(
        r"(?:^|\n)\s*/assign\s+@([A-Za-z0-9_][A-Za-z0-9_.-]*)",
    )
    assigned_users = {m.group(1).lower() for m in assign_pattern.finditer(body)}

    if not assigned_users:
        return mentions

    filtered = [m for m in mentions if m.lower() not in assigned_users]
    if len(filtered) < len(mentions):
        suppressed = [m for m in mentions if m.lower() in assigned_users]
        LOGGER.info(
            "Suppressed mention(s) for /assign target(s): %s",
            suppressed,
        )
    return filtered


def _format_trigger_event_id(event_uuid: str, mention: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in mention)
    return f"{event_uuid}-{safe}" if mention else event_uuid


def _exclude_single_mention_rules(rule) -> bool:
    return len(rule.mentions) != 1


def _only_single_mention_rules(rule) -> bool:
    return len(rule.mentions) == 1


def _exclude_assignee_rules(rule) -> bool:
    """Exclude assignee-matching routes (issue #31).

    Used to gate assign-on-issue-creation: when
    ``ENABLE_ASSIGN_ON_ISSUE_CREATION`` is off, this predicate drops the
    read-write assignment routes from an ``Issue Hook``/``open`` resolution so a
    create-with-assignee event falls through to ``issue-triage`` (the pre-#31
    behavior). ``/assign`` on an existing issue (``action: update``) is
    unaffected because the gate is only applied to ``open`` events.
    """
    return not rule.assignees


def _compose(*predicates):
    """Combine rule predicates with logical AND, ignoring ``None`` entries.

    Returns ``None`` when no predicate applies so callers pass through the
    registry's default (unfiltered) resolution unchanged.
    """
    active = [p for p in predicates if p is not None]
    if not active:
        return None
    if len(active) == 1:
        return active[0]
    return lambda rule: all(p(rule) for p in active)


def _resolve_resource(ctx: Dict[str, Any]) -> Tuple[Optional[str], Any, Optional[str]]:
    """Extract project_path, iid, and resource_type from the context."""
    project_path = ctx.get("project")
    event_payload = ctx.get("payload", {})
    kind = event_payload.get("object_kind")
    iid = None
    resource_type = None

    if kind in ("issue", "merge_request"):
        resource_type = kind
        iid = event_payload.get("object_attributes", {}).get("iid")
    elif kind == "note":
        if event_payload.get("issue"):
            resource_type = "issue"
            iid = event_payload.get("issue", {}).get("iid")
        elif event_payload.get("merge_request"):
            resource_type = "merge_request"
            iid = event_payload.get("merge_request", {}).get("iid")

    return project_path, iid, resource_type


def _valid_resource(ctx: Dict[str, Any]) -> Optional[Tuple[str, int, str]]:
    """Return (project_path, int(iid), resource_type) when all are present, else None."""
    project_path, iid, resource_type = _resolve_resource(ctx)
    if project_path and iid and resource_type:
        return project_path, int(iid), resource_type
    return None


async def _cleanup_on_kill(ctx: Dict[str, Any], assigned_agent: Optional[str]) -> None:
    """Notify and auto-unassign the assigned agent after a manual kill."""
    if not assigned_agent:
        return
    try:
        resolved = _valid_resource(ctx)
        if resolved is None:
            project_path, iid, resource_type = _resolve_resource(ctx)
            LOGGER.warning(
                "Kill-path unassign/notify skipped: missing context",
                extra={"project": project_path, "iid": iid, "resource_type": resource_type},
            )
            return
        project_path, iid, resource_type = resolved
        notified = await notify_agent_termination(
            project_path, iid, resource_type,
            assigned_agent, reason="Manual Kill",
            details="Operator terminated the agent via the dashboard.",
        )
        if notified:
            LOGGER.info(
                "Termination comment posted successfully",
                extra={"agent": assigned_agent, "type": resource_type, "iid": iid},
            )
        else:
            LOGGER.warning(
                "Termination comment failed, proceeding with cleanup",
                extra={"agent": assigned_agent, "type": resource_type, "iid": iid},
            )
        if settings.enable_auto_unassign:
            LOGGER.info(
                "Auto-unassigning agent after manual kill",
                extra={"agent": assigned_agent, "type": resource_type, "iid": iid},
            )
            success = await unassign_agent(project_path, iid, resource_type, assigned_agent)
            if success:
                _record_self_unassign(project_path, iid, assigned_agent)
            else:
                LOGGER.warning(
                    "Auto-unassign glab call returned failure after kill",
                    extra={"agent": assigned_agent, "type": resource_type, "iid": iid},
                )
    except Exception as exc:
        LOGGER.error("Failed to handle kill-path cleanup", exc_info=exc)


def _should_unassign_timeout(
    result: Dict[str, Any], assigned_agent: Optional[str], already_unassigned: bool
) -> bool:
    """True when a timed-out result should trigger auto-unassign of the assigned agent."""
    return bool(
        settings.enable_auto_unassign
        and assigned_agent
        and not already_unassigned
        and (result.get("agent") or "").lower() == assigned_agent
    )


async def _handle_one_timeout(
    result: Dict[str, Any],
    ctx: Dict[str, Any],
    assigned_agent: Optional[str],
    already_unassigned: bool,
) -> bool:
    """Notify (and possibly auto-unassign) for a single timed-out result.

    Returns True only when this call successfully unassigned the assigned agent.
    Notification and unassign are independent: unassign runs even if the comment
    fails.
    """
    timeout_reason = result.get("timed_out")
    try:
        resolved = _valid_resource(ctx)
        if resolved is None:
            return False
        project_path, iid, resource_type = resolved
        agent_name = result.get("agent", "unknown")
        log_file = result.get("log_file", "")
        details = (
            f"Agent exceeded the configured "
            f"{'wall-clock' if timeout_reason == 'wall_clock' else 'inactivity'} "
            f"time limit."
        )
        if log_file:
            details += f"\n\nRun log: `{log_file}`"
        await notify_agent_termination(
            project_path, iid, resource_type,
            agent_name, reason="Timeout", details=details,
        )
        if _should_unassign_timeout(result, assigned_agent, already_unassigned):
            assert assigned_agent is not None  # guaranteed by _should_unassign_timeout
            LOGGER.info(
                "Auto-unassigning agent after timeout",
                extra={
                    "agent": assigned_agent,
                    "type": resource_type,
                    "iid": iid,
                    "timeout_reason": timeout_reason,
                },
            )
            success = await unassign_agent(project_path, iid, resource_type, assigned_agent)
            if success:
                _record_self_unassign(project_path, iid, assigned_agent)
                # Set only on success: a False return leaves the flag clear so the
                # next matching timed-out result can retry (handles transient glab
                # failures). On True we stop further attempts in this dispatch.
                return True
            LOGGER.warning(
                "Auto-unassign glab call returned failure after timeout",
                extra={"agent": assigned_agent, "type": resource_type, "iid": iid},
            )
    except Exception as exc:
        LOGGER.error("Failed to post timeout notification", exc_info=exc)
    return False


async def _notify_and_unassign_timeouts(
    results: List[Dict[str, Any]], ctx: Dict[str, Any], assigned_agent: Optional[str]
) -> bool:
    """Notify on every timed-out result, auto-unassigning the assigned agent once.

    Returns whether the assigned agent was unassigned via the timeout path.
    """
    unassigned_on_timeout = False
    for result in results:
        if not result.get("timed_out"):
            continue
        if await _handle_one_timeout(result, ctx, assigned_agent, unassigned_on_timeout):
            unassigned_on_timeout = True
    return unassigned_on_timeout


async def _notify_backups(results: List[Dict[str, Any]], ctx: Dict[str, Any]) -> None:
    """Post a backup-branch notification for every result that created backups."""
    for result in results:
        backups = result.get("backups") or []
        if not backups:
            continue
        try:
            resolved = _valid_resource(ctx)
            if resolved is None:
                continue
            project_path, iid, resource_type = resolved
            agent_name = result.get("agent", "unknown")
            for backup in backups:
                await notify_backup_created(
                    project_path, iid, resource_type,
                    agent_name, backup["branch"],
                    backup_reason=backup.get("reason", ""),
                )
        except Exception as exc:
            LOGGER.error("Failed to post backup notification", exc_info=exc)


async def _auto_unassign_on_success(
    results: List[Dict[str, Any]], ctx: Dict[str, Any], assigned_agent: str
) -> None:
    """Unassign the assigned agent after a successful completion of its task.

    The caller guarantees ``assigned_agent`` is set (auto-unassign is gated on it).
    """
    try:
        resolved = _valid_resource(ctx)
        if resolved is None:
            project_path, iid, resource_type = _resolve_resource(ctx)
            LOGGER.warning(
                "Auto-unassign skipped: missing context",
                extra={"project": project_path, "iid": iid, "resource_type": resource_type},
            )
            return
        project_path, iid, resource_type = resolved
        did_unassign = False
        for result in results:
            agent_name = (result.get("agent") or "").lower()
            result_status = result.get("status")
            if agent_name == assigned_agent:
                if result_status == "ok":
                    did_unassign = True
                    LOGGER.info(
                        "Auto-unassigning agent after successful completion",
                        extra={"agent": assigned_agent, "type": resource_type, "iid": iid},
                    )
                    success = await unassign_agent(project_path, iid, resource_type, assigned_agent)
                    if success:
                        _record_self_unassign(project_path, iid, assigned_agent)
                    else:
                        LOGGER.warning(
                            "Auto-unassign glab call returned failure",
                            extra={"agent": assigned_agent, "type": resource_type, "iid": iid},
                        )
                else:
                    LOGGER.info(
                        "Auto-unassign skipped: agent task did not succeed",
                        extra={
                            "agent": assigned_agent,
                            "status": result_status,
                            "returncode": result.get("returncode"),
                        },
                    )
                break
        if not did_unassign:
            LOGGER.debug(
                "Auto-unassign skipped: no matching agent result found",
                extra={"assigned_agent": assigned_agent, "results": [r.get("agent") for r in results]},
            )
    except Exception as exc:
        LOGGER.error("Failed to auto-unassign agent", exc_info=exc)


async def _run_dispatch(
    event_identifier: str,
    tasks: List[Any],
    ctx: Dict[str, Any],
    assigned_agent: Optional[str],
) -> List[Dict[str, Any]]:
    """Dispatch the agents and run the post-run notification/unassign cleanup.

    On manual kill, cleanup runs and the AgentKilledError is re-raised. Otherwise
    the timeout, backup, and success-completion cleanups run in order.
    """
    try:
        results = await dispatch_agents(event_identifier, tasks, ctx)
    except AgentKilledError:
        await _cleanup_on_kill(ctx, assigned_agent)
        raise

    unassigned_on_timeout = await _notify_and_unassign_timeouts(results, ctx, assigned_agent)

    if settings.enable_backup_notifications:
        await _notify_backups(results, ctx)

    # Skip when the timeout path already unassigned the same agent.
    if settings.enable_auto_unassign and assigned_agent and not unassigned_on_timeout:
        await _auto_unassign_on_success(results, ctx, assigned_agent)

    return results


def _create_work_item(
    event_id: str,
    base_event_uuid: str,
    event_name: str,
    action: Optional[str],
    author: Optional[str],
    labels: List[str],
    mentions: List[str],
    match: RouteMatch,
    context: Dict[str, Any],
    payload: Dict[str, Any],
) -> TriggerWorkItem:
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    # Detect if an agent was assigned (via quick action or manual UI)
    assigned_agent = _extract_newly_assigned_agent(payload)
    if assigned_agent:
        LOGGER.debug("Detected assigned agent for auto-unassign tracking", extra={"agent": assigned_agent})

    # Include access mode, route, and base event UUID in context for
    # project path resolution and run-log filename generation
    ctx_with_access = {
        **context,
        "access": match.rule.access,
        "route": match.rule.name,
        "base_event_uuid": base_event_uuid,
    }

    async def _runner() -> List[Dict[str, Any]]:
        return await _run_dispatch(event_id, match.agents, ctx_with_access, assigned_agent)

    # Determine mention-hold metadata for deduplication (issue #69)
    is_mention = event_name == "Note Hook" and bool(mentions)
    is_assignment = assigned_agent is not None
    item_project_path, item_iid, _ = _resolve_resource(ctx_with_access)
    if item_iid is not None:
        item_iid = int(item_iid)
    # hold_agents: for mentions, the mentioned agents; for assignments, the assigned agent
    if is_mention:
        agent_usernames = set(_parse_all_mentions_agents(settings.all_mentions_agents))
        hold_agent_list = [m for m in mentions if m.lower() in agent_usernames]
    elif is_assignment and assigned_agent:
        hold_agent_list = [assigned_agent]
    else:
        hold_agent_list = []

    return TriggerWorkItem(
        event_id=event_id,
        base_event_uuid=base_event_uuid,
        event_name=event_name,
        action=action,
        author=author,
        labels=list(labels),
        mentions=list(mentions),
        route_name=match.rule.name,
        handler=_runner,
        future=future,
        project_path=item_project_path,
        iid=item_iid,
        is_mention_trigger=is_mention,
        is_assignment_trigger=is_assignment,
        hold_agents=hold_agent_list,
    )


def _log_route_match(
    match: RouteMatch,
    event_id: str,
    event_name: str,
    action: Optional[str],
    author: Optional[str],
    labels: List[str],
    mentions: List[str],
    assignees: Optional[List[str]] = None,
) -> None:
    LOGGER.info(
        "Matched route: event_id=%s route=%s event=%s action=%s author=%s labels=%s mentions=%s assignees=%s agents=%s",
        event_id,
        match.rule.name,
        event_name,
        action,
        author,
        labels,
        mentions,
        assignees or [],
        [task.agent for task in match.agents],
    )
