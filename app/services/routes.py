"""Robot Dev Team Project
File: app/services/routes.py
Description: Routing logic for webhook events to agent tasks.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Callable, Dict, List, Optional, Pattern

import yaml

from app.core.logging import get_logger

LOGGER = get_logger(__name__)

MODEL_ARG_FLAG = "--model"


@dataclass
class AgentTask:
    """Represents a single agent invocation configuration."""

    agent: str
    task: str
    prompt: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)
    max_wall_clock_seconds: Optional[int] = None
    max_inactivity_seconds: Optional[int] = None


@dataclass
class RouteRule:
    """A single routing rule loaded from YAML."""

    name: str
    event: str
    action: Optional[str] = None
    author: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    mentions: List[str] = field(default_factory=list)
    assignees: List[str] = field(default_factory=list)
    agents: List[AgentTask] = field(default_factory=list)
    access: str = "readonly"
    pattern: Optional[Pattern[str]] = None
    max_wall_clock_seconds: Optional[int] = None
    max_inactivity_seconds: Optional[int] = None

    def matches(
        self,
        event_name: str,
        action: Optional[str],
        author: Optional[str],
        labels: List[str],
        mentions: List[str],
        body: Optional[str] = None,
        assignees: Optional[List[str]] = None,
    ) -> bool:
        if self.event != event_name:
            return False
        if self.action and self.action != action:
            return False
        if self.author and self.author != author:
            return False
        if self.labels and not set(self.labels).issubset(set(labels)):
            return False
        if self.mentions and not _mentions_subset(self.mentions, mentions):
            return False
        if self.assignees and not _mentions_subset(self.assignees, assignees or []):
            return False
        if self.pattern:
            if body is None or not self.pattern.search(body):
                return False
            LOGGER.debug(
                "Pattern matched",
                extra={"route": self.name, "pattern": self.pattern.pattern},
            )
        return True


def _mentions_subset(required: List[str], provided: List[str]) -> bool:
    required_set = {mention.lower() for mention in required}
    provided_set = {mention.lower() for mention in provided}
    return required_set.issubset(provided_set)


@dataclass
class RouteMatch:
    """Represents a successful route match and associated agents."""

    rule: RouteRule
    agents: List[AgentTask]


class RouteRegistry:
    """Loads and resolves routing rules from a YAML configuration file."""

    def __init__(
        self,
        path: str,
        reload_on_change: bool = False,
        model_variables: Optional[Dict[str, str]] = None,
    ) -> None:
        self._path = Path(path)
        self._reload = reload_on_change
        self._model_variables = dict(model_variables) if model_variables else {}
        self._rules: List[RouteRule] = []
        self._last_mtime: Optional[float] = None
        self._load()

    def refresh(self) -> None:
        """Reload rules if the file has changed and hot reload is enabled."""

        if not self._reload:
            return
        try:
            current_mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return
        if self._last_mtime is None or current_mtime > self._last_mtime:
            self._load()

    def resolve(
        self,
        event_name: str,
        action: Optional[str],
        author: Optional[str],
        labels: List[str],
        mentions: List[str],
        body: Optional[str] = None,
        assignees: Optional[List[str]] = None,
    ) -> List[AgentTask]:
        """Return agent tasks for the first matching rule."""

        match = self.resolve_match(event_name, action, author, labels, mentions, body=body, assignees=assignees)
        return match.agents if match else []

    def resolve_match(
        self,
        event_name: str,
        action: Optional[str],
        author: Optional[str],
        labels: List[str],
        mentions: List[str],
        body: Optional[str] = None,
        assignees: Optional[List[str]] = None,
        rule_predicate: Optional[Callable[[RouteRule], bool]] = None,
    ) -> Optional[RouteMatch]:
        """Return the first matching route and its agent tasks."""

        self.refresh()
        for rule in self._rules:
            if rule_predicate and not rule_predicate(rule):
                continue
            if rule.matches(event_name, action, author, labels, mentions, body=body, assignees=assignees):
                return RouteMatch(rule=rule, agents=rule.agents)
        return None

    def _load(self) -> None:
        data = self._read_yaml()
        self._rules = [self._parse_rule(item) for item in data]
        try:
            self._last_mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            self._last_mtime = None

    def _read_yaml(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            return []
        with self._path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        routes = raw.get("routes", [])
        if not isinstance(routes, list):
            raise ValueError("routes.yaml must define a list under 'routes'")
        return routes

    def _parse_rule(self, item: Dict[str, Any]) -> RouteRule:
        name = item.get("name") or "unnamed"
        match = item.get("match", {})
        agents = item.get("agents", [])

        # Parse route-level timeout overrides
        route_wall_clock = _parse_optional_positive_int(
            item.get("max_wall_clock_seconds"), "max_wall_clock_seconds", name,
        )
        route_inactivity = _parse_optional_positive_int(
            item.get("max_inactivity_seconds"), "max_inactivity_seconds", name,
        )

        parsed_agents = [
            AgentTask(
                agent=agent.get("agent"),
                task=agent.get("task", "default"),
                prompt=agent.get("prompt"),
                options=agent.get("options", {}),
                max_wall_clock_seconds=route_wall_clock,
                max_inactivity_seconds=route_inactivity,
            )
            for agent in agents
            if agent.get("agent")
        ]
        self._expand_model_placeholders(parsed_agents)
        labels = match.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        mentions = match.get("mentions") or []
        if isinstance(mentions, str):
            mentions = [mentions]
        assignees = match.get("assignees") or []
        if isinstance(assignees, str):
            assignees = [assignees]
        access = item.get("access", "readonly")
        if access not in ("readonly", "readwrite"):
            raise ValueError(
                f"Invalid access mode '{access}' in route '{name}'. "
                "Must be 'readonly' or 'readwrite'."
            )
        pattern_str = match.get("pattern")
        compiled_pattern: Optional[Pattern[str]] = None
        if pattern_str:
            try:
                compiled_pattern = re.compile(pattern_str)
            except re.error as exc:
                raise ValueError(
                    f"Invalid regex pattern '{pattern_str}' in route '{name}': {exc}"
                ) from exc
        return RouteRule(
            name=name,
            event=match.get("event", ""),
            action=match.get("action"),
            author=match.get("author"),
            labels=labels,
            mentions=mentions,
            assignees=assignees,
            agents=parsed_agents,
            access=access,
            pattern=compiled_pattern,
            max_wall_clock_seconds=route_wall_clock,
            max_inactivity_seconds=route_inactivity,
        )

    def _expand_model_placeholders(self, agents: List[AgentTask]) -> None:
        """Replace ${VAR} placeholders for --model arguments."""

        for agent in agents:
            options = agent.options or {}
            args = options.get("args")
            if not isinstance(args, list):
                continue
            for index, arg in enumerate(args):
                if arg != MODEL_ARG_FLAG or index + 1 >= len(args):
                    continue
                model_value = args[index + 1]
                if isinstance(model_value, str):
                    args[index + 1] = self._substitute_model_value(model_value)

    _MODEL_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*_MODEL$")

    def _substitute_model_value(self, raw_value: str) -> str:
        """Resolve ${VAR} syntax for model values.

        Resolution order:
        1. Explicit ``model_variables`` passed to the constructor.
        2. Environment variables (convention-based: any ``*_MODEL`` env var).

        Logs a warning when a referenced variable does not match the
        ``*_MODEL`` naming convention, since the full ``os.environ`` is
        exposed and a misconfigured route could inadvertently leak
        sensitive environment variables into agent CLI arguments.
        """

        if "${" not in raw_value:
            return raw_value
        # Build a combined mapping: explicit overrides first, then env vars
        combined = dict(os.environ)
        combined.update(self._model_variables)
        template = Template(raw_value)
        try:
            resolved = template.substitute(combined)
        except KeyError as exc:
            missing = exc.args[0]
            raise ValueError(
                f"routes.yaml references undefined model placeholder '${{{missing}}}'"
            ) from exc

        # Warn about non-MODEL variable references that could leak secrets
        for match in re.finditer(r"\$\{([^}]+)\}", raw_value):
            var_name = match.group(1)
            if var_name not in self._model_variables and not self._MODEL_PATTERN.match(var_name):
                LOGGER.warning(
                    "Route model placeholder references non-MODEL env var '%s'; "
                    "consider using a *_MODEL variable to avoid leaking secrets",
                    var_name,
                )

        return resolved


def _parse_optional_positive_int(value: Any, field_name: str, route_name: str) -> Optional[int]:
    """Validate an optional positive integer field from route YAML."""
    if value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid {field_name} '{value}' in route '{route_name}': must be a positive integer"
        ) from exc
    if result <= 0:
        raise ValueError(
            f"Invalid {field_name} '{value}' in route '{route_name}': must be a positive integer"
        )
    return result


__all__ = ["AgentTask", "RouteMatch", "RouteRegistry"]
