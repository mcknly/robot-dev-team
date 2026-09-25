"""Robot Dev Team Project
File: app/preflight.py
Description: Startup validation of agent config; emits the harness install set.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from app.core.config import settings
from app.services.glab import agent_token_env_var
from app.services.routes import RouteRegistry

# Installers declare every binary they put on PATH, because the script name is
# not necessarily a binary name (install-gemini.sh installs `agy`) and a
# harness may require companion executables. Names are space-separated.
PROVIDES_PATTERN = re.compile(r"^#\s*provides:\s*(.+?)\s*$")

# Only the header region of an installer is scanned for the declaration.
PROVIDES_SCAN_LINES = 15

# glab-usr refuses to authenticate an agent whose git email is the built-in
# placeholder, so a route naming such an agent can never dispatch.
PLACEHOLDER_EMAIL_SUFFIX = "@example.com"


def agent_git_email_env_var(agent: str) -> str:
    """Derive the git email env var for an agent (mirrors ``glab-usr``)."""

    return agent.upper().replace("-", "_") + "_AGENT_GIT_EMAIL"


def agent_token_path(agent: str, home: Optional[Path] = None) -> Path:
    """Return the token-file fallback path for an agent (mirrors ``glab-usr``)."""

    base = home if home is not None else Path.home()
    directory = agent.lower().replace("_", "-")
    return base / f".{directory}" / "glab-token"


@dataclass(frozen=True)
class Installer:
    """An install script and the binaries it declares it provides."""

    path: Path
    provides: Tuple[str, ...]


@dataclass
class Report:
    """Outcome of a preflight run."""

    installers: List[Installer] = field(default_factory=list)
    # Binaries every routed, credentialed agent needs on PATH, plus every output
    # declared by the selected installers. The entrypoint re-checks these
    # *after* the install loop: a download failure stays a soft warning, but an
    # incomplete harness is fatal because every dispatch would fail.
    required_binaries: Set[str] = field(default_factory=set)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_provides(path: Path) -> Tuple[str, ...]:
    """Read the ``# provides:`` declaration from an install script."""

    try:
        with path.open("r", encoding="utf-8") as handle:
            for _ in range(PROVIDES_SCAN_LINES):
                line = handle.readline()
                if not line:
                    break
                match = PROVIDES_PATTERN.match(line)
                if match:
                    return tuple(match.group(1).split())
    except OSError:
        return ()
    return ()


def discover_installers(scripts_dir: Path) -> List[Installer]:
    """Find every ``install-*.sh`` under ``scripts_dir`` with its declaration."""

    return [
        Installer(path=path, provides=parse_provides(path))
        for path in sorted(scripts_dir.glob("install-*.sh"))
        if path.is_file()
    ]


def credential_problems(agent: str, home: Optional[Path] = None) -> List[str]:
    """Return the reasons ``agent`` cannot authenticate, empty when it can.

    Mirrors ``glab-usr`` exactly: a GitLab token resolved from either the
    convention env var or the token file, plus a real (non-placeholder) git
    email. Both are hard requirements at dispatch time, so both are checked
    here rather than failing later, after a webhook has already fired.
    """

    problems: List[str] = []

    token_var = agent_token_env_var(agent)
    token = (os.environ.get(token_var) or "").strip()
    if not token:
        token_file = agent_token_path(agent, home)
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if not token:
            problems.append(f"no GitLab token (set {token_var}, or write {token_file})")

    email_var = agent_git_email_env_var(agent)
    email = (os.environ.get(email_var) or "").strip()
    if not email:
        problems.append(f"no git identity (set {email_var} to a real address)")
    elif email.endswith(PLACEHOLDER_EMAIL_SUFFIX):
        problems.append(f"{email_var} is the placeholder '{email}'; set a real address")

    return problems


def credentialed_agents_from_env() -> Set[str]:
    """Return every agent that has a token env var set to a non-empty value."""

    agents: Set[str] = set()
    for name, value in os.environ.items():
        if not name.endswith("_AGENT_GITLAB_TOKEN") or not value.strip():
            continue
        prefix = name[: -len("_AGENT_GITLAB_TOKEN")]
        agents.add(prefix.lower().replace("_", "-"))
    return agents


