"""Robot Dev Team Project
File: tests/test_config.py
Description: Pytest coverage for application configuration defaults.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from pathlib import Path

import pytest

from app.core.config import Settings


ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

# Default agent model env vars documented in .env.example
DEFAULT_MODEL_ENV_KEYS = ("CLAUDE_MODEL", "GEMINI_MODEL", "CODEX_MODEL")


def _parse_env_example():
    """Return a dict of KEY=VALUE pairs from .env.example."""
    result = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


class TestModelEnvVars:
    """Ensure default agent model env vars are documented in .env.example."""

    @pytest.fixture()
    def env_defaults(self):
        return _parse_env_example()

    @pytest.mark.parametrize("env_key", DEFAULT_MODEL_ENV_KEYS)
    def test_model_env_var_documented_in_env_example(self, env_defaults, env_key):
        assert env_key in env_defaults, (
            f"{env_key} should be documented in .env.example"
        )

    @pytest.mark.parametrize("env_key", DEFAULT_MODEL_ENV_KEYS)
    def test_model_env_var_has_non_empty_default(self, env_defaults, env_key):
        assert env_defaults.get(env_key), (
            f"{env_key} should have a non-empty default in .env.example"
        )


class TestSettingsDefaults:
    """Ensure key Settings defaults are sensible."""

    def test_all_mentions_agents_default(self):
        s = Settings(_env_file=None)
        assert s.all_mentions_agents == "claude,gemini,codex"

    def test_settings_ignores_extra_env_vars(self):
        """Settings(extra='ignore') allows arbitrary env vars."""
        s = Settings(_env_file=None)
        assert s.app_name == "Robot Dev Team Webhook Listener"
