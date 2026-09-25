"""Robot Dev Team Project
File: tests/test_preflight.py
Description: Pytest coverage for the startup agent-config preflight.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

import os
from pathlib import Path

import pytest
import yaml

from app import preflight
from app.core.config import settings
from app.services.routes import RouteRegistry

REPO_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Isolate tests from the developer's real agent credentials.

    The repo ships a .env and the maintainer's shell exports real agent
    tokens; without scrubbing them, these tests would pass or fail based on
    the machine they run on.
    """

    for name in list(os.environ):
        if name.endswith(("_AGENT_GITLAB_TOKEN", "_AGENT_GIT_EMAIL")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "branch_pruning_enabled", False)
    monkeypatch.setattr(settings, "all_mentions_agents", "")
    home = tmp_path / "home"
    home.mkdir()
    return home


def credential(monkeypatch, agent, token="pat-test", email=None):
    """Fully credential an agent the way .env would."""

    upper = agent.upper().replace("-", "_")
    monkeypatch.setenv(f"{upper}_AGENT_GITLAB_TOKEN", token)
    monkeypatch.setenv(f"{upper}_AGENT_GIT_EMAIL", email or f"{agent}@example.org")


def write_routes(tmp_path, agents, mentions=None):
    """Write a one-rule route file wiring the given (agent, command) pairs."""

    match = {"event": "Issue Hook", "action": "open"}
    if mentions:
        match["mentions"] = mentions
    rule = {
        "name": "test-route",
        "match": match,
        "agents": [
            {"agent": agent, "task": "triage", "options": {"command": command}}
            for agent, command in agents
        ],
    }
    path = tmp_path / "routes.yaml"
    path.write_text(yaml.safe_dump({"routes": [rule]}), encoding="utf-8")
    return path


def make_scripts(tmp_path, declarations):
    """Create a fake scripts dir; declarations maps script name -> provides line."""

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name, provides in declarations.items():
        body = "#!/usr/bin/env bash\n"
        if provides is not None:
            body += f"# provides: {provides}\n"
        body += "echo installing\n"
        (scripts / name).write_text(body, encoding="utf-8")
    return scripts


def report_for(tmp_path, home, scripts, routes):
    return preflight.build_report(scripts, RouteRegistry(str(routes)), home=home)


def installed_names(report):
    return sorted(installer.path.name for installer in report.installers)


class TestProvidesParsing:
    def test_reads_single_binary(self, tmp_path):
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        assert preflight.parse_provides(scripts / "install-claude.sh") == ("claude",)

    def test_reads_aliases(self, tmp_path):
        scripts = make_scripts(tmp_path, {"install-oc.sh": "opencode opencode-rdt"})
        assert preflight.parse_provides(scripts / "install-oc.sh") == (
            "opencode",
            "opencode-rdt",
        )

    def test_missing_declaration_is_empty(self, tmp_path):
        scripts = make_scripts(tmp_path, {"install-old.sh": None})
        assert preflight.parse_provides(scripts / "install-old.sh") == ()

    def test_shipped_installers_declare_their_binaries(self):
        """The gemini script installs `agy` -- a filename-derived scheme breaks here."""

        found = {
            installer.path.name: installer.provides
            for installer in preflight.discover_installers(REPO_SCRIPTS)
        }
        assert found["install-claude.sh"] == ("claude",)
        assert found["install-codex.sh"] == ("codex", "codex-code-mode-host")
        assert found["install-gemini.sh"] == ("agy",)
        assert found["install-opencode.sh"] == ("opencode",)
        assert found["install-goose.sh"] == ("goose",)
        assert found["install-grok.sh"] == ("grok",)
        assert found["install-pi.sh"] == ("pi",)

    def test_install_pi_does_not_leak_npm_or_npx_onto_shared_path(self):
        """install-pi.sh must symlink only `node` onto the shared ~/.local/bin.

        ~/.local/bin is on the application-wide PATH (docker-entrypoint.sh), so if
        install-pi.sh symlinked `npm`/`npx` there, enabling Pi would also hand an
        enabled Goose harness an `npx` -- letting it run host-configured npx-based
        stdio extensions and silencing goose_config's "not on PATH" warning. Pi's
        `node` shim is the only thing that legitimately needs the shared PATH; its
        npm lives in the versioned Node dir and is used via a command-scoped PATH.

        This pins that contract so a future edit that reintroduces the leak (e.g.
        `for bin in node npm npx; do ln -sf ... "${INSTALL_DIR}/${bin}"`) fails CI.
        """

        script = (REPO_SCRIPTS / "install-pi.sh").read_text(encoding="utf-8")

        for leaked in ('"${INSTALL_DIR}/npm"', '"${INSTALL_DIR}/npx"'):
            assert leaked not in script, (
                f"install-pi.sh symlinks {leaked} onto the shared PATH; only `node` "
                f"may land in ~/.local/bin (npm/npx leaking there enables npx for "
                f"an unrelated Goose harness)"
            )
        assert 'ln -sf "${NODE_DIR}/bin/node" "${INSTALL_DIR}/node"' in script, (
            "install-pi.sh must symlink `node` (and only node) onto ~/.local/bin"
        )


