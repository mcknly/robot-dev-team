"""Robot Dev Team Project
File: app/core/config.py
Description: Application configuration via environment variables.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Base application settings."""

    app_name: str = "Robot Dev Team Webhook Listener"
    app_host: str = "127.0.0.1"
    app_port: int = 8080
    app_log_level: str = "INFO"
    gitlab_webhook_secret: str = ""
    glab_host: str = "gitlab.com"
    glab_token: str = ""
    route_config_path: str = "config/routes.yaml"
    prompt_dir: str = "prompts"
    run_logs_dir: str = "run-logs"
    npm_cache_dir: str = "/work/.npm-cache"
    enable_auto_clone: bool = False
    auto_clone_depth: int = 0
    enable_branch_switch: bool = False
    enable_smart_branch_selection: bool = True
    enable_auto_unassign: bool = False
    enable_backup_notifications: bool = True
    mention_hold_seconds: float = 3.0
    debug_reload_routes: bool = False
    glab_timeout_seconds: int = 30
    agent_max_wall_clock_seconds: int = 7200
    agent_max_inactivity_seconds: int = 900
    agent_timeout_grace_seconds: int = 10
    live_dashboard_enabled: bool = False
    all_mentions_agents: str = "claude,gemini,codex"

    # Log pruning settings
    log_pruning_enabled: bool = True
    log_retention_days: int = 7
    log_pruning_interval_hours: int = 24

    # Branch pruning settings
    branch_pruning_enabled: bool = False
    branch_pruning_interval_hours: int = 24
    branch_pruning_dry_run: bool = True
    branch_pruning_base_branch: str = "main"
    branch_pruning_protected_patterns: str = "main,master,HEAD,backup/*"
    branch_pruning_agent: str = "claude"
    branch_pruning_min_age_hours: int = 24

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
