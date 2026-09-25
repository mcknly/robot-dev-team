"""Robot Dev Team Project
File: tests/test_wrappers.py
Description: Subprocess-based smoke tests for the gitlab-connect and glab-usr
             wrapper scripts. Covers placeholder/scheme loud-fails, input
             validation, list pass-through, --repo injection (host-stripped),
             credential host keying, and informative missing-token errors.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GITLAB_CONNECT = REPO_ROOT / "gitlab-connect"
GLAB_USR = REPO_ROOT / "glab-usr"
# The value both wrappers treat as "not configured". It is also what they must not suggest.
PLACEHOLDER_HOST = "gitlab.example.com"


def _run(script: Path, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    if env:
        base_env.update(env)
    return subprocess.run(
        ["bash", str(script), *args],
        env=base_env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )


def _write_fake(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a clean PATH dir with stubbed glab/git/sudo binaries.

    Each stub logs its argv to a sibling .log file so tests can assert what
    the wrappers invoked. Returns the bin directory; tests prepend it to
    PATH via the env dict passed to _run().
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "_calls").mkdir()

    # Fake glab: log argv and succeed.
    _write_fake(
        bin_dir / "glab",
        f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {bin_dir / "_calls" / "glab.log"}
exit 0
""",
    )
    # Fake sudo: just exec the rest as the current user.
    _write_fake(
        bin_dir / "sudo",
        """#!/usr/bin/env bash
# Skip the "-u USER" prefix that callers use.
if [[ "$1" == "-u" ]]; then
  shift 2
fi
exec "$@"
""",
    )
    # gitlab-connect resolves glab-usr from PATH. Route it to the repository
    # copy so wrapper tests never depend on a developer-machine installation.
    _write_fake(
        bin_dir / "glab-usr",
        f'''#!/usr/bin/env bash
exec bash "{GLAB_USR}" "$@"
''',
    )
    return bin_dir


def _calls_for(bin_dir: Path, name: str) -> list[str]:
    log = bin_dir / "_calls" / f"{name}.log"
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if line]


# ---------------------------------------------------------------------------
# Loud-fail: placeholder host (both scripts)
# ---------------------------------------------------------------------------


class TestPlaceholderHostFail:
    def test_gitlab_connect_rejects_unset_host(self):
        result = _run(GITLAB_CONNECT, "auth", "--agent", "claude", env={})
        assert result.returncode == 1
        assert "GLAB_HOST is not configured" in result.stderr
        assert "gitlab.example.com" in result.stderr

    def test_gitlab_connect_rejects_explicit_placeholder(self):
        result = _run(GITLAB_CONNECT, "auth", "--agent", "claude", env={"GLAB_HOST": "gitlab.example.com"})
        assert result.returncode == 1
        assert "GLAB_HOST is not configured" in result.stderr

    def test_gitlab_connect_rejects_ported_placeholder(self):
        result = _run(GITLAB_CONNECT, "auth", "--agent", "claude", env={"GLAB_HOST": "gitlab.example.com:8080"})
        assert result.returncode == 1
        assert "GLAB_HOST is not configured" in result.stderr

    @pytest.mark.parametrize("script", [GITLAB_CONNECT, GLAB_USR], ids=["gitlab-connect", "glab-usr"])
    def test_placeholder_hint_names_no_example_host_at_all(self, script):
        """The remedy line stays a shape, not an address.

        Two properties, asserted by one equality because one subsumes the other and a check that
        can never fail independently is worse than no check.

        It must not offer the placeholder: both wrappers exist to reject `gitlab.example.com`, so
        "for example, GLAB_HOST=gitlab.example.com" instructs the reader to set the one value
        guaranteed to reproduce the error they are reading. And it must not offer any concrete host
        at all: these two files are copied into the published image, so an example address here
        ships to strangers. The canonical instance is the one that must never appear --
        `tests/test_public_surface.py` enforces its absence across the whole tree -- but the
        durable property is narrower and easier to keep, so it is asserted positively: the line
        names the variable and its shape and leaves the value to the operator.
        """
        args = ("auth", "--agent", "claude") if script == GITLAB_CONNECT else ("claude",)
        result = _run(script, *args, env={})

        assert result.returncode == 1
        hint = next(
            (line for line in result.stderr.splitlines() if "Export GLAB_HOST" in line), None
        )
        assert hint is not None, (
            f"{script.name} rejected the placeholder host without telling the operator how to "
            f"fix it:\n{result.stderr}"
        )
        assert hint.strip() == "Export GLAB_HOST=<your-gitlab-host> and retry."
        assert PLACEHOLDER_HOST not in hint

    def test_gitlab_connect_rejects_scheme_in_host(self):
        result = _run(GITLAB_CONNECT, "auth", "--agent", "claude", env={"GLAB_HOST": "https://git.example.com"})
        assert result.returncode == 1
        assert "must be a bare host" in result.stderr

    def test_glab_usr_rejects_unset_host(self):
        result = _run(GLAB_USR, "claude", env={})
        assert result.returncode == 1
        assert "GLAB_HOST is not configured" in result.stderr

    def test_glab_usr_rejects_ported_placeholder(self):
        result = _run(GLAB_USR, "claude", env={"GLAB_HOST": "gitlab.example.com:443"})
        assert result.returncode == 1
        assert "GLAB_HOST is not configured" in result.stderr

    def test_glab_usr_rejects_scheme_in_api_host(self):
        result = _run(
            GLAB_USR,
            "claude",
            env={
                "GLAB_HOST": "git.example.com",
                "GLAB_API_HOST": "http://api.example.com",
            },
        )
        assert result.returncode == 1
        assert "must be a bare host" in result.stderr


# ---------------------------------------------------------------------------
# Input validation (glab-usr only)
# ---------------------------------------------------------------------------


class TestProtocolValidation:
    def test_rejects_bogus_protocol(self):
        result = _run(
            GLAB_USR,
            "claude",
            env={"GLAB_HOST": "git.example.com", "GLAB_PROTOCOL": "ftp"},
        )
        assert result.returncode == 1
        assert 'GLAB_PROTOCOL must be "http" or "https"' in result.stderr

    def test_accepts_http(self, fake_path: Path, tmp_path: Path):
        # http is allowed; we expect to proceed past validation and hit the
        # token check next. Provide no token so we still exit cleanly with
        # the informative missing-token error.
        result = _run(
            GLAB_USR,
            "claude",
            env={
                "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                "HOME": str(tmp_path),
                "GLAB_HOST": "git.example.com",
                "GLAB_PROTOCOL": "http",
            },
        )
        assert result.returncode == 1
        assert "missing token" in result.stderr