class TestInstallSelection:
    def test_installs_only_routed_and_credentialed_harnesses(
        self, tmp_path, monkeypatch, clean_env
    ):
        credential(monkeypatch, "claude")
        scripts = make_scripts(
            tmp_path,
            {"install-claude.sh": "claude", "install-codex.sh": "codex"},
        )
        routes = write_routes(tmp_path, [("claude", "claude")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert report.ok
        assert installed_names(report) == ["install-claude.sh"]

    def test_matches_binary_not_script_name(self, tmp_path, monkeypatch, clean_env):
        """The `gemini` agent runs `agy`; the install script is install-gemini.sh."""

        credential(monkeypatch, "gemini")
        scripts = make_scripts(tmp_path, {"install-gemini.sh": "agy"})
        routes = write_routes(tmp_path, [("gemini", "agy")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert report.ok
        assert installed_names(report) == ["install-gemini.sh"]

    def test_selected_installer_requires_every_declared_binary(
        self, tmp_path, monkeypatch, clean_env
    ):
        """Companion executables must pass the entrypoint's post-install gate."""

        credential(monkeypatch, "codex")
        scripts = make_scripts(
            tmp_path,
            {"install-codex.sh": "codex codex-code-mode-host"},
        )
        routes = write_routes(tmp_path, [("codex", "codex")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert report.ok
        assert installed_names(report) == ["install-codex.sh"]
        assert report.required_binaries == {"codex", "codex-code-mode-host"}

    def test_shared_binary_installed_once_for_many_logical_agents(
        self, tmp_path, monkeypatch, clean_env
    ):
        credential(monkeypatch, "opencode-kimi")
        credential(monkeypatch, "opencode-glm")
        scripts = make_scripts(tmp_path, {"install-opencode.sh": "opencode"})
        routes = write_routes(
            tmp_path,
            [("opencode-kimi", "opencode"), ("opencode-glm", "opencode")],
        )

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert report.ok
        assert installed_names(report) == ["install-opencode.sh"]

    def test_unrouted_harness_is_skipped(self, tmp_path, monkeypatch, clean_env):
        credential(monkeypatch, "claude")
        scripts = make_scripts(
            tmp_path,
            {"install-claude.sh": "claude", "install-opencode.sh": "opencode"},
        )
        routes = write_routes(tmp_path, [("claude", "claude")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert installed_names(report) == ["install-claude.sh"]

    def test_installer_without_provides_warns_and_is_skipped(
        self, tmp_path, monkeypatch, clean_env
    ):
        credential(monkeypatch, "claude")
        scripts = make_scripts(
            tmp_path, {"install-claude.sh": "claude", "install-legacy.sh": None}
        )
        routes = write_routes(tmp_path, [("claude", "claude")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert report.ok
        assert installed_names(report) == ["install-claude.sh"]
        assert any("install-legacy.sh" in w for w in report.warnings)


class TestFatalCredentialErrors:
    def test_routed_agent_without_token_is_fatal(self, tmp_path, monkeypatch, clean_env):
        monkeypatch.setenv("CLAUDE_AGENT_GIT_EMAIL", "claude@real.example.org")
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        routes = write_routes(tmp_path, [("claude", "claude")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert not report.ok
        assert any("CLAUDE_AGENT_GITLAB_TOKEN" in e for e in report.errors)
        assert report.installers == []

    def test_routed_agent_without_git_email_is_fatal(
        self, tmp_path, monkeypatch, clean_env
    ):
        monkeypatch.setenv("CLAUDE_AGENT_GITLAB_TOKEN", "pat-test")
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        routes = write_routes(tmp_path, [("claude", "claude")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert not report.ok
        assert any("CLAUDE_AGENT_GIT_EMAIL" in e for e in report.errors)

    def test_placeholder_email_is_fatal(self, tmp_path, monkeypatch, clean_env):
        """glab-usr rejects @example.com, so such a route can never dispatch."""

        credential(monkeypatch, "claude", email="claude@example.com")
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        routes = write_routes(tmp_path, [("claude", "claude")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert not report.ok
        assert any("placeholder" in e for e in report.errors)

    def test_one_error_per_agent_listing_every_route(
        self, tmp_path, monkeypatch, clean_env
    ):
        """An agent named by five routes must not produce five identical errors."""

        rules = [
            {
                "name": f"route-{index}",
                "match": {"event": "Issue Hook"},
                "agents": [
                    {
                        "agent": "codex",
                        "task": "triage",
                        "options": {"command": "codex"},
                    }
                ],
            }
            for index in range(3)
        ]
        routes = tmp_path / "routes.yaml"
        routes.write_text(yaml.safe_dump({"routes": rules}), encoding="utf-8")
        scripts = make_scripts(tmp_path, {"install-codex.sh": "codex"})

        report = report_for(tmp_path, clean_env, scripts, routes)

        # One missing token + one missing email, not one pair per route.
        assert len(report.errors) == 2
        for name in ("route-0", "route-1", "route-2"):
            assert all(name in error for error in report.errors)

    def test_uncredentialed_agent_does_not_pull_in_its_harness(
        self, tmp_path, monkeypatch, clean_env
    ):
        """A broken route must not silently install its binary anyway."""

        credential(monkeypatch, "claude")
        scripts = make_scripts(
            tmp_path,
            {"install-claude.sh": "claude", "install-codex.sh": "codex"},
        )
        routes = write_routes(
            tmp_path, [("claude", "claude"), ("codex", "codex")]
        )

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert not report.ok
        assert installed_names(report) == ["install-claude.sh"]

    def test_token_file_satisfies_the_check_without_env_var(
        self, tmp_path, monkeypatch, clean_env
    ):
        """Compose bind-mounts host ~/.claude, so the token may only exist as a file."""

        monkeypatch.setenv("CLAUDE_AGENT_GIT_EMAIL", "claude@real.example.org")
        token_file = clean_env / ".claude" / "glab-token"
        token_file.parent.mkdir()
        token_file.write_text("pat-from-file", encoding="utf-8")
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        routes = write_routes(tmp_path, [("claude", "claude")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert report.ok
        assert installed_names(report) == ["install-claude.sh"]


class TestBranchPruningAgent:
    """The pruning agent is a glab identity: it has no route and no binary."""

    def test_fatal_when_pruning_enabled_and_agent_uncredentialed(
        self, tmp_path, monkeypatch, clean_env
    ):
        monkeypatch.setattr(settings, "branch_pruning_enabled", True)
        monkeypatch.setattr(settings, "branch_pruning_agent", "pruner")
        credential(monkeypatch, "claude")
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        routes = write_routes(tmp_path, [("claude", "claude")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert not report.ok
        assert any("BRANCH_PRUNING_AGENT" in e for e in report.errors)

    def test_ignored_when_pruning_disabled(self, tmp_path, monkeypatch, clean_env):
        monkeypatch.setattr(settings, "branch_pruning_enabled", False)
        monkeypatch.setattr(settings, "branch_pruning_agent", "pruner")
        credential(monkeypatch, "claude")
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        routes = write_routes(tmp_path, [("claude", "claude")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert report.ok

    def test_credentialed_pruning_agent_needs_no_harness(
        self, tmp_path, monkeypatch, clean_env
    ):
        """A pruning identity is credentialed but pulls in no install script."""

        monkeypatch.setattr(settings, "branch_pruning_enabled", True)
        monkeypatch.setattr(settings, "branch_pruning_agent", "pruner")
        credential(monkeypatch, "claude")
        credential(monkeypatch, "pruner")
        scripts = make_scripts(
            tmp_path, {"install-claude.sh": "claude", "install-pruner.sh": "pruner"}
        )
        routes = write_routes(tmp_path, [("claude", "claude")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert report.ok
        assert installed_names(report) == ["install-claude.sh"]


class TestWarnings:
    def test_token_without_route_warns_but_is_not_fatal(
        self, tmp_path, monkeypatch, clean_env
    ):
        """Staged rollout: credentials may land before the route does."""

        credential(monkeypatch, "claude")
        credential(monkeypatch, "codex")
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        routes = write_routes(tmp_path, [("claude", "claude")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert report.ok
        assert any("'codex' has a GitLab token but no route" in w for w in report.warnings)

    def test_unknown_route_command_warns_but_is_not_fatal(
        self, tmp_path, monkeypatch, clean_env
    ):
        """BYOA: a binary can be baked into a custom image with no install script."""

        credential(monkeypatch, "custom")
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        routes = write_routes(tmp_path, [("custom", "custom-cli")])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert report.ok
        assert any("custom-cli" in w for w in report.warnings)

    def test_all_mentions_drift_warns(self, tmp_path, monkeypatch, clean_env):
        monkeypatch.setattr(settings, "all_mentions_agents", "claude,opencode-kimi")
        credential(monkeypatch, "claude")
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        routes = write_routes(tmp_path, [("claude", "claude")], mentions=["claude"])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert report.ok
        assert any("opencode-kimi" in w for w in report.warnings)
        assert not any("'claude'" in w for w in report.warnings)


class TestVacuousConfig:
    """A config with nothing to dispatch makes every other check vacuous."""

    def test_empty_route_list_is_fatal(self, tmp_path, monkeypatch, clean_env):
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        routes = write_routes(tmp_path, [])

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert not report.ok
        assert any("no dispatchable agent tasks" in e for e in report.errors)

    def test_agent_entry_without_agent_key_is_fatal(
        self, tmp_path, monkeypatch, clean_env
    ):
        """RouteRegistry silently drops entries with no 'agent:' key."""

        routes = tmp_path / "routes.yaml"
        routes.write_text(
            yaml.safe_dump(
                {
                    "routes": [
                        {
                            "name": "typo",
                            "match": {"event": "Issue Hook"},
                            # No 'agent:' key -- dropped at parse time.
                            "agents": [
                                {"task": "triage", "options": {"command": "claude"}}
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert not report.ok
        assert any("no dispatchable agent tasks" in e for e in report.errors)

    def test_empty_command_is_fatal(self, tmp_path, monkeypatch, clean_env):
        """Dispatch would exec nothing; preflight must not fall back silently."""

        credential(monkeypatch, "claude")
        routes = write_routes(tmp_path, [("claude", "")])
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert not report.ok
        assert any("empty 'command'" in e for e in report.errors)
        assert report.installers == []

    def test_absent_command_falls_back_to_agent_name(
        self, tmp_path, monkeypatch, clean_env
    ):
        """Dispatch reads options.get('command', agent), so absence is a fallback."""

        credential(monkeypatch, "claude")
        routes = tmp_path / "routes.yaml"
        routes.write_text(
            yaml.safe_dump(
                {
                    "routes": [
                        {
                            "name": "no-command",
                            "match": {"event": "Issue Hook"},
                            "agents": [{"agent": "claude", "task": "triage"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})

        report = report_for(tmp_path, clean_env, scripts, routes)

        assert report.ok
        assert installed_names(report) == ["install-claude.sh"]


class TestMain:
    def test_prints_installers_and_binaries_and_exits_zero(
        self, tmp_path, monkeypatch, clean_env, capsys
    ):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: clean_env))
        credential(monkeypatch, "gemini")
        scripts = make_scripts(tmp_path, {"install-gemini.sh": "agy"})
        routes = write_routes(tmp_path, [("gemini", "agy")])

        code = preflight.main(
            ["--scripts-dir", str(scripts), "--routes", str(routes)]
        )

        assert code == 0
        stdout = capsys.readouterr().out.splitlines()
        # The entrypoint runs `install=` lines, then verifies `binary=` lines
        # are on PATH once the installs are done.
        assert stdout == [
            f"install={scripts / 'install-gemini.sh'}",
            "binary=agy",
        ]

    def test_missing_route_file_is_fatal(
        self, tmp_path, monkeypatch, clean_env, capsys
    ):
        """A typo'd ROUTE_CONFIG_PATH must not look like a valid empty config."""

        monkeypatch.setattr(Path, "home", staticmethod(lambda: clean_env))
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        missing = tmp_path / "does-not-exist.yaml"

        code = preflight.main(
            ["--scripts-dir", str(scripts), "--routes", str(missing)]
        )

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert "route config not found" in captured.err
        assert str(missing) in captured.err

    def test_exits_nonzero_with_empty_stdout_on_fatal(
        self, tmp_path, monkeypatch, clean_env, capsys
    ):
        """The entrypoint consumes stdout; it must be empty when we fail."""

        monkeypatch.setattr(Path, "home", staticmethod(lambda: clean_env))
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})
        routes = write_routes(tmp_path, [("claude", "claude")])

        code = preflight.main(
            ["--scripts-dir", str(scripts), "--routes", str(routes)]
        )

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert "CLAUDE_AGENT_GITLAB_TOKEN" in captured.err

    def test_undefined_model_variable_is_reported_cleanly(
        self, tmp_path, monkeypatch, clean_env, capsys
    ):
        """RouteRegistry raises on an undefined ${VAR}; surface it, don't traceback."""

        monkeypatch.delenv("MISSING_MODEL", raising=False)
        routes = tmp_path / "routes.yaml"
        routes.write_text(
            yaml.safe_dump(
                {
                    "routes": [
                        {
                            "name": "r",
                            "match": {"event": "Issue Hook"},
                            "agents": [
                                {
                                    "agent": "claude",
                                    "task": "triage",
                                    "options": {
                                        "command": "claude",
                                        "args": ["--model", "${MISSING_MODEL}"],
                                    },
                                }
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        scripts = make_scripts(tmp_path, {"install-claude.sh": "claude"})

        code = preflight.main(
            ["--scripts-dir", str(scripts), "--routes", str(routes)]
        )

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert "MISSING_MODEL" in captured.err
