"""Robot Dev Team Project
File: app/services/routes.py
Description: Routing logic for webhook events to agent tasks.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Callable, Dict, Iterator, List, Optional, Pattern, Tuple

import yaml

from app.core.logging import get_logger

LOGGER = get_logger(__name__)

MODEL_ARG_FLAG = "--model"
PROMPT_ARG_PLACEHOLDER = "${PROMPT}"


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
    actions: List[str] = field(default_factory=list)
    authors: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    mentions: List[str] = field(default_factory=list)
    assignees: List[str] = field(default_factory=list)
    agents: List[AgentTask] = field(default_factory=list)
    access: str = "readonly"
    pattern: Optional[Pattern[str]] = None
    max_wall_clock_seconds: Optional[int] = None
    max_inactivity_seconds: Optional[int] = None
    randomize: bool = False

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
        if self.actions and action not in self.actions:
            return False
        if self.authors and (author or "").lower() not in self.authors:
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


def _parse_authors(value: Any, route_name: str) -> List[str]:
    """Normalize the ``author`` YAML value to a lowercased list of usernames.

    Accepts ``None``/missing (-> ``[]``), a single non-empty string
    (-> ``[value]``), or a list of non-empty strings. An empty list is treated
    the same as a missing field. Entries are lowercased at load time so
    matching is case-insensitive without per-call cost. Empty-string values
    (scalar ``""`` or list entries) are rejected because they almost always
    indicate a config typo rather than an intentional wildcard.
    """

    if value is None:
        return []
    if isinstance(value, str):
        if value == "":
            raise ValueError(
                f"Invalid author '' in route '{route_name}': "
                "use a non-empty username or omit the field to allow any author"
            )
        return [value.lower()]
    if isinstance(value, list):
        normalized: List[str] = []
        for entry in value:
            if not isinstance(entry, str):
                raise ValueError(
                    f"Invalid author entry '{entry!r}' in route '{route_name}': "
                    "all author list items must be strings"
                )
            if entry == "":
                raise ValueError(
                    f"Invalid empty author entry in route '{route_name}': "
                    "remove the empty string or omit the field to allow any author"
                )
            normalized.append(entry.lower())
        return normalized
    raise ValueError(
        f"Invalid author value '{value!r}' in route '{route_name}': "
        "must be a string or list of strings"
    )


def _parse_actions(value: Any, route_name: str) -> List[str]:
    """Normalize the ``action`` YAML value to a list of action strings.

    Accepts ``None``/missing (-> ``[]``), a single non-empty string
    (-> ``[value]``), or a list of non-empty strings. An empty list is treated
    the same as a missing field (no constraint / wildcard), mirroring the
    empty-list convention in ``_parse_authors``. Empty-string values (scalar
    ``""`` or list entries) are rejected because they almost always indicate a
    config typo rather than an intentional wildcard.

    Unlike ``_parse_authors``, action values are NOT lowercased: GitLab action
    strings (``open``, ``update``, ``merge``, ...) are already canonical
    lowercase and the matcher compares them exactly, so preserving the raw
    value avoids a silent change to matching semantics.
    """

    if value is None:
        return []
    if isinstance(value, str):
        if value == "":
            raise ValueError(
                f"Invalid action '' in route '{route_name}': "
                "use a non-empty action or omit the field to allow any action"
            )
        return [value]
    if isinstance(value, list):
        normalized: List[str] = []
        for entry in value:
            if not isinstance(entry, str):
                raise ValueError(
                    f"Invalid action entry '{entry!r}' in route '{route_name}': "
                    "all action list items must be strings"
                )
            if entry == "":
                raise ValueError(
                    f"Invalid empty action entry in route '{route_name}': "
                    "remove the empty string or omit the field to allow any action"
                )
            normalized.append(entry)
        return normalized
    raise ValueError(
        f"Invalid action value '{value!r}' in route '{route_name}': "
        "must be a string or list of strings"
    )


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

    @property
    def rules(self) -> List[RouteRule]:
        """Return the parsed rules (a copy, so callers cannot mutate state)."""

        return list(self._rules)

    def iter_agent_tasks(self) -> Iterator[Tuple[RouteRule, AgentTask]]:
        """Yield every (rule, agent task) pair in the loaded configuration.

        Used by the startup preflight to validate that each dispatchable
        agent is credentialed and to derive the set of harness binaries the
        active configuration actually needs.
        """

        for rule in self._rules:
            for task in rule.agents:
                yield rule, task

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
                # Shuffle a *copy* so the registry's stored agent order is
                # never mutated; otherwise repeated calls would drift and the
                # log line would not reflect the original YAML order on reload.
                agents = list(rule.agents)
                if rule.randomize and len(agents) > 1:
                    random.shuffle(agents)
                return RouteMatch(rule=rule, agents=agents)
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
        self._validate_prompt_arg_placeholders(parsed_agents, name)
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
        randomize = _parse_bool_flag(item.get("randomize", False), "randomize", name)
        if randomize and len(parsed_agents) <= 1:
            LOGGER.warning(
                "Route '%s' sets randomize=true but has %d agent(s); "
                "the flag has no effect on routes with fewer than 2 agents",
                name,
                len(parsed_agents),
            )
        return RouteRule(
            name=name,
            event=match.get("event", ""),
            actions=_parse_actions(match.get("action"), name),
            authors=_parse_authors(match.get("author"), name),
            labels=labels,
            mentions=mentions,
            assignees=assignees,
            agents=parsed_agents,
            access=access,
            pattern=compiled_pattern,
            max_wall_clock_seconds=route_wall_clock,
            max_inactivity_seconds=route_inactivity,
            randomize=randomize,
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

    def _validate_prompt_arg_placeholders(self, agents: List[AgentTask], route_name: str) -> None:
        """Ensure ${PROMPT} is used only as a complete argv element."""

        for agent in agents:
            options = agent.options or {}
            args = options.get("args")
            if not isinstance(args, list):
                continue
            for arg in args:
                if (
                    isinstance(arg, str)
                    and PROMPT_ARG_PLACEHOLDER in arg
                    and arg != PROMPT_ARG_PLACEHOLDER
                ):
                    raise ValueError(
                        f"Invalid {PROMPT_ARG_PLACEHOLDER} usage in route "
                        f"'{route_name}' for agent '{agent.agent}': the "
                        "placeholder must be its own args element"
                    )

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
            if raw_value == "":
                raise ValueError("routes.yaml defines an empty --model value")
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
        if resolved == "":
            raise ValueError("routes.yaml resolved --model placeholder to an empty value")

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


def _parse_bool_flag(value: Any, field_name: str, route_name: str) -> bool:
    """Validate a strictly-boolean route field. YAML truthy strings are rejected
    on purpose so misconfigurations like ``randomize: "yes"`` fail loudly at
    load time rather than silently behaving as ``False``.
    """
    if not isinstance(value, bool):
        raise ValueError(
            f"Invalid {field_name} '{value!r}' in route '{route_name}': "
            "must be a boolean (true or false)"
        )
    return value


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


__all__ = ["AgentTask", "RouteMatch", "RouteRegistry", "RouteRule"]
