<!--
Robot Dev Team Project
File: gitlab/readme-gitlab.md
Description: Guidance for the GitLab CE reference deployment.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# GitLab CE Reference Deployment

This directory contains a reference Docker Compose setup for running GitLab CE with the Robot Dev Team File Hook for automatic webhook provisioning.

## Contents

| File | Purpose |
|------|---------|
| `docker-compose.gitlab.yml` | Docker Compose file for GitLab CE with File Hook volume mount |
| `file_hooks/add_webhooks.rb` | File Hook script that auto-creates project webhooks on project creation |

## Quick Start

The `GITLAB_ADMIN_TOKEN` requires a Personal Access Token generated from within the GitLab web UI, so setup is a two-phase process:

1. Edit `docker-compose.gitlab.yml` and set `ROBOT_WEBHOOK_URL` and `GITLAB_WEBHOOK_SECRET`. Leave `GITLAB_ADMIN_TOKEN` as the placeholder for now.
2. Start GitLab for the first time:
   ```bash
   docker compose -f docker-compose.gitlab.yml up -d
   ```
3. Wait for GitLab to finish initializing (this may take several minutes on first boot), then sign in to the web UI.
4. Create an admin Personal Access Token with `api` scope (**Settings > Access Tokens**).
5. Update `GITLAB_ADMIN_TOKEN` in `docker-compose.gitlab.yml` with the new PAT value, then restart:
   ```bash
   docker compose -f docker-compose.gitlab.yml up -d
   ```
6. See `docs/GROUP_SETUP.md` for full setup instructions including group creation and agent membership.

## Image Tag Policy

The compose file defaults to `gitlab/gitlab-ce:latest`. This keeps deployments current with GitLab's frequent release cycle.

**Important:** GitLab requires a very specific upgrade path between major and minor versions. Jumping directly from an old version to `latest` can break the database or cause data loss. When upgrading:

1. **Back up all bind-mounted volumes** (`gitlab_config`, `gitlab_logs`, `gitlab_data`) before every upgrade.
2. **Pin the image tag** to the next version in the required upgrade path rather than pulling `latest` blindly.
3. **Follow the official upgrade path tool** to determine the correct sequence of intermediate versions: <https://gitlab-com.gitlab.io/support/toolbox/upgrade-path/>
4. Upgrade one step at a time, verifying GitLab starts cleanly before proceeding to the next version.
5. Once you reach the desired target version, you can switch back to `latest` for ongoing updates within the same major release.

## Environment Variables

See the environment variables table in `docs/GROUP_SETUP.md` for all File Hook configuration options. Sensitive values (tokens, secrets) should never be committed to version control.
