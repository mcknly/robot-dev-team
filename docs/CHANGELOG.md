<!--
Robot Dev Team Project
File: docs/CHANGELOG.md
Description: Change history for the Robot Dev Team Project.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Changelog

All notable changes to this project will be documented in this file.

## [v0.1.0] - 2025-10-22

### Features
- **On-demand project cloning** (`ENABLE_AUTO_CLONE`) -- projects are cloned automatically on first webhook trigger with namespace-aware paths and concurrent-clone protection.
- **Automatic branch resolution** (`ENABLE_BRANCH_SWITCH`) -- detects and checks out the relevant branch from the webhook payload before agent dispatch, with divergence handling and backup branch creation.
- **Smart branch selection** (`ENABLE_SMART_BRANCH_SELECTION`) -- uses `closes_issues` API and note mentions to rank linked MRs when resolving branches for issues, preventing false-positive branch switches.
- **Auto-backup notifications** (`ENABLE_BACKUP_NOTIFICATIONS`) -- posts a GitLab comment with recovery instructions when uncommitted changes are backed up during branch resolution.
- **Auto-unassign** (`ENABLE_AUTO_UNASSIGN`) -- automatically unassigns agents after successful task completion or manual kill, using `--assignee '-agent'` to preserve other assignees.
- **Mention hold deduplication** (`MENTION_HOLD_SECONDS`) -- suppresses duplicate dispatches when a mention and assignment webhook arrive for the same agent within a configurable hold window.
- **I/O-driven timeout watchdog** -- dual-limit agent timeout with wall-clock and stdout-inactivity limits, per-route overrides, and timeout notification comments on GitLab.
- **Kill-path enhancements** -- dashboard kill switch now posts a termination comment and auto-unassigns the killed agent.
- **Suppress `/assign` mention route** -- filters out mention-triggered routes when an `/assign` quick action targets the same agent.
- **Non-interactive mode enforcement** -- Claude and Gemini agents are launched with flags that prevent interactive prompts.
- **Cross-project access guidance** -- system prompt includes instructions for browsing read-only project mounts and querying issues/MRs across projects.
- **Attachment download guidance** -- system prompt covers downloading and analyzing GitLab upload attachments.
- **Note event enrichment** -- context builder now fetches additional data for comment/note events.
- **`${EXTRA}` enrichment in templates** -- note followup and MR review prompts include enriched context data.
- **Branch pruning** (`BRANCH_PRUNING_ENABLED`) -- background task prunes merged remote branches with configurable age filter, protected patterns, and dry-run mode.
- **Agent model variables** (`CLAUDE_MODEL`, `GEMINI_MODEL`, `CODEX_MODEL`) -- environment-driven model selection substituted into route arguments.

### Infrastructure
- Migrated agent CLIs to hybrid native installers (Claude and Codex use native installers; Gemini uses npm).
- Migrated base Docker image from Debian Bullseye to Bookworm.
- Migrated deprecated `@app.on_event` to FastAPI lifespan context manager.
- Added `restart: unless-stopped` policy to Docker Compose so the container survives reboots.
- Added `procps` package for Gemini CLI `pgrep` dependency.
- Injected agent-specific `GITLAB_TOKEN` into subprocess environment for per-agent authentication.
- Added `glab-usr` timeouts to agent dispatch and branch resolver to prevent stalls.

### Documentation
- Added Quick Start guidance for Linux, macOS, and Windows (WSL) users.
- Added prominent agent account prerequisite callout to Quick Start sections across all platforms.
- Created `docs/ADD_NEW_PROJECT.md` -- consolidated checklist for adding projects to an existing deployment.
- Broke overloaded Linux Quick Start step 3 into smaller, clearer sub-steps.
- Added `docs/GROUP_SETUP.md` -- GitLab group setup with File Hook for automatic webhook provisioning.
- Added "Why Personal CLI Accounts?" rationale and BYOA philosophy section to README.
- Added `SECURITY.md` with container hardening and operational checklists.
- Published environment configuration reference and onboarding guides under `docs/`.
- Expanded `docs/ENVIRONMENT.md` with all new configuration variables and `APP_LOG_LEVEL` guide.
- Expanded `docs/ROUTES.md` with access mode, assignee routing, pattern matching, and model argument variables.
- Updated `docs/DASHBOARD_GUIDE.md` with kill-path post-actions.
- Added "How Agent CLI Authentication Works" section to `docs/AGENT_ONBOARDING.md`.
- Added "agents cannot post comments" troubleshooting entry to `docs/GITLAB_WEBHOOKS.md`.
- Standardized `projects.local.yaml` vs `projects.yaml` references in webhook troubleshooting.
- Documented live dashboard usage, GitLab webhook setup, and contribution workflow.
- Added MIT license, repository-wide file headers, and automated header validation tooling.
- Documented licensing policy, dependency license table, and header templates in contributor guidance.

### Bug Fixes
- Fixed self-unassign suppression to prevent agent retrigger on auto-unassign webhook events.
- Fixed `gitlab-connect` read-only repo detection, hostname preservation, and SSH port stripping.
- Fixed dashboard auto-follow disabling on mobile browsers and restored desktop resize handles.
- Fixed mention hold buffer concurrency bugs.
- Hardened token validation with override precedence.
- Fixed watchdog to only count stdout activity for inactivity timeout (stderr no longer resets timer).
- Added subprocess timeouts and startup delay to branch pruning to prevent agent stall.
- Preserved backup records when checkout fails after dirty-tree backup.

### Testing
- Expanded test suite to cover pre-release gaps (219+ tests across agents, branch resolver, context builder, dashboard, glab, log pruning, project paths, routes, and webhooks).
- Isolated environment variable leakage in glab and model default tests.
