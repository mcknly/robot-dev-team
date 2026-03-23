<!--
Robot Dev Team Project
File: docs/SANITIZATION_REPORT.md
Description: Sanitization report and privacy considerations.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Repository Sanitization Report

_Date:_ 2025-10-27  
_Agent:_ Codex

## Summary
- Removed host-specific project mounts and the unused GitLab CLI config bind from `docker-compose.yml`.
- Added `docker-compose.override.example.yml` to document how credentials and project paths should be bound locally without leaking host details.
- Updated `docs/ENVIRONMENT.md` to direct contributors toward the override pattern and clarified that GitLab CLI configuration is generated inside the container.
- Confirmed `.gitignore` excludes `docker-compose.override.yml` so machine-specific settings remain private.
- Replaced `config/projects.yaml` contents with sanitized placeholders; contributors must map their own GitLab namespaces locally.
- Introduced an ignored `config/projects.local.yaml` pattern so operators can supply private namespace mappings via `PROJECT_MAP_PATH`.

## Validation
- `.venv/bin/pytest` — 21 tests passed (warnings only for FastAPI `on_event` deprecation).
- `docker compose build app` — image rebuilt successfully.
- `docker compose up -d app` — container recreated and running (verified via `docker compose ps`).
- `docker compose exec app curl -sf http://127.0.0.1:8080/health` — FastAPI health endpoint reports `{"status":"ok"}` from inside the container.
- `.venv/bin/detect-secrets scan --force-use-all-plugins --exclude-files 'run-logs/|npm-cache/|\.venv/|sbom/'` — no secrets detected.

## Secret & PII Scan
- Tool: `detect-secrets 1.5.0` (all plugins, tracked files only)
- Command: `.venv/bin/detect-secrets scan --force-use-all-plugins --exclude-files 'run-logs/|npm-cache/|\.venv/|sbom/'`
- Result: **No findings** (empty result set)

## Manual Review Notes
- Inspected `docs/ENVIRONMENT.md`, `.env.example`, and `config/projects.yaml` to ensure only sanitized placeholders remain.
- Verified `run-logs/` and `npm-cache/` directories stay ignored with `.gitkeep` stubs only.
- Confirmed agent credential directories (`.claude`, `.gemini`, `.codex`) remain intentionally mountable for host-side auth workflows.

## Ongoing Hygiene Checklist
- Keep real overrides in `docker-compose.override.yml` (ignored by git).
- Re-run `detect-secrets scan` before releases or when new repos/configs are added.
- Avoid force-adding runtime artifacts under `run-logs/` or credential caches.
- Document any future project mounts in `docker-compose.override.example.yml` instead of the base compose file.