def _route_binary(options: Dict[str, Any], agent: str) -> Optional[str]:
    """Resolve the harness binary a route entry will exec (mirrors dispatch).

    Dispatch reads ``options.get("command", agent)`` (``app/services/agents.py``),
    so an *absent* command falls back to the agent name. A command that is
    present but empty or non-string is not a fallback -- dispatch would exec
    nothing -- so it is reported as a config error via ``None``.
    """

    if "command" not in options:
        return agent
    command = options["command"]
    if not isinstance(command, str) or not command.strip():
        return None
    return Path(command.strip()).name


@dataclass
class _RouteAudit:
    """Per-agent view of the active route table."""

    # Binaries that at least one routed *and* credentialed agent needs.
    required_binaries: Set[str] = field(default_factory=set)
    routed_agents: Set[str] = field(default_factory=set)
    problems: Dict[str, List[str]] = field(default_factory=dict)
    # An agent is typically named by several routes; collect them so the
    # operator gets one error per agent listing every route to fix, rather
    # than the same error repeated once per route.
    dispatching_routes: Dict[str, List[str]] = field(default_factory=dict)
    task_count: int = 0
    bad_commands: List[str] = field(default_factory=list)

    def errors(self) -> List[str]:
        messages: List[str] = list(self.bad_commands)
        for agent in sorted(a for a, found in self.problems.items() if found):
            routes = ", ".join(f"'{name}'" for name in self.dispatching_routes[agent])
            messages.extend(
                f"agent '{agent}' is dispatched by route(s) {routes} but has {problem}. "
                f"Fix the credential, or remove the agent from those routes."
                for problem in self.problems[agent]
            )
        return messages


def _audit_routes(registry: RouteRegistry, home: Optional[Path]) -> _RouteAudit:
    """Check every dispatchable agent and collect the binaries they need."""

    audit = _RouteAudit()
    for rule, task in registry.iter_agent_tasks():
        agent = task.agent
        audit.task_count += 1
        audit.routed_agents.add(agent)

        if agent not in audit.problems:
            audit.problems[agent] = credential_problems(agent, home)
        routes = audit.dispatching_routes.setdefault(agent, [])
        if rule.name not in routes:
            routes.append(rule.name)

        binary = _route_binary(task.options, agent)
        if binary is None:
            audit.bad_commands.append(
                f"route '{rule.name}' gives agent '{agent}' an empty 'command'; "
                f"dispatch would have nothing to exec. Set a real command, or "
                f"remove the key to default to the agent name."
            )
        elif not audit.problems[agent]:
            audit.required_binaries.add(binary)
    return audit


def build_report(
    scripts_dir: Path,
    registry: RouteRegistry,
    home: Optional[Path] = None,
) -> Report:
    """Validate the active config and derive the set of installers to run."""

    report = Report()
    audit = _audit_routes(registry, home)
    report.errors.extend(audit.errors())
    report.errors.extend(_branch_pruning_errors(home))
    report.required_binaries = set(audit.required_binaries)

    # A config with nothing to dispatch makes every check below vacuous and
    # boots a container that can never do any work. There are several ways in
    # -- an empty or fully commented-out file, or an agent entry silently
    # dropped for lacking its 'agent:' key -- so guard the outcome, not each
    # cause. (A route path that does not exist is caught earlier, in main.)
    if audit.task_count == 0:
        report.errors.append(
            "the active route config defines no dispatchable agent tasks. "
            "Check that its 'routes:' list is populated and that every agent "
            "entry has an 'agent:' key -- entries without one are dropped."
        )

    provided: Set[str] = set()
    for installer in discover_installers(scripts_dir):
        if not installer.provides:
            report.warnings.append(
                f"{installer.path.name} declares no '# provides: <binary>' line; "
                f"it will never be installed. Add one (see docs/ADDING_AN_AGENT.md)."
            )
            continue
        provided.update(installer.provides)
        if audit.required_binaries.intersection(installer.provides):
            report.installers.append(installer)
            report.required_binaries.update(installer.provides)

    # A route may legitimately exec a binary no installer owns (BYOA, or a
    # binary baked into a custom image). Surface it, but never fail on it.
    report.warnings.extend(
        f"route command '{binary}' is not provided by any install script; "
        f"assuming it is already present on PATH."
        for binary in sorted(audit.required_binaries - provided)
    )
    report.warnings.extend(_mention_warnings(registry))
    report.warnings.extend(
        f"agent '{agent}' has a GitLab token but no route dispatches it; "
        f"no harness will be installed for it."
        for agent in sorted(credentialed_agents_from_env() - audit.routed_agents)
    )

    return report