# ---------------------------------------------------------------------------
# Missing-token error names env var and file path
# ---------------------------------------------------------------------------


class TestMissingTokenError:
    def test_message_names_env_var_and_file(self, fake_path: Path, tmp_path: Path):
        result = _run(
            GLAB_USR,
            "claude",
            env={
                "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                "HOME": str(tmp_path),
                "GLAB_HOST": "git.example.com",
            },
        )
        assert result.returncode == 1
        assert "CLAUDE_AGENT_GITLAB_TOKEN" in result.stderr
        assert ".claude/glab-token" in result.stderr

    def test_message_handles_hyphenated_agent_name(self, fake_path: Path, tmp_path: Path):
        result = _run(
            GLAB_USR,
            "qwen-code",
            env={
                "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                "HOME": str(tmp_path),
                "GLAB_HOST": "git.example.com",
            },
        )
        assert result.returncode == 1
        assert "QWEN_CODE_AGENT_GITLAB_TOKEN" in result.stderr
        assert ".qwen-code/glab-token" in result.stderr


# ---------------------------------------------------------------------------
# Self-defense against root-owned ~/.config (regression test for the
# "MR !7 dispatch silently failed" incident: a `docker exec` as root left
# /home/appuser/.config/glab-cli/ root-owned, after which every appuser
# call hit "permission denied" creating the config dir).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root bypasses Unix mode bits, so the guard cannot fire under root pytest",
)
class TestUnwritableConfigDir:
    def test_unwritable_config_dir_fails_loudly(self, fake_path: Path, tmp_path: Path):
        config_dir = tmp_path / ".config"
        config_dir.mkdir(mode=0o500)  # readable but not writable by owner
        config_dir.chmod(0o500)  # explicit chmod: mkdir(mode=) is umask-sensitive
        try:
            result = _run(
                GLAB_USR,
                "claude",
                env={
                    "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                    "HOME": str(tmp_path),
                    "GLAB_HOST": "git.example.com",
                    "CLAUDE_AGENT_GIT_EMAIL": "claude@example.test",
                    "CLAUDE_AGENT_GITLAB_TOKEN": "fake-token",
                },
            )
        finally:
            config_dir.chmod(0o700)  # restore so pytest cleanup can rmtree
        assert result.returncode == 1
        assert "exists but is not writable" in result.stderr
        assert "chown -R" in result.stderr

    def test_unwritable_glab_cli_subdir_fails_loudly(self, fake_path: Path, tmp_path: Path):
        config_dir = tmp_path / ".config"
        config_dir.mkdir(mode=0o700)
        config_dir.chmod(0o700)
        glab_dir = config_dir / "glab-cli"
        glab_dir.mkdir(mode=0o500)
        glab_dir.chmod(0o500)
        try:
            result = _run(
                GLAB_USR,
                "claude",
                env={
                    "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                    "HOME": str(tmp_path),
                    "GLAB_HOST": "git.example.com",
                    "CLAUDE_AGENT_GIT_EMAIL": "claude@example.test",
                    "CLAUDE_AGENT_GITLAB_TOKEN": "fake-token",
                },
            )
        finally:
            glab_dir.chmod(0o700)
        assert result.returncode == 1
        assert "glab-cli" in result.stderr
        assert "not writable" in result.stderr

    def test_unwritable_gitconfig_fails_loudly(self, fake_path: Path, tmp_path: Path):
        # The directories may be appuser-owned but ~/.gitconfig itself
        # can land root-owned if `git config --global` ran as root.
        gitconfig = tmp_path / ".gitconfig"
        gitconfig.write_text("[user]\n  email = root@example.invalid\n")
        gitconfig.chmod(0o400)
        try:
            result = _run(
                GLAB_USR,
                "claude",
                env={
                    "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                    "HOME": str(tmp_path),
                    "GLAB_HOST": "git.example.com",
                    "CLAUDE_AGENT_GIT_EMAIL": "claude@example.test",
                    "CLAUDE_AGENT_GITLAB_TOKEN": "fake-token",
                },
            )
        finally:
            gitconfig.chmod(0o600)
        assert result.returncode == 1
        assert ".gitconfig" in result.stderr
        assert "not writable" in result.stderr

    def test_unwritable_glab_config_yml_fails_loudly(self, fake_path: Path, tmp_path: Path):
        # The glab-cli dir may be writable, but the config.yml itself can
        # land root-owned if `glab` ran as root after the dir already existed.
        cfg = tmp_path / ".config" / "glab-cli"
        cfg.mkdir(parents=True, mode=0o700)
        cfg.chmod(0o700)
        config_yml = cfg / "config.yml"
        config_yml.write_text("hosts:\n  example: {}\n")
        config_yml.chmod(0o400)
        try:
            result = _run(
                GLAB_USR,
                "claude",
                env={
                    "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                    "HOME": str(tmp_path),
                    "GLAB_HOST": "git.example.com",
                    "CLAUDE_AGENT_GIT_EMAIL": "claude@example.test",
                    "CLAUDE_AGENT_GITLAB_TOKEN": "fake-token",
                },
            )
        finally:
            config_yml.chmod(0o600)
        assert result.returncode == 1
        assert "config.yml" in result.stderr
        assert "not writable" in result.stderr

    def test_unwritable_git_credentials_fails_loudly(self, fake_path: Path, tmp_path: Path):
        # The guard tracks the per-host store glab-usr actually writes, not
        # git's shared ~/.git-credentials, which the script no longer touches.
        creds = tmp_path / ".config" / "glab-cli" / "git-credentials-git.example.com"
        creds.parent.mkdir(parents=True)
        creds.write_text("https://token@git.example.com\n")
        creds.chmod(0o400)
        try:
            result = _run(
                GLAB_USR,
                "claude",
                env={
                    "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                    "HOME": str(tmp_path),
                    "GLAB_HOST": "git.example.com",
                    "CLAUDE_AGENT_GIT_EMAIL": "claude@example.test",
                    "CLAUDE_AGENT_GITLAB_TOKEN": "fake-token",
                },
            )
        finally:
            creds.chmod(0o600)
        assert result.returncode == 1
        assert "git-credentials-git.example.com" in result.stderr
        assert "not writable" in result.stderr

    def test_missing_config_dir_is_fine(self, fake_path: Path, tmp_path: Path):
        # The check must not trip on the normal first-run case where
        # ~/.config doesn't exist yet.
        # Runs in a throwaway repo: with no cwd, glab-usr would resolve the
        # checkout pytest was invoked from and rewrite its real credentials.
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        result = _run(
            GLAB_USR,
            "claude",
            env={
                "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                "HOME": str(tmp_path),
                "GLAB_HOST": "git.example.com",
                "CLAUDE_AGENT_GIT_EMAIL": "claude@example.test",
                "CLAUDE_AGENT_GITLAB_TOKEN": "fake-token",
            },
            cwd=repo,
        )
        # Should proceed past the writability check; exit code depends on
        # whether the fake-glab stub authenticates cleanly. We only assert
        # the writability error did not fire.
        assert "exists but is not writable" not in result.stderr


