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
| `docker-entrypoint-rdt.sh` | Entrypoint wrapper that performs automatic first-boot setup |
| `file_hooks/add_webhooks.rb` | File Hook script that auto-creates project webhooks on project creation |
| `setup-gitlab.sh` | Standalone setup script for bare-metal deployments (Docker: runs automatically) |

## Quick Start

Setup is fully automatic on first boot when using the bundled `docker-compose.gitlab.yml`.

1. Copy the example env file and fill in your values:
   ```bash
   cp .env.example .env
   # Edit .env — set ROBOT_WEBHOOK_URL at minimum; see .env.example for all options
   ```
2. Start GitLab:
   ```bash
   docker compose -f docker-compose.gitlab.yml up -d
   ```
3. Wait for first boot to complete. GitLab typically takes 3–5 minutes to become
   healthy. Once healthy, `docker-entrypoint-rdt.sh` automatically:
   - Creates an admin Personal Access Token (`rdt-admin`) using the auto-generated
     root password from `/etc/gitlab/initial_root_password`.
   - Enables outbound requests to local/private network addresses (required for the
     File Hook to register webhooks pointing to `host.docker.internal`).
   - Writes the token to `/etc/gitlab/rdt.env` for the File Hook to read.

   Follow the logs to watch progress:
   ```bash
   docker logs -f gitlab | grep '\[RDT\]'
   ```
4. Log in to the GitLab web UI — see **First Login** below.
5. See `docs/GROUP_SETUP.md` for next steps (create a group, add agent members).

> **Custom root password:** If you set `GITLAB_ROOT_PASSWORD` in your environment,
> the auto-generated `/etc/gitlab/initial_root_password` file is not created and
> automatic PAT setup is skipped. In that case, create an admin PAT manually in
> the web UI, set `GITLAB_ADMIN_TOKEN` in `gitlab/.env`, and restart the container.
> Run `bash setup-gitlab.sh` to apply the local webhook delivery setting.

## First Login

GitLab is available at **http://localhost:8929** once the container is healthy.

### Auto-generated password (default)

On first boot GitLab writes a random root password to `/etc/gitlab/initial_root_password`
inside the container. Read it with:

```bash
docker exec gitlab grep 'Password:' /etc/gitlab/initial_root_password
```

Log in with username `root` and that password. **Change the password immediately**
after signing in: **User menu (top-right) > Edit profile > Password**.

> The file is deleted automatically by GitLab 24 hours after first boot. Retrieve
> it before then, or reset the password via `gitlab-rake "gitlab:password:reset"` if
> you miss the window.

### Known password (optional)

To use a password of your choice from the start, add it to `gitlab/.env` before
the first `docker compose up`:

```ini
GITLAB_ROOT_PASSWORD=your-strong-password
```

GitLab seeds the root account with this value instead of generating one.
The `initial_root_password` file is not created, and the `docker-entrypoint-rdt.sh`
auto-setup will skip PAT creation (see the custom root password note above) — so
also set `GITLAB_ADMIN_TOKEN` in `gitlab/.env` after creating a PAT manually in the
web UI, or omit `GITLAB_ROOT_PASSWORD` and let the entrypoint handle everything
automatically.

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
