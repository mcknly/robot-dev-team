"""Robot Dev Team Project
File: tests/test_goose_config.py
Description: Tests for the container-local Goose config materialization.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from app import goose_config

GATEWAY = "host.docker.internal"


class TestLoopbackDetection:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.0.0.53",  # systemd-resolved; the whole /8 is loopback, not just .1
            "localhost",
            "LocalHost",
            "::1",
            "[::1]",
            "0.0.0.0",  # a bind-all address pasted into a client URL
            "::",
        ],
    )
    def test_loopback_forms(self, host):
        assert goose_config.is_loopback_host(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            "api.openai.com",
            "llama-cpp",
            "192.168.1.50",
            "10.0.0.1",
            "host.docker.internal",
            "",
        ],
    )
    def test_non_loopback_forms(self, host):
        assert goose_config.is_loopback_host(host) is False


class TestRewriteText:
    def test_rewrites_host_and_preserves_port_and_path(self):
        text = '{"base_url": "http://127.0.0.1:10000/v1"}'
        result, replacements = goose_config.rewrite_text(text, GATEWAY)
        assert result == '{"base_url": "http://host.docker.internal:10000/v1"}'
        assert replacements == [
            ("http://127.0.0.1:10000", "http://host.docker.internal:10000")
        ]

    def test_preserves_scheme_and_userinfo(self):
        text = "https://user:pw@localhost:8443/api"
        result, _ = goose_config.rewrite_text(text, GATEWAY)
        assert result == "https://user:pw@host.docker.internal:8443/api"

    def test_rewrites_bracketed_ipv6(self):
        text = "http://[::1]:10000/v1"
        result, _ = goose_config.rewrite_text(text, GATEWAY)
        assert result == "http://host.docker.internal:10000/v1"

    def test_url_without_port_survives(self):
        text = "http://localhost/v1/models"
        result, _ = goose_config.rewrite_text(text, GATEWAY)
        assert result == "http://host.docker.internal/v1/models"

    def test_remote_hosts_untouched(self):
        text = 'base_url: "https://api.openai.com/v1"'
        result, replacements = goose_config.rewrite_text(text, GATEWAY)
        assert result == text
        assert replacements == []

    def test_non_url_text_untouched(self):
        # `localhost` as a bare word, and a loopback-looking string that is not a
        # URL, must both survive -- the rewrite keys on URL shape, not substring.
        text = "description: talks to localhost\nname: 127.0.0.1-profile\n"
        result, replacements = goose_config.rewrite_text(text, GATEWAY)
        assert result == text
        assert replacements == []

    def test_mixed_document_rewrites_only_loopback(self):
        text = (
            "a: http://127.0.0.1:10000/v1\n"
            "b: https://api.anthropic.com\n"
            "c: http://localhost:11434\n"
        )
        result, replacements = goose_config.rewrite_text(text, GATEWAY)
        assert "http://host.docker.internal:10000/v1" in result
        assert "https://api.anthropic.com" in result
        assert "http://host.docker.internal:11434" in result
        assert len(replacements) == 2

    def test_is_idempotent(self):
        once, _ = goose_config.rewrite_text("http://127.0.0.1:10000/v1", GATEWAY)
        twice, replacements = goose_config.rewrite_text(once, GATEWAY)
        assert twice == once
        assert replacements == []


class TestRewriteBareHosts:
    """Not every Goose endpoint is spelled as a URL.

    The provider host variables accept a bare host and Goose prepends the scheme
    itself -- verified against goose 1.41.0, where `OLLAMA_HOST:
    host.docker.internal:10000` connects. A scheme-only rewrite left
    `OLLAMA_HOST: localhost:11434` untouched: the container resolved `localhost`
    to itself and the operator got connection-refused, while the log reported
    "no loopback URLs found".
    """

    def test_rewrites_bare_host_with_port(self):
        text = "OLLAMA_HOST: localhost:11434\n"
        result, replacements = goose_config.rewrite_text(text, GATEWAY)
        assert result == "OLLAMA_HOST: host.docker.internal:11434\n"
        assert replacements == [("localhost:11434", "host.docker.internal:11434")]

    def test_rewrites_bare_host_without_port(self):
        text = "OLLAMA_HOST: 127.0.0.1\n"
        result, _ = goose_config.rewrite_text(text, GATEWAY)
        assert result == "OLLAMA_HOST: host.docker.internal\n"

    def test_rewrites_quoted_and_json_forms(self):
        text = 'OPENAI_HOST: "localhost:10000"\n'
        result, _ = goose_config.rewrite_text(text, GATEWAY)
        assert result == 'OPENAI_HOST: "host.docker.internal:10000"\n'

        text = '  "OLLAMA_HOST": "127.0.0.1:11434",\n'
        result, _ = goose_config.rewrite_text(text, GATEWAY)
        assert result == '  "OLLAMA_HOST": "host.docker.internal:11434",\n'

    def test_leaves_remote_bare_host_alone(self):
        text = "OPENAI_HOST: api.openai.com\n"
        result, replacements = goose_config.rewrite_text(text, GATEWAY)
        assert result == text
        assert replacements == []

    def test_does_not_touch_non_host_keys(self):
        # A model or provider name that happens to look host-ish must not be
        # rewritten -- only *_HOST keys are endpoints.
        text = "GOOSE_MODEL: localhost\nactive_provider: localhost\n"
        result, replacements = goose_config.rewrite_text(text, GATEWAY)
        assert result == text
        assert replacements == []

    def test_url_valued_host_key_is_rewritten_once(self):
        """A *_HOST key carrying a full URL must go through the URL path only --
        the bare-host pass must not then mangle the scheme."""

        text = "OPENAI_HOST: http://127.0.0.1:10000\n"
        result, replacements = goose_config.rewrite_text(text, GATEWAY)
        assert result == "OPENAI_HOST: http://host.docker.internal:10000\n"
        assert len(replacements) == 1

    def test_is_idempotent(self):
        once, _ = goose_config.rewrite_text("OLLAMA_HOST: localhost:11434\n", GATEWAY)
        twice, replacements = goose_config.rewrite_text(once, GATEWAY)
        assert twice == once
        assert replacements == []


class TestMaterialize:
    @pytest.fixture
    def source(self, tmp_path):
        """A stand-in for a real host ~/.config/goose."""

        src = tmp_path / "goose-host"
        (src / "custom_providers").mkdir(parents=True)
        (src / "config.yaml").write_text(
            "extensions:\n"
            "  developer:\n"
            "    enabled: true\n"
            "active_provider: local_llama_cpp\n",
            encoding="utf-8",
        )
        (src / "custom_providers" / "local_llama_cpp.json").write_text(
            json.dumps(
                {"name": "local_llama_cpp", "base_url": "http://127.0.0.1:10000/v1"}
            ),
            encoding="utf-8",
        )
        return src

    def test_rewrites_provider_base_url(self, source, tmp_path):
        target = tmp_path / "goose"
        goose_config.materialize(source, target, GATEWAY)

        provider = json.loads(
            (target / "custom_providers" / "local_llama_cpp.json").read_text()
        )
        assert provider["base_url"] == "http://host.docker.internal:10000/v1"

    def test_leaves_extensions_and_provider_choice_alone(self, source, tmp_path):
        target = tmp_path / "goose"
        goose_config.materialize(source, target, GATEWAY)

        config = (target / "config.yaml").read_text(encoding="utf-8")
        assert "developer" in config
        assert "active_provider: local_llama_cpp" in config

    def test_never_mutates_the_host_config(self, source, tmp_path):
        """The whole design rests on this: rewriting in place would break Goose
        ON the host, where 127.0.0.1 is correct."""

        before = (source / "custom_providers" / "local_llama_cpp.json").read_text()
        goose_config.materialize(source, tmp_path / "goose", GATEWAY)
        after = (source / "custom_providers" / "local_llama_cpp.json").read_text()
        assert before == after
        assert "127.0.0.1" in after

    def test_copies_secrets_verbatim_and_preserves_mode(self, source, tmp_path):
        """A container has no keyring daemon, so secrets.yaml is how a provider
        that does need auth gets its key -- it has to survive the copy intact."""

        secrets = source / "secrets.yaml"
        secrets.write_text("OPENAI_API_KEY: sk-real-key\n", encoding="utf-8")
        secrets.chmod(0o600)

        target = tmp_path / "goose"
        goose_config.materialize(source, target, GATEWAY)

        copied = target / "secrets.yaml"
        assert copied.read_text(encoding="utf-8") == "OPENAI_API_KEY: sk-real-key\n"
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600

    def test_copies_non_utf8_files_bytewise(self, source, tmp_path):
        blob = source / "cache.bin"
        blob.write_bytes(b"\xff\xfe\x00binary")

        target = tmp_path / "goose"
        goose_config.materialize(source, target, GATEWAY)

        assert (target / "cache.bin").read_bytes() == b"\xff\xfe\x00binary"

    def test_rebuilds_target_from_scratch(self, source, tmp_path):
        """A stale rewrite from a previous boot must not survive."""

        target = tmp_path / "goose"
        target.mkdir()
        (target / "stale.yaml").write_text("leftover", encoding="utf-8")

        goose_config.materialize(source, target, GATEWAY)

        assert not (target / "stale.yaml").exists()
        assert (target / "config.yaml").exists()

    def test_is_idempotent_across_boots(self, source, tmp_path):
        target = tmp_path / "goose"
        goose_config.materialize(source, target, GATEWAY)
        first = (target / "custom_providers" / "local_llama_cpp.json").read_text()
        goose_config.materialize(source, target, GATEWAY)
        second = (target / "custom_providers" / "local_llama_cpp.json").read_text()
        assert first == second

    def test_refuses_to_materialize_onto_itself(self, source):
        """Guards the compose typo that would make the container rewrite the
        operator's own host config."""

        with pytest.raises(ValueError, match="onto itself"):
            goose_config.materialize(source, source, GATEWAY)

    def test_refuses_when_target_is_a_mount_point(self, source, tmp_path, monkeypatch):
        """The dangerous half of the compose mistake, and the reason for the guard.

        An operator who *keeps* the read-only staging mount and *adds* a
        conventional `$HOME/.config/goose:/home/appuser/.config/goose` mount
        alongside it (copying the OpenCode pattern) has source != target, so the
        identical-path check above passes -- and rmtree would then walk a bind
        mount of their real host config, deleting secrets.yaml before failing
        EBUSY on the mountpoint. Destructive, on the host, from a boot.
        """

        target = tmp_path / "goose"
        target.mkdir()
        canary = target / "secrets.yaml"
        canary.write_text("OPENAI_API_KEY: sk-real\n", encoding="utf-8")

        monkeypatch.setattr(
            goose_config.os.path, "ismount", lambda path: Path(path) == target
        )

        with pytest.raises(ValueError, match="mount point"):
            goose_config.materialize(source, target, GATEWAY)

        assert canary.read_text(encoding="utf-8") == "OPENAI_API_KEY: sk-real\n"

    def test_refuses_an_empty_source_dir(self, tmp_path):
        """Docker creates the host dir if the mount is uncommented before
        `goose configure` ever ran. Without this check we would wipe the target,
        copy nothing, report success, and let Goose die at first dispatch."""

        empty = tmp_path / "goose-host"
        empty.mkdir()

        with pytest.raises(ValueError, match="no config.yaml"):
            goose_config.materialize(empty, tmp_path / "goose", GATEWAY)

    def test_preserves_directory_modes(self, source, tmp_path):
        """A 0700 source dir must not land as 0755 -- otherwise the carefully
        preserved 0600 secrets.yaml sits in a world-readable directory."""

        (source / "custom_providers").chmod(0o700)

        target = tmp_path / "goose"
        goose_config.materialize(source, target, GATEWAY)

        mode = stat.S_IMODE((target / "custom_providers").stat().st_mode)
        assert mode == 0o700

    def test_failure_leaves_previous_config_intact(self, source, tmp_path, monkeypatch):
        """The copy is staged and swapped, so a mid-flight error does not leave a
        half-written config behind."""

        target = tmp_path / "goose"
        target.mkdir()
        (target / "config.yaml").write_text("previous: config\n", encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(goose_config.shutil, "copystat", _boom)

        with pytest.raises(OSError):
            goose_config.materialize(source, target, GATEWAY)

        assert (target / "config.yaml").read_text(encoding="utf-8") == "previous: config\n"


class TestLocalStreamingWarnings:
    """The check that would have caught the llama.cpp tool-call stream failure.

    That bug is close to invisible: the agent burns minutes of GPU, llama-server
    logs nothing wrong and releases its slot normally, the watchdog never fires,
    and no comment is posted. Only turns emitting a tool call trip it, so a short
    reply succeeds while a real task fails. It has to be caught at boot, because
    nobody will diagnose it from the symptom.
    """

    def _provider(self, tmp_path, **overrides):
        providers = tmp_path / "custom_providers"
        providers.mkdir(exist_ok=True)
        spec = {
            "name": "local_llama_cpp",
            "base_url": f"http://{GATEWAY}:10000/v1",
            "supports_streaming": True,
        }
        spec.update(overrides)
        (providers / "local_llama_cpp.json").write_text(
            json.dumps(spec), encoding="utf-8"
        )
        return tmp_path

    def test_warns_on_local_provider_with_streaming_enabled(self, tmp_path):
        config_dir = self._provider(tmp_path)
        warnings = goose_config.local_streaming_warnings(config_dir, GATEWAY)
        assert len(warnings) == 1
        assert "supports_streaming" in warnings[0]
        assert "local_llama_cpp" in warnings[0]

    def test_silent_once_streaming_is_disabled(self, tmp_path):
        config_dir = self._provider(tmp_path, supports_streaming=False)
        assert goose_config.local_streaming_warnings(config_dir, GATEWAY) == []

    def test_warns_when_streaming_is_merely_unset(self, tmp_path):
        # Absent is not off -- Goose streams by default, so this is the bug.
        providers = tmp_path / "custom_providers"
        providers.mkdir()
        (providers / "p.json").write_text(
            json.dumps({"name": "p", "base_url": f"http://{GATEWAY}:10000/v1"}),
            encoding="utf-8",
        )
        assert len(goose_config.local_streaming_warnings(tmp_path, GATEWAY)) == 1

    def test_silent_for_a_remote_provider(self, tmp_path):
        # Streaming against a cloud provider is fine, and is what feeds stdout to
        # the inactivity watchdog. Warning here would be noise.
        config_dir = self._provider(
            tmp_path, base_url="https://api.openai.com/v1", name="openai"
        )
        assert goose_config.local_streaming_warnings(config_dir, GATEWAY) == []

    def test_warns_on_a_loopback_url_that_was_never_rewritten(self, tmp_path):
        # Belt and braces: catches a local provider even if the rewrite did not
        # fire (e.g. an operator hand-wrote the config inside the image).
        config_dir = self._provider(tmp_path, base_url="http://127.0.0.1:10000/v1")
        assert len(goose_config.local_streaming_warnings(config_dir, GATEWAY)) == 1

    def test_tolerates_a_malformed_provider_file(self, tmp_path):
        providers = tmp_path / "custom_providers"
        providers.mkdir()
        (providers / "broken.json").write_text("{not json", encoding="utf-8")
        assert goose_config.local_streaming_warnings(tmp_path, GATEWAY) == []

    def test_silent_when_there_are_no_custom_providers(self, tmp_path):
        assert goose_config.local_streaming_warnings(tmp_path, GATEWAY) == []


class TestStdioExtensionWarnings:
    """The bind mount carries an extension's config, not its executable."""

    def _write(self, tmp_path, config):
        (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
        return tmp_path

    def test_warns_when_stdio_executable_is_missing(self, tmp_path):
        # The real-world case is `cmd: npx` -- the most common MCP extension
        # launcher, and the image ships no Node.js (dropped when Gemini's harness
        # became `agy`). The test cannot use npx itself: a developer host often
        # *has* it (nvm), which is precisely the asymmetry that makes this bug
        # invisible until the container runs. Use a name nothing can provide.
        config_dir = self._write(
            tmp_path,
            "extensions:\n"
            "  fetch:\n"
            "    enabled: true\n"
            "    type: stdio\n"
            "    cmd: goose-ext-not-installed-anywhere\n",
        )
        warnings = goose_config.stdio_extension_warnings(config_dir)
        assert len(warnings) == 1
        assert "goose-ext-not-installed-anywhere" in warnings[0]
        assert "not on PATH" in warnings[0]

    def test_silent_for_builtin_extensions(self, tmp_path):
        config_dir = self._write(
            tmp_path,
            "extensions:\n"
            "  developer:\n"
            "    enabled: true\n"
            "    type: platform\n",
        )
        assert goose_config.stdio_extension_warnings(config_dir) == []

    def test_silent_when_executable_is_present(self, tmp_path):
        config_dir = self._write(
            tmp_path,
            "extensions:\n"
            "  local:\n"
            "    enabled: true\n"
            "    type: stdio\n"
            "    cmd: sh\n",
        )
        assert goose_config.stdio_extension_warnings(config_dir) == []

    def test_silent_for_disabled_extensions(self, tmp_path):
        config_dir = self._write(
            tmp_path,
            "extensions:\n"
            "  fetch:\n"
            "    enabled: false\n"
            "    type: stdio\n"
            "    cmd: npx\n",
        )
        assert goose_config.stdio_extension_warnings(config_dir) == []


class TestMain:
    def test_missing_mount_is_fatal_when_there_is_no_config_at_all(
        self, tmp_path, capsys
    ):
        """This only runs because the preflight found a Goose route that is both
        enabled and credentialed, so "no config" is not a benign default -- the
        very next webhook would dispatch a Goose with no provider. Fail at boot,
        which is the whole philosophy of the preflight."""

        rc = goose_config.main(
            ["--source", str(tmp_path / "absent"), "--target", str(tmp_path / "goose")]
        )
        assert rc == 1
        assert "FATAL" in capsys.readouterr().err

    def test_missing_mount_is_allowed_when_a_config_is_baked_in(self, tmp_path, capsys):
        """The one legitimate no-mount setup: a config baked into the image."""

        target = tmp_path / "goose"
        target.mkdir()
        (target / "config.yaml").write_text("active_provider: openai\n", encoding="utf-8")

        rc = goose_config.main(
            ["--source", str(tmp_path / "absent"), "--target", str(target)]
        )
        assert rc == 0
        assert "using the existing config" in capsys.readouterr().err

    def test_reports_each_rewrite(self, tmp_path, capsys):
        source = tmp_path / "goose-host"
        source.mkdir()
        (source / "config.yaml").write_text(
            "base_url: http://127.0.0.1:10000/v1\n", encoding="utf-8"
        )

        rc = goose_config.main(
            ["--source", str(source), "--target", str(tmp_path / "goose")]
        )

        assert rc == 0
        assert "http://127.0.0.1:10000 -> http://host.docker.internal:10000" in (
            capsys.readouterr().out
        )