# ---------------------------------------------------------------------------
# Help text mentions the new `list` action
# ---------------------------------------------------------------------------


class TestHelpText:
    def test_issue_help_includes_list(self):
        result = _run(
            GITLAB_CONNECT,
            "--no-auth",
            "issue",
            "--help",
            env={"GLAB_HOST": "git.example.com"},
        )
        assert result.returncode == 0, result.stderr
        assert "list" in result.stdout
        assert "--state, --label, --author" in result.stdout

    def test_mr_help_includes_list(self):
        result = _run(
            GITLAB_CONNECT,
            "--no-auth",
            "mr",
            "--help",
            env={"GLAB_HOST": "git.example.com"},
        )
        assert result.returncode == 0, result.stderr
        assert "list" in result.stdout
        assert "--source-branch" in result.stdout


# ---------------------------------------------------------------------------
# list pass-through: action is recognized and dispatches to `glab ... list`
# ---------------------------------------------------------------------------


class TestListPassThrough:
    def _bootstrap_writable_repo(self, tmp_path: Path) -> Path:
        """Init a real git repo so gitlab-connect's read-only check returns
        false and it skips the --repo injection path. Sets HOME inside the
        repo so glab-usr never tries to mutate the user's real home."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://git.example.com/owner/proj.git"], cwd=repo, check=True)
        return repo

    def test_issue_list_forwards_args(self, fake_path: Path, tmp_path: Path):
        repo = self._bootstrap_writable_repo(tmp_path)
        # Provide a token file so glab-usr completes authentication.
        token_dir = tmp_path / ".claude"
        token_dir.mkdir()
        (token_dir / "glab-token").write_text("fake-token")
        result = _run(
            GITLAB_CONNECT,
            "--agent",
            "claude",
            "issue",
            "list",
            "--state",
            "opened",
            "--author",
            "alice",
            env={
                "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                "HOME": str(tmp_path),
                "GLAB_HOST": "git.example.com",
                "CLAUDE_AGENT_GIT_EMAIL": "claude@example.test",
            },
            cwd=repo,
        )
        assert result.returncode == 0, result.stderr
        glab_calls = _calls_for(fake_path, "glab")
        # The auth-login call is invoked first; the `issue list` call is last.
        assert any(call.startswith("issue list") and "--state opened" in call and "--author alice" in call for call in glab_calls), \
            f"expected `glab issue list --state opened --author alice`; got {glab_calls!r}"

    def test_mr_list_with_source_branch(self, fake_path: Path, tmp_path: Path):
        repo = self._bootstrap_writable_repo(tmp_path)
        token_dir = tmp_path / ".claude"
        token_dir.mkdir()
        (token_dir / "glab-token").write_text("fake-token")
        result = _run(
            GITLAB_CONNECT,
            "--agent",
            "claude",
            "mr",
            "list",
            "--source-branch",
            "feature/foo",
            env={
                "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                "HOME": str(tmp_path),
                "GLAB_HOST": "git.example.com",
                "CLAUDE_AGENT_GIT_EMAIL": "claude@example.test",
            },
            cwd=repo,
        )
        assert result.returncode == 0, result.stderr
        glab_calls = _calls_for(fake_path, "glab")
        assert any(call.startswith("mr list") and "--source-branch feature/foo" in call for call in glab_calls), \
            f"expected `glab mr list --source-branch feature/foo`; got {glab_calls!r}"

    def test_ls_alias_normalizes_to_list(self, fake_path: Path, tmp_path: Path):
        repo = self._bootstrap_writable_repo(tmp_path)
        token_dir = tmp_path / ".claude"
        token_dir.mkdir()
        (token_dir / "glab-token").write_text("fake-token")
        result = _run(
            GITLAB_CONNECT,
            "--agent",
            "claude",
            "issue",
            "ls",
            env={
                "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                "HOME": str(tmp_path),
                "GLAB_HOST": "git.example.com",
                "CLAUDE_AGENT_GIT_EMAIL": "claude@example.test",
            },
            cwd=repo,
        )
        assert result.returncode == 0, result.stderr
        assert any(call.startswith("issue list") for call in _calls_for(fake_path, "glab"))


# ---------------------------------------------------------------------------
# --repo injection strips the host prefix in read-only mounts
# ---------------------------------------------------------------------------


class TestRepoInjectionStripsHost:
    def test_readonly_repo_passes_owner_project_only(self, fake_path: Path, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://gitlab.internal:8080/team/widget.git"],
            cwd=repo,
            check=True,
        )
        # Make .git read-only to trigger the read-only injection path.
        # chmod the dir itself so test -w fails.
        (repo / ".git").chmod(0o555)
        try:
            result = _run(
                GITLAB_CONNECT,
                "issue",
                "view",
                "42",
                env={
                    "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                    "HOME": str(tmp_path),
                    "GLAB_HOST": "git.example.com",
                },
                cwd=repo,
            )
        finally:
            (repo / ".git").chmod(0o755)
        assert result.returncode == 0, result.stderr
        glab_calls = _calls_for(fake_path, "glab")
        assert any("--repo team/widget" in call for call in glab_calls), \
            f"expected --repo team/widget (no host); got {glab_calls!r}"
        assert not any("gitlab.internal" in call for call in glab_calls), \
            f"host leaked into --repo arg: {glab_calls!r}"


# ---------------------------------------------------------------------------
# Credential host fix: credentials key on GLAB_HOST, not GLAB_API_HOST
# ---------------------------------------------------------------------------


class TestCredentialHostFix:
    def test_credentials_use_web_host_not_api_host(self, fake_path: Path, tmp_path: Path):
        # Set up a writable repo so update_git_credentials writes a
        # repository-scoped credential file we can inspect.
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        # Provide token via env so glab-usr proceeds to auth.
        token_dir = tmp_path / ".claude"
        token_dir.mkdir()
        (token_dir / "glab-token").write_text("fake-token")
        result = _run(
            GLAB_USR,
            "claude",
            env={
                "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                "HOME": str(tmp_path),
                # Web host and API host intentionally differ so the test
                # observes which one ends up in the credential URL.
                "GLAB_HOST": "git.example.com",
                "GLAB_API_HOST": "api.example.com:8080",
                "GLAB_PROTOCOL": "https",
                "CLAUDE_AGENT_GIT_EMAIL": "claude@example.test",
            },
            cwd=repo,
        )
        assert result.returncode == 0, result.stderr
        cred_file = repo / ".git" / "credentials"
        assert cred_file.exists(), "expected repo-scoped credential file"
        contents = cred_file.read_text()
        assert "@git.example.com" in contents, f"credentials must key on GLAB_HOST; got: {contents!r}"
        assert "api.example.com" not in contents, f"credentials must NOT key on GLAB_API_HOST; got: {contents!r}"


# ---------------------------------------------------------------------------
# Credential helper precedence (issue #40) and per-host global store (#42).
#
# Every assertion below reads only the `username=` line of `git credential
# fill`, so token values never reach test output or a CI log.
# ---------------------------------------------------------------------------


def _isolated_git_env(home: Path) -> dict[str, str]:
    """Env that pins git to a throwaway global config and no system config."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _usr_env(fake_path: Path, home: Path, **extra: str) -> dict[str, str]:
    env = _isolated_git_env(home)
    env["PATH"] = f"{fake_path}:{os.environ.get('PATH', '')}"
    env["GLAB_HOST"] = "git.example.com"
    env["CLAUDE_AGENT_GITLAB_TOKEN"] = "fake-token"
    env["CLAUDE_AGENT_GIT_EMAIL"] = "claude@example.test"
    env.update(extra)
    return env