def _branch_pruning_errors(home: Optional[Path]) -> List[str]:
    """Branch pruning needs a glab identity, but no route and no harness."""

    if not settings.branch_pruning_enabled:
        return []
    agent = settings.branch_pruning_agent
    return [
        f"BRANCH_PRUNING_AGENT is '{agent}' but it has {problem}. "
        f"Fix the credential, or set BRANCH_PRUNING_ENABLED=false."
        for problem in credential_problems(agent, home)
    ]


def _mention_warnings(registry: RouteRegistry) -> List[str]:
    """Flag @all members that no mention route can actually reach."""

    mentionable = {
        mention.lower() for rule in registry.rules for mention in rule.mentions
    }
    warnings: List[str] = []
    for entry in settings.all_mentions_agents.split(","):
        name = entry.strip().lower()
        if name and name not in mentionable:
            warnings.append(
                f"ALL_MENTIONS_AGENTS lists '{name}' but no route matches that mention; "
                f"@all will silently drop it."
            )
    return warnings


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Validate the active config and emit what the entrypoint must act on.

    stdout is machine-readable, one ``key=value`` per line:

    - ``install=<path>``  -- an install script to run
    - ``binary=<name>``   -- an executable that must be on PATH after installs

    Diagnostics go to stderr; a non-zero exit means the config is unusable.
    """

    parser = argparse.ArgumentParser(
        prog="python -m app.preflight",
        description="Validate agent configuration and emit the harness install set.",
    )
    parser.add_argument(
        "--scripts-dir",
        default="scripts",
        help="Directory containing install-*.sh scripts (default: %(default)s)",
    )
    parser.add_argument(
        "--routes",
        default=settings.route_config_path,
        help="Route config to validate (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    # RouteRegistry treats a missing file as an empty route list, which would
    # make a typo'd ROUTE_CONFIG_PATH look like a valid do-nothing config.
    # The path is the source of truth for every check below, so its absence is
    # fatal on its own terms, with the path in the message.
    if not Path(args.routes).exists():
        print(
            f"[preflight] ERROR: route config not found: {args.routes}. "
            f"Set ROUTE_CONFIG_PATH to an existing file.",
            file=sys.stderr,
        )
        return 1

    try:
        registry = RouteRegistry(args.routes)
    except (ValueError, OSError) as exc:
        print(f"[preflight] ERROR: cannot load {args.routes}: {exc}", file=sys.stderr)
        return 1

    report = build_report(Path(args.scripts_dir), registry)

    for warning in report.warnings:
        print(f"[preflight] WARN: {warning}", file=sys.stderr)
    for error in report.errors:
        print(f"[preflight] ERROR: {error}", file=sys.stderr)

    if not report.ok:
        print(
            f"[preflight] {len(report.errors)} fatal configuration error(s) in "
            f"{args.routes}. Every routed agent must have a GitLab token and a git "
            f"identity; see docs/ENVIRONMENT.md.",
            file=sys.stderr,
        )
        return 1

    for installer in report.installers:
        print(f"install={installer.path}")
    for binary in sorted(report.required_binaries):
        print(f"binary={binary}")

    names = ", ".join(sorted(b for i in report.installers for b in i.provides)) or "none"
    print(f"[preflight] OK: harnesses to install: {names}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
