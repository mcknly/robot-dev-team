"""Robot Dev Team Project
File: app/services/context_builder.py
Description: Builds prompt context from webhook payloads and optional enrichment.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.services import glab
from app.services.branch_resolver import get_branch_context

LOGGER = get_logger(__name__)


async def build_context(event: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble context details for downstream agents."""

    project_path = _project_path(event)
    base_context = {
        "project": project_path,
        "title": _title(event),
        "description": _description(event),
        "author": _author(event),
        "web_url": _web_url(event),
        "clone_url": _clone_url(event),
        "payload": event,
    }

    extra_context = await _maybe_fetch_enrichment(event, project_path)
    if extra_context:
        base_context["extra_context"] = extra_context
    return base_context


def render_prompt(template_name: str, context: Dict[str, Any]) -> str:
    """Render a prompt template stored on disk using string.Template."""

    substitutions = _build_substitutions(context)

    template_path = Path(settings.prompt_dir) / template_name
    if not template_path.exists():
        raise FileNotFoundError(f"Prompt template not found: {template_path}")

    task_prompt = Template(template_path.read_text(encoding="utf-8"))
    task_rendered = task_prompt.safe_substitute(substitutions)

    system_prompt = _load_system_prompt(substitutions)
    if system_prompt:
        return f"{system_prompt}\n\n{task_rendered}".strip()
    return task_rendered


def _serialize_payload(payload: Any) -> str:
    import json

    try:
        return json.dumps(payload, indent=2, sort_keys=True)
    except TypeError:
        return str(payload)


async def _maybe_fetch_enrichment(event: Dict[str, Any], project_path: Optional[str]) -> Optional[str]:
    if not project_path:
        return None

    if event.get("object_kind") == "merge_request":
        data = event.get("object_attributes", {})
        iid = data.get("iid")
        if iid is not None:
            response = await glab.fetch_merge_request(project_path, int(iid))
            return _format_enrichment(response)
    if event.get("object_kind") == "issue":
        data = event.get("object_attributes", {})
        iid = data.get("iid")
        if iid is not None:
            response = await glab.fetch_issue(project_path, int(iid))
            return _format_enrichment(response)

    if event.get("object_kind") == "note":
        noteable_type = event.get("object_attributes", {}).get("noteable_type", "")
        if noteable_type == "MergeRequest":
            iid = event.get("merge_request", {}).get("iid")
            if iid is not None:
                response = await glab.fetch_merge_request(
                    project_path, int(iid)
                )
                return _format_enrichment(response)
        elif noteable_type == "Issue":
            iid = event.get("issue", {}).get("iid")
            if iid is not None:
                response = await glab.fetch_issue(project_path, int(iid))
                return _format_enrichment(response)
        else:
            LOGGER.debug(
                "No enrichment handler for noteable_type=%s", noteable_type
            )

    return None


def _format_enrichment(data: Optional[Dict[str, Any]]) -> Optional[str]:
    if not data:
        return None
    import json

    formatted: str = json.dumps(data, indent=2, sort_keys=True)
    return formatted


def _project_path(event: Dict[str, Any]) -> Optional[str]:
    project = event.get("project") or {}
    return project.get("path_with_namespace")


def _title(event: Dict[str, Any]) -> Optional[str]:
    attrs = event.get("object_attributes") or {}
    return attrs.get("title")


def _description(event: Dict[str, Any]) -> Optional[str]:
    attrs = event.get("object_attributes") or {}
    if event.get("object_kind") == "note":
        note = attrs.get("note") or attrs.get("description")
        if isinstance(note, str):
            return note
        if note is not None:
            return str(note)

    description = attrs.get("description")
    if isinstance(description, str) or description is None:
        return description
    return str(description)


def _author(event: Dict[str, Any]) -> Optional[str]:
    user = event.get("user") or event.get("user_username")
    if isinstance(user, dict):
        return user.get("username") or user.get("name")
    if isinstance(user, str):
        return user
    return None


def _web_url(event: Dict[str, Any]) -> Optional[str]:
    attrs = event.get("object_attributes") or {}
    return attrs.get("url") or attrs.get("web_url")


def _clone_url(event: Dict[str, Any]) -> Optional[str]:
    """Extract git clone URL from webhook payload (HTTPS preferred)."""
    project = event.get("project") or {}
    return project.get("git_http_url") or project.get("http_url")


def _load_system_prompt(substitutions: Dict[str, Any]) -> str:
    system_path = Path(settings.prompt_dir) / "system_prompt.txt"
    if not system_path.exists():
        return ""
    content = system_path.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    return Template(content).safe_substitute(substitutions)


def _build_substitutions(context: Dict[str, Any]) -> Dict[str, Any]:
    payload_json = context.get("payload", {})
    branch_ctx = get_branch_context(payload_json)
    substitutions: Dict[str, Any] = {
        "PROJECT": context.get("project", ""),
        "TITLE": context.get("title", ""),
        "DESCRIPTION": context.get("description", ""),
        "AUTHOR": context.get("author", ""),
        "URL": context.get("web_url", ""),
        "EXTRA": context.get("extra_context", ""),
        "JSON": _serialize_payload(payload_json),
        "SOURCE_BRANCH": branch_ctx.get("SOURCE_BRANCH", ""),
        "TARGET_BRANCH": branch_ctx.get("TARGET_BRANCH", ""),
        "CURRENT_BRANCH": context.get("current_branch", ""),
    }
    return substitutions