def _seed_global_helper(home: Path, *entries: str) -> None:
    """Install a global credential.helper answering as a different agent.

    This is the state that caused the misattribution in issue #40: the
    operator already had a global store, so it answered before any
    repository-local helper glab-usr configured.
    """
    store = home / "preexisting-store"
    store.write_text("".join(f"{entry}\n" for entry in entries))
    store.chmod(0o600)
    subprocess.run(
        ["git", "config", "--global", "credential.helper", f"store --file {store}"],
        env=_isolated_git_env(home),
        check=True,
        capture_output=True,
    )


def _credential_username(
    home: Path,
    *,
    host: str,
    protocol: str = "https",
    cwd: Path | None = None,
    config_args: list[str] | None = None,
) -> str:
    """Return the username git resolves for a host, or "" if none answers.

    *config_args* are passed to git ahead of the subcommand, which is how a
    caller overrides a checkout's own helper without writing to it.
    """
    result = subprocess.run(
        ["git", *(config_args or []), "credential", "fill"],
        input=f"protocol={protocol}\nhost={host}\n\n",
        env=_isolated_git_env(home),
        cwd=str(cwd) if cwd else str(home),
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("username="):
            return line[len("username="):]
    return ""


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


class TestRepositoryHelperPrecedence:
    def test_repo_helper_wins_over_inherited_global_helper(self, fake_path: Path, tmp_path: Path):
        # The exact reproduction from issue #40: a valid global credential
        # for the same host, belonging to another agent.
        _seed_global_helper(tmp_path, "https://grok:global-token@git.example.com")
        repo = _init_repo(tmp_path / "repo")
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo)
        assert result.returncode == 0, result.stderr
        assert _credential_username(tmp_path, host="git.example.com", cwd=repo) == "claude"

    def test_scoped_key_holds_reset_then_store(self, fake_path: Path, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo)
        assert result.returncode == 0, result.stderr
        values = subprocess.run(
            ["git", "config", "--local", "--get-all", "credential.https://git.example.com.helper"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        # An empty first value is git's helper-list reset; it must come
        # before the store or inherited helpers stay ahead in the list.
        assert values[0] == "", f"expected an empty reset value first; got {values!r}"
        assert len(values) == 2 and "credentials" in values[1], f"unexpected helper list: {values!r}"

    def test_other_hosts_still_reach_the_global_helper(self, fake_path: Path, tmp_path: Path):
        # The reset is host-scoped, so unrelated hosts must be unaffected.
        _seed_global_helper(
            tmp_path,
            "https://grok:global-token@git.example.com",
            "https://mirror-user:mirror-token@github.com",
        )
        repo = _init_repo(tmp_path / "repo")
        _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo)
        assert _credential_username(tmp_path, host="github.com", cwd=repo) == "mirror-user"

    def test_switching_agents_replaces_the_repository_credential(self, fake_path: Path, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        first = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo)
        assert first.returncode == 0, first.stderr
        assert _credential_username(tmp_path, host="git.example.com", cwd=repo) == "claude"
        second = _run(
            GLAB_USR, "codex",
            env=_usr_env(
                fake_path, tmp_path,
                CODEX_AGENT_GITLAB_TOKEN="fake-token",
                CODEX_AGENT_GIT_EMAIL="codex@example.test",
            ),
            cwd=repo,
        )
        assert second.returncode == 0, second.stderr
        assert _credential_username(tmp_path, host="git.example.com", cwd=repo) == "codex"

    def test_repeated_runs_do_not_duplicate_helper_entries(self, fake_path: Path, tmp_path: Path):
        # glab-usr runs on every dispatch, so the scoped key must be
        # rewritten rather than appended to.
        repo = _init_repo(tmp_path / "repo")
        for _ in range(3):
            assert _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo).returncode == 0
        values = subprocess.run(
            ["git", "config", "--local", "--get-all", "credential.https://git.example.com.helper"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        assert len(values) == 2, f"helper list grew across runs: {values!r}"

    def test_http_protocol_request_resolves_to_the_selected_agent(self, fake_path: Path, tmp_path: Path):
        # A https-scoped key does not match an http request, so the key has
        # to be built from GLAB_PROTOCOL.
        _seed_global_helper(tmp_path, "http://grok:global-token@git.example.com")
        repo = _init_repo(tmp_path / "repo")
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path, GLAB_PROTOCOL="http"), cwd=repo)
        assert result.returncode == 0, result.stderr
        assert _credential_username(tmp_path, host="git.example.com", protocol="http", cwd=repo) == "claude"

    def test_ported_host_request_resolves_to_the_selected_agent(self, fake_path: Path, tmp_path: Path):
        # A portless key does not match a host:port request.
        _seed_global_helper(tmp_path, "https://grok:global-token@git.example.com:8080")
        repo = _init_repo(tmp_path / "repo")
        result = _run(
            GLAB_USR, "claude",
            env=_usr_env(fake_path, tmp_path, GLAB_HOST="git.example.com:8080"),
            cwd=repo,
        )
        assert result.returncode == 0, result.stderr
        assert _credential_username(tmp_path, host="git.example.com:8080", cwd=repo) == "claude"

    def test_checkout_path_containing_a_space(self, fake_path: Path, tmp_path: Path):
        # Git runs helpers through a shell, so the store path must be quoted
        # or the helper aborts with a usage error and nothing answers.
        repo = _init_repo(tmp_path / "my repo")
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo)
        assert result.returncode == 0, result.stderr
        assert _credential_username(tmp_path, host="git.example.com", cwd=repo) == "claude"

    def test_checkout_path_containing_a_single_quote(self, fake_path: Path, tmp_path: Path):
        # The escaping branch of shell_quote, which a future edit is most
        # likely to break: a bare '' pair would terminate the quoted string
        # and leave the rest of the path as separate helper arguments.
        repo = _init_repo(tmp_path / "it's a repo")
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo)
        assert result.returncode == 0, result.stderr
        assert _credential_username(tmp_path, host="git.example.com", cwd=repo) == "claude"

    def test_legacy_generic_helper_is_cleaned_without_clobbering_others(self, fake_path: Path, tmp_path: Path):
        # Older glab-usr versions wrote a generic credential.helper. Only
        # that exact value may be removed; unrelated repo helpers stay.
        repo = _init_repo(tmp_path / "repo")
        legacy = f"store --file {repo}/.git/credentials"
        for value in (legacy, "cache"):
            subprocess.run(["git", "config", "--local", "--add", "credential.helper", value], cwd=repo, check=True)
        assert _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo).returncode == 0
        remaining = subprocess.run(
            ["git", "config", "--local", "--get-all", "credential.helper"],
            cwd=repo, capture_output=True, text=True,
        ).stdout.splitlines()
        assert remaining == ["cache"], f"expected only the unrelated helper to survive; got {remaining!r}"


class TestGlobalFallbackStore:
    """Issue #42: the global path must not touch git's shared store."""

    def _run_outside_repo(self, fake_path: Path, tmp_path: Path, **extra: str):
        # A plain directory is not a git repo, so glab-usr takes the global
        # path exactly as it does on a read-only project mount.
        workdir = tmp_path / "not-a-repo"
        workdir.mkdir()
        return _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path, **extra), cwd=workdir)

    def test_shared_git_credentials_is_left_alone(self, fake_path: Path, tmp_path: Path):
        shared = tmp_path / ".git-credentials"
        original = "https://mirror-user:mirror-token@github.com\nhttps://other:other-token@gitlab.other.test\n"
        shared.write_text(original)
        shared.chmod(0o600)
        result = self._run_outside_repo(fake_path, tmp_path)
        assert result.returncode == 0, result.stderr
        assert shared.read_text() == original, "glab-usr must not rewrite git's shared credential store"

    def test_global_store_is_per_host_and_mode_600(self, fake_path: Path, tmp_path: Path):
        result = self._run_outside_repo(fake_path, tmp_path)
        assert result.returncode == 0, result.stderr
        store = tmp_path / ".config" / "glab-cli" / "git-credentials-git.example.com"
        assert store.exists(), "expected a dedicated per-host credential store"
        assert stat.S_IMODE(store.stat().st_mode) == 0o600

    def test_multivalued_global_helper_does_not_block_configuration(self, fake_path: Path, tmp_path: Path):
        # A single-value `git config --global credential.helper` fails with
        # "cannot overwrite multiple values" here; the scoped --add does not.
        for value in ("cache", "store"):
            subprocess.run(
                ["git", "config", "--global", "--add", "credential.helper", value],
                env=_isolated_git_env(tmp_path), check=True, capture_output=True,
            )
        result = self._run_outside_repo(fake_path, tmp_path)
        assert result.returncode == 0, result.stderr
        assert "failed to configure" not in result.stderr
        assert _credential_username(tmp_path, host="git.example.com") == "claude"

    def test_global_fallback_supports_http_and_ported_hosts(self, fake_path: Path, tmp_path: Path):
        result = self._run_outside_repo(
            fake_path, tmp_path, GLAB_HOST="git.example.com:8080", GLAB_PROTOCOL="http",
        )
        assert result.returncode == 0, result.stderr
        assert _credential_username(tmp_path, host="git.example.com:8080", protocol="http") == "claude"


class TestNoCredentialFallback:
    """The scoped reset removes GLAB_HOST's fallback, deliberately.

    A missing or stale store must be a hard authentication failure, not a
    silent fall-through to whatever the operator's global helper returns:
    a failed push is recoverable, a misattributed one is not.  Asserted
    rather than left to be re-derived from git's precedence rules.
    """

    def test_deleting_the_repository_store_leaves_nothing_answering(
        self, fake_path: Path, tmp_path: Path,
    ):
        _seed_global_helper(tmp_path, "https://grok:global-token@git.example.com")
        repo = _init_repo(tmp_path / "repo")
        assert _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo).returncode == 0
        assert _credential_username(tmp_path, host="git.example.com", cwd=repo) == "claude"

        (repo / ".git" / "credentials").unlink()
        # The seeded global helper is still configured and still holds a
        # valid entry for this host; the reset must keep it from answering.
        assert _credential_username(tmp_path, host="git.example.com", cwd=repo) == ""

    def test_deleting_the_global_store_leaves_nothing_answering(
        self, fake_path: Path, tmp_path: Path,
    ):
        _seed_global_helper(tmp_path, "https://grok:global-token@git.example.com")
        workdir = tmp_path / "not-a-repo"
        workdir.mkdir()
        assert _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=workdir).returncode == 0
        assert _credential_username(tmp_path, host="git.example.com") == "claude"

        (tmp_path / ".config" / "glab-cli" / "git-credentials-git.example.com").unlink()
        assert _credential_username(tmp_path, host="git.example.com") == ""


class TestCredentialConfigContract:
    """glab-usr publishes the key and helper it configured, for callers that
    must reproduce them on a git command line.

    The branch pruner authenticates once outside any repository and then runs
    fetch / ``push --delete`` inside each project, where a dispatched agent's
    repository-scoped helper outranks the pruner's global one.  Command-line
    ``-c`` is the only scope above repository config that writes nothing --
    re-authenticating with the repo as cwd would rewrite .git/config under a
    concurrently running agent.
    """

    def _contract_line(self, stderr: str) -> tuple[str, str]:
        prefix = "[glab-usr] CREDENTIAL-CONFIG "
        lines = [ln for ln in stderr.splitlines() if ln.startswith(prefix)]
        assert len(lines) == 1, f"expected exactly one contract line; got {lines!r}"
        fields = lines[0][len(prefix):]
        scope, _, rest = fields.partition(" key=")
        key, _, helper = rest.partition(" helper=")
        return key, helper

    def _config_args(self, key: str, helper: str) -> list[str]:
        # Reset first, then the store -- the same two values, in the same
        # order, that glab-usr writes to config.
        return ["-c", f"{key}=", "-c", f"{key}={helper}"]

    def test_contract_line_reports_protocol_port_and_quoted_store(
        self, fake_path: Path, tmp_path: Path,
    ):
        repo = _init_repo(tmp_path / "my repo")
        result = _run(
            GLAB_USR, "claude",
            env=_usr_env(
                fake_path, tmp_path,
                GLAB_HOST="git.example.com:8080", GLAB_PROTOCOL="http",
            ),
            cwd=repo,
        )
        assert result.returncode == 0, result.stderr
        key, helper = self._contract_line(result.stderr)
        assert key == "credential.http://git.example.com:8080.helper"
        assert helper == f"store --file '{repo}/.git/credentials'"

    def test_contract_line_carries_no_token_material(self, fake_path: Path, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo)
        assert result.returncode == 0, result.stderr
        _, helper = self._contract_line(result.stderr)
        assert "fake-token" not in helper

    def test_stdout_is_unchanged_by_the_contract_line(self, fake_path: Path, tmp_path: Path):
        # Existing callers parse stdout; the contract goes to stderr.
        repo = _init_repo(tmp_path / "repo")
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo)
        assert result.returncode == 0, result.stderr
        assert "CREDENTIAL-CONFIG" not in result.stdout

    def test_published_config_overrides_a_foreign_repository_helper(
        self, fake_path: Path, tmp_path: Path,
    ):
        """The branch-pruner regression, end to end against real git."""
        repo = _init_repo(tmp_path / "repo")
        # A dispatched agent leaves its repository-scoped helper behind.
        assert _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo).returncode == 0

        # The pruner authenticates as a different agent, outside the repo.
        workdir = tmp_path / "not-a-repo"
        workdir.mkdir()
        pruner_env = _usr_env(
            fake_path, tmp_path,
            CODEX_AGENT_GITLAB_TOKEN="fake-token",
            CODEX_AGENT_GIT_EMAIL="codex@example.test",
        )
        auth = _run(GLAB_USR, "codex", env=pruner_env, cwd=workdir)
        assert auth.returncode == 0, auth.stderr
        key, helper = self._contract_line(auth.stderr)

        # Without the override, the checkout's own helper answers: this is
        # the wrong-identity fetch/push the pruner would have performed.
        assert _credential_username(tmp_path, host="git.example.com", cwd=repo) == "claude"
        # With it, the pruning agent does.
        assert _credential_username(
            tmp_path, host="git.example.com", cwd=repo,
            config_args=self._config_args(key, helper),
        ) == "codex"

    def test_override_survives_a_store_path_containing_a_space(
        self, fake_path: Path, tmp_path: Path,
    ):
        # Same shell-quoting constraint as the config path: git hands the
        # -c helper value to a shell too.
        repo = _init_repo(tmp_path / "my repo")
        assert _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo).returncode == 0
        auth = _run(
            GLAB_USR, "codex",
            env=_usr_env(
                fake_path, tmp_path,
                CODEX_AGENT_GITLAB_TOKEN="fake-token",
                CODEX_AGENT_GIT_EMAIL="codex@example.test",
            ),
            cwd=tmp_path,
        )
        assert auth.returncode == 0, auth.stderr
        key, helper = self._contract_line(auth.stderr)
        assert _credential_username(
            tmp_path, host="git.example.com", cwd=repo,
            config_args=self._config_args(key, helper),
        ) == "codex"


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root bypasses Unix mode bits, so a read-only .git cannot be simulated",
)
class TestReadOnlyRepoFallback:
    def test_readonly_git_dir_falls_back_to_the_global_store(self, fake_path: Path, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        (repo / ".git").chmod(0o500)
        try:
            result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo)
        finally:
            (repo / ".git").chmod(0o700)
        assert result.returncode == 0, result.stderr
        assert "configured global credential store" in result.stderr
        assert not (repo / ".git" / "credentials").exists()
        assert (tmp_path / ".config" / "glab-cli" / "git-credentials-git.example.com").exists()

    def test_shadowing_repository_helper_is_reported(self, fake_path: Path, tmp_path: Path):
        """The realistic deployment shape: docker-compose exposes the same
        tree read-write and read-only, so a read-only dispatch reads the
        .git/config a writable dispatch wrote.  Repository config is read
        last, so the global store configured here is never consulted in this
        checkout -- reporting success alone would be misleading.
        """
        repo = _init_repo(tmp_path / "repo")
        assert _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo).returncode == 0

        (repo / ".git").chmod(0o500)
        try:
            result = _run(
                GLAB_USR, "codex",
                env=_usr_env(
                    fake_path, tmp_path,
                    CODEX_AGENT_GITLAB_TOKEN="fake-token",
                    CODEX_AGENT_GIT_EMAIL="codex@example.test",
                ),
                cwd=repo,
            )
        finally:
            (repo / ".git").chmod(0o700)

        # A warning, not a failure: the read-only path cannot push, and
        # glab/gitlab-connect authenticate from GLAB_TOKEN rather than the
        # git helper, so failing would break every read-only dispatch.
        assert result.returncode == 0, result.stderr
        assert "repository-scoped credential helper" in result.stderr
        assert str(repo) in result.stderr
        # And the warning is accurate: claude's store still answers here.
        assert _credential_username(tmp_path, host="git.example.com", cwd=repo) == "claude"

    def test_no_warning_when_the_repository_has_no_helper(self, fake_path: Path, tmp_path: Path):
        repo = _init_repo(tmp_path / "repo")
        (repo / ".git").chmod(0o500)
        try:
            result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=repo)
        finally:
            (repo / ".git").chmod(0o700)
        assert result.returncode == 0, result.stderr
        assert "repository-scoped credential helper" not in result.stderr


# ---------------------------------------------------------------------------
# Placeholder email loud-fail
# ---------------------------------------------------------------------------


class TestPlaceholderEmailFail:
    def test_rejects_default_example_com_email(self, fake_path: Path, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        token_dir = tmp_path / ".claude"
        token_dir.mkdir()
        (token_dir / "glab-token").write_text("fake-token")
        # Deliberately omit CLAUDE_AGENT_GIT_EMAIL; default is claude@example.com.
        result = _run(
            GLAB_USR,
            "claude",
            env={
                "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                "HOME": str(tmp_path),
                "GLAB_HOST": "git.example.com",
            },
            cwd=repo,
        )
        assert result.returncode == 1
        assert "CLAUDE_AGENT_GIT_EMAIL is not configured" in result.stderr
        assert "@example.com" in result.stderr


# ---------------------------------------------------------------------------
# glab auth invocation uses host/api-host/protocol flags as constructed
# ---------------------------------------------------------------------------


class TestAuthCommandConstruction:
    def test_auth_login_passes_split_host_and_protocol(self, fake_path: Path, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        token_dir = tmp_path / ".claude"
        token_dir.mkdir()
        (token_dir / "glab-token").write_text("fake-token")
        result = _run(
            GLAB_USR,
            "claude",
            env={
                "PATH": f"{fake_path}:{os.environ.get('PATH', '')}",
                "HOME": str(tmp_path),
                "GLAB_HOST": "git.example.com",
                "GLAB_API_HOST": "api.example.com:8080",
                "GLAB_PROTOCOL": "http",
                "CLAUDE_AGENT_GIT_EMAIL": "claude@example.test",
            },
            cwd=repo,
        )
        assert result.returncode == 0, result.stderr
        # The first glab invocation is `auth login`; assert all the flags
        # made it through.
        login_calls = [c for c in _calls_for(fake_path, "glab") if c.startswith("auth login")]
        assert login_calls, f"no auth login call observed: {_calls_for(fake_path, 'glab')!r}"
        login = login_calls[0]
        assert "--hostname git.example.com" in login
        assert "--api-host api.example.com:8080" in login
        assert "--api-protocol http" in login
        assert "--git-protocol http" in login


# ---------------------------------------------------------------------------
# Issue #43: repository shapes where .git is a file, not a directory.
# ---------------------------------------------------------------------------


def _init_repo_with_commit(path: Path) -> Path:
    """A repo with a commit -- `git worktree add` needs a resolvable HEAD."""
    _init_repo(path)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
         "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=path, check=True, capture_output=True,
    )
    return path


def _add_worktree(repo: Path, path: Path) -> Path:
    subprocess.run(
        ["git", "worktree", "add", "-q", str(path)],
        cwd=repo, check=True, capture_output=True,
    )
    return path


def _add_submodule(parent: Path, source: Path, name: str) -> Path:
    # Local-path submodules need protocol.file.allow since git 2.38.
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always",
         "-c", "user.name=Test", "-c", "user.email=test@example.test",
         "submodule", "add", "-q", str(source), name],
        cwd=parent, check=True, capture_output=True,
    )
    return parent / name


def _global_values(home: Path, key: str) -> list[str]:
    result = subprocess.run(
        ["git", "config", "--global", "--get-all", key],
        env=_isolated_git_env(home), capture_output=True, text=True,
    )
    return result.stdout.splitlines()


def _local_values(repo: Path, key: str) -> list[str]:
    result = subprocess.run(
        ["git", "config", "--local", "--get-all", key],
        cwd=repo, capture_output=True, text=True,
    )
    return result.stdout.splitlines()


HELPER_KEY = "credential.https://git.example.com.helper"


class TestLinkedWorktreeScope:
    """In a linked worktree .git is a *file* holding a gitdir: pointer.

    The old `-d .git` predicate failed on it and silently took the global
    path: a host-scoped helper and a git identity written into ~/.gitconfig,
    applying to every checkout under that home, from inside a perfectly
    writable repository.
    """

    def _worktree(self, tmp_path: Path) -> tuple[Path, Path]:
        repo = _init_repo_with_commit(tmp_path / "repo")
        return repo, _add_worktree(repo, tmp_path / "wt")

    def test_store_lands_in_the_common_git_dir(self, fake_path: Path, tmp_path: Path):
        repo, wt = self._worktree(tmp_path)
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=wt)
        assert result.returncode == 0, result.stderr
        assert "configured repository credential store" in result.stderr
        assert (repo / ".git" / "credentials").exists(), (
            "store must land in the common git dir, not the global fallback"
        )

    def test_credential_resolves_the_selected_agent(self, fake_path: Path, tmp_path: Path):
        # The operator's own global helper holds a valid entry for this host;
        # the repository-scoped reset must still win inside the worktree.
        _seed_global_helper(tmp_path, "https://grok:global-token@git.example.com")
        _, wt = self._worktree(tmp_path)
        assert _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=wt).returncode == 0
        assert _credential_username(tmp_path, host="git.example.com", cwd=wt) == "claude"

    def test_nothing_is_written_to_global_scope(self, fake_path: Path, tmp_path: Path):
        # The bug's signature is a spurious global write, so the absence is
        # the real regression guard.
        _, wt = self._worktree(tmp_path)
        assert _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=wt).returncode == 0
        assert _global_values(tmp_path, HELPER_KEY) == []
        assert _global_values(tmp_path, "user.name") == []
        assert _global_values(tmp_path, "user.email") == []
        assert not (tmp_path / ".config" / "glab-cli" / "git-credentials-git.example.com").exists()

    def test_identity_is_repository_scoped(self, fake_path: Path, tmp_path: Path):
        repo, wt = self._worktree(tmp_path)
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=wt)
        assert result.returncode == 0, result.stderr
        assert "configured repository git identity" in result.stderr
        assert _local_values(wt, "user.email") == ["claude@example.test"]

    def test_the_gitfile_is_not_treated_as_a_directory(self, fake_path: Path, tmp_path: Path):
        # A partial fix -- changing the predicate but still deriving
        # "$repo_root/.git/credentials" -- turns the silent scoping bug into a
        # hard write failure against a path that is a file.
        _, wt = self._worktree(tmp_path)
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=wt)
        assert result.returncode == 0, result.stderr
        assert (wt / ".git").is_file(), "the worktree gitfile must be left alone"
        assert "Not a directory" not in result.stderr

    def test_contract_line_reports_repository_scope(self, fake_path: Path, tmp_path: Path):
        # branch_pruning.py keys off this line; a global scope here would
        # publish the wrong store for a worktree dispatch.
        repo, wt = self._worktree(tmp_path)
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=wt)
        line = [entry for entry in result.stderr.splitlines() if "CREDENTIAL-CONFIG" in entry][0]
        assert "scope=repository" in line
        assert str(repo / ".git" / "credentials") in line

    def test_ownership_mismatch_is_registered_before_resolution(
        self, fake_path: Path, tmp_path: Path,
    ):
        # Bind-mounted checkouts commonly have a different owner. Git refuses
        # rev-parse before safe.directory is registered, so the manual walk-up
        # must find the worktree gitfile before either git-native resolution.
        repo, wt = self._worktree(tmp_path)
        env = _usr_env(fake_path, tmp_path, GIT_TEST_ASSUME_DIFFERENT_OWNER="1")
        before = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=wt,
            env=env,
            capture_output=True,
            text=True,
        )
        assert before.returncode != 0
        assert "dubious ownership" in before.stderr

        result = _run(GLAB_USR, "claude", env=env, cwd=wt)
        assert result.returncode == 0, result.stderr
        assert "configured repository credential store" in result.stderr
        assert (repo / ".git" / "credentials").exists()
        assert _global_values(tmp_path, HELPER_KEY) == []
        assert _global_values(tmp_path, "safe.directory") == [str(wt)]

    def test_common_dir_legacy_helper_is_cleaned(
        self, fake_path: Path, tmp_path: Path,
    ):
        # Repository-local legacy cleanup must use the resolved common dir,
        # not <worktree>/.git, which is a file and cannot contain the store.
        repo, wt = self._worktree(tmp_path)
        legacy = f"store --file {repo}/.git/credentials"
        for value in (legacy, "cache"):
            subprocess.run(
                ["git", "config", "--local", "--add", "credential.helper", value],
                cwd=wt,
                check=True,
            )

        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=wt)
        assert result.returncode == 0, result.stderr
        assert _local_values(wt, "credential.helper") == ["cache"]

    def test_dispatch_from_a_worktree_subdirectory(self, fake_path: Path, tmp_path: Path):
        # The walk-up loop has to climb to the worktree top level, not past it.
        repo, wt = self._worktree(tmp_path)
        nested = wt / "a" / "b"
        nested.mkdir(parents=True)
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=nested)
        assert result.returncode == 0, result.stderr
        assert (repo / ".git" / "credentials").exists()
        assert _global_values(tmp_path, HELPER_KEY) == []

    def test_sibling_worktrees_share_one_store(self, fake_path: Path, tmp_path: Path):
        """The documented invariant: one agent per *repository*, worktrees
        included.

        `git config --local` from a linked worktree writes the common config,
        so authenticating in one worktree re-points every sibling.  Asserted
        deliberately rather than left as an incidental consequence of
        --git-common-dir: the dispatcher is serialized, and a future change
        that made worktrees look independently assignable would silently
        misattribute pushes.
        """
        repo = _init_repo_with_commit(tmp_path / "repo")
        first = _add_worktree(repo, tmp_path / "wt-a")
        second = _add_worktree(repo, tmp_path / "wt-b")

        assert _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=first).returncode == 0
        assert _credential_username(tmp_path, host="git.example.com", cwd=first) == "claude"

        assert _run(
            GLAB_USR, "codex",
            env=_usr_env(
                fake_path, tmp_path,
                CODEX_AGENT_GITLAB_TOKEN="fake-token",
                CODEX_AGENT_GIT_EMAIL="codex@example.test",
            ),
            cwd=second,
        ).returncode == 0
        for worktree in (first, second, repo):
            assert _credential_username(tmp_path, host="git.example.com", cwd=worktree) == "codex"


class TestSubmoduleScope:
    """A submodule's .git is a file pointing into <parent>/.git/modules/<name>."""

    def _submodule(self, tmp_path: Path) -> tuple[Path, Path]:
        parent = _init_repo_with_commit(tmp_path / "parent")
        source = _init_repo_with_commit(tmp_path / "source")
        return parent, _add_submodule(parent, source, "subdir")

    def test_store_lands_under_git_modules(self, fake_path: Path, tmp_path: Path):
        parent, sub = self._submodule(tmp_path)
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=sub)
        assert result.returncode == 0, result.stderr
        assert "configured repository credential store" in result.stderr
        assert (parent / ".git" / "modules" / "subdir" / "credentials").exists()
        # The parent repository is a separate repository and must be untouched.
        assert not (parent / ".git" / "credentials").exists()

    def test_credential_resolves_the_selected_agent(self, fake_path: Path, tmp_path: Path):
        _seed_global_helper(tmp_path, "https://grok:global-token@git.example.com")
        _, sub = self._submodule(tmp_path)
        assert _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=sub).returncode == 0
        assert _credential_username(tmp_path, host="git.example.com", cwd=sub) == "claude"

    def test_nothing_is_written_to_global_scope(self, fake_path: Path, tmp_path: Path):
        _, sub = self._submodule(tmp_path)
        assert _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=sub).returncode == 0
        assert _global_values(tmp_path, HELPER_KEY) == []
        assert _global_values(tmp_path, "user.name") == []
        assert _global_values(tmp_path, "user.email") == []

    def test_identity_is_repository_scoped(self, fake_path: Path, tmp_path: Path):
        _, sub = self._submodule(tmp_path)
        result = _run(GLAB_USR, "claude", env=_usr_env(fake_path, tmp_path), cwd=sub)
        assert result.returncode == 0, result.stderr
        assert _local_values(sub, "user.email") == ["claude@example.test"]
