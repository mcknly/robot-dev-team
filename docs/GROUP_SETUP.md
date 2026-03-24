<!--
Robot Dev Team Project
File: docs/GROUP_SETUP.md
Description: Guide for GitLab group setup with automatic webhook provisioning.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# GitLab Group and Automatic Webhook Setup

This guide explains how to configure a GitLab Project Group and a File Hook so that new projects automatically receive webhook configuration and agent access. This eliminates the need to manually configure webhooks and agent membership for every new project.

## Overview

The standard per-project setup (documented in `docs/GITLAB_WEBHOOKS.md`) requires you to:
1. Create a webhook in each project individually.
2. Add each agent account as a member of each project.

For teams managing multiple projects, this becomes tedious. The group-based approach solves both problems:

| Concern | Per-Project Setup | Group-Based Setup |
|---------|-------------------|-------------------|
| Webhook creation | Manual per project | Automatic via File Hook |
| Agent membership | Manual per project | Inherited from group |
| New project setup | Repeat all steps | No action needed |
| GitLab tier required | Free | Free (self-managed only) |

## Requirements

- **GitLab Self-Managed** instance (File Hooks are not available on GitLab.com SaaS). The included `gitlab/docker-compose.gitlab.yml` uses the `latest` GitLab CE image.
- Filesystem access to the GitLab server (or ability to mount volumes into the GitLab container)
- A GitLab admin or Maintainer-level Personal Access Token with `api` scope (created automatically on first boot when using `gitlab/docker-compose.gitlab.yml`)

## Step 1: Create a Project Group

1. In GitLab, navigate to **Menu > Groups > Create group**.
2. Choose a group name that reflects your organization or team (e.g., `my-team`).
3. Set the visibility level as appropriate for your projects.
4. Click **Create group**.

All projects created under this group will inherit group-level membership and receive automatic webhook configuration via the File Hook.

## Step 2: Add Agent Accounts to the Group

First, ensure a GitLab user account exists for each agent. You do not need to
set a password or sign in as the agent to generate its Personal Access Token —
use admin impersonation instead:

1. Sign in as an admin and go to **Admin Area > Users > (agent user) > Impersonate**.
2. Navigate to **User Settings > Access Tokens**, create a PAT with `api` scope, and copy the token.
3. Click **Stop impersonating** to return to your admin session.
4. Set the token as `<AGENT>_AGENT_GITLAB_TOKEN` in `.env` (see `docs/ADDING_AN_AGENT.md`).

Then add each agent as a **Developer** member of the group:

1. Navigate to your group and open **Group information > Members**.
2. Click **Invite members**.
3. Add each agent account (`claude`, `gemini`, `codex`, etc.) with the **Developer** role.
4. Click **Invite**.

Group membership is inherited by all projects within the group, so agents will automatically have Developer access to any new project created under the group namespace.

## Step 3: Install the File Hook

The File Hook script (`gitlab/file_hooks/add_webhooks.rb`) runs automatically whenever a new project is created in your GitLab instance. It registers a webhook pointing to the Robot Dev Team listener.

### Deployment

**For containerized GitLab (Docker):**

1. The `gitlab/file_hooks/` directory already contains `add_webhooks.rb`. Ensure it is executable:
   ```bash
   chmod +x gitlab/file_hooks/add_webhooks.rb
   ```

2. Copy the example env file and fill in the values (at minimum, set `ROBOT_WEBHOOK_URL`):
   ```bash
   cp gitlab/.env.example gitlab/.env
   # Edit gitlab/.env — see the variable reference in the table below
   ```

3. Start GitLab:
   ```bash
   docker compose -f gitlab/docker-compose.gitlab.yml up -d
   ```

   On first boot, the bundled `docker-entrypoint-rdt.sh` wrapper runs automatically once GitLab is healthy and performs two setup steps without any manual intervention:

   - **Creates an admin PAT** (`rdt-admin`, `api` scope) by reading the auto-generated root password from `/etc/gitlab/initial_root_password` and running `gitlab-rails runner`. The token is written to `/etc/gitlab/rdt.env` on the `gitlab_config` volume; the File Hook reads it from there.
   - **Enables local network webhook delivery** — calls `PUT /api/v4/application/settings?allow_local_requests_from_web_hooks_and_services=true`, which cannot be set in `gitlab.rb` and must be applied via the API. This allows the File Hook to register webhooks pointing to `host.docker.internal` (a Docker gateway address in the `172.x.x.x` range that GitLab would otherwise reject with `422 Invalid url given`).

   Follow progress with:
   ```bash
   docker logs -f gitlab | grep '\[RDT\]'
   ```

   > **Custom root password:** If `GITLAB_ROOT_PASSWORD` was set, the auto-generated password file is absent and automatic setup is skipped. Create the PAT manually, add it to `gitlab/.env` as `GITLAB_ADMIN_TOKEN`, and run `bash gitlab/setup-gitlab.sh`.

   See `gitlab/readme-gitlab.md` for upgrade guidance.

**For bare-metal GitLab:**

1. Copy the script directly:
   ```bash
   sudo cp gitlab/file_hooks/add_webhooks.rb \
     /opt/gitlab/embedded/service/gitlab-rails/file_hooks/
   sudo chmod +x /opt/gitlab/embedded/service/gitlab-rails/file_hooks/add_webhooks.rb
   sudo chown git:git /opt/gitlab/embedded/service/gitlab-rails/file_hooks/add_webhooks.rb
   ```

2. Set the environment variables in `/etc/gitlab/gitlab.rb`:
   ```ruby
   gitlab_rails['env'] = {
     'GITLAB_ADMIN_TOKEN'    => 'glpat-xxxxxxxxxxxxxxxxxxxx',
     'ROBOT_WEBHOOK_URL'     => 'https://your-host/webhooks/gitlab',
     'GITLAB_WEBHOOK_SECRET' => 'your-shared-secret'
   }
   ```

3. Reconfigure:
   ```bash
   sudo gitlab-ctl reconfigure
   ```

4. Run the setup script (set `GITLAB_URL` and `GITLAB_ADMIN_TOKEN` in the environment if not using `gitlab/.env`):
   ```bash
   GITLAB_URL=https://your-host bash gitlab/setup-gitlab.sh
   ```

### Validation

Run the built-in GitLab rake task to confirm the hook is detected:

```bash
# Containerized
docker exec -it gitlab gitlab-rake file_hooks:validate

# Bare-metal
sudo gitlab-rake file_hooks:validate
```

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `GITLAB_ADMIN_TOKEN` | Admin or Maintainer PAT with `api` scope. For Docker deployments using `gitlab/docker-compose.gitlab.yml`, this is created automatically on first boot and written to `/etc/gitlab/rdt.env` — you do not need to set this manually. For bare-metal, create it in the web UI and set it here. | Docker: No / Bare-metal: Yes |
| `ROBOT_WEBHOOK_URL` | Webhook listener URL (e.g., `https://host/webhooks/gitlab`) | Yes |
| `GITLAB_WEBHOOK_SECRET` | Shared secret matching `GITLAB_WEBHOOK_SECRET` in the Robot Dev Team `.env` | Recommended |
| `GITLAB_API_URL` | GitLab API base URL | No (defaults to `http://localhost:80/api/v4`) |
| `GITLAB_WEBHOOK_SSL_VERIFY` | Enable SSL verification on the created webhook | No (defaults to `true`; set to `false` for self-signed certs) |

### How It Works

1. GitLab fires a `project_create` system event whenever a new project is created.
2. The File Hook receives the event payload via STDIN.
3. The script checks whether a webhook for the target URL already exists on the project (idempotency).
4. If no matching webhook is found, it creates one via the GitLab Projects API with the configured event types and shared secret.
5. Results are logged to `/var/log/gitlab/file_hooks/add_webhooks.log`.

### Configured Webhook Events

The File Hook creates webhooks with the following events enabled:

- Issues events
- Merge request events
- Note (comment) events
- Confidential issues events
- Confidential note events

Push events are disabled by default since the automation service does not route on push events.

## Step 4: Verify the Setup

1. Create a new test project inside your group.
2. Navigate to the project's **Settings > Webhooks** and confirm the webhook was created automatically.
3. Verify the agent accounts appear under the project's **Members** (inherited from the group).
4. Use the webhook **Test** button to send a test event and confirm the Robot Dev Team service responds with `200 OK`.

## Remaining Per-Project Steps

Even with group-level automation, the following still require per-project configuration:

- **`config/routes.yaml`**: Routing rules that map event types and agent assignments to agent commands. Add entries for new projects as needed.

Projects are resolved automatically from the mounted `projects/` directory using the `<namespace>/<project-name>` structure. With `ENABLE_AUTO_CLONE=true`, repositories are cloned on first webhook trigger, so no manual project registration is required.

## Migrating Existing Projects

If you have existing projects with individually configured webhooks:

1. Move (or fork) the projects into the new group namespace.
2. Add agent accounts to the group as described in Step 2.
3. The File Hook only triggers on `project_create`, so **existing projects will not receive automatic webhooks**. You have two options:
   - Manually verify each project already has the correct webhook configured.
   - Run a one-time migration script using the GitLab API to add webhooks to existing projects.
4. **Remove duplicate webhooks**: If a project has both a manually created webhook and a File Hook-provisioned one pointing to the same URL, delete the duplicate to avoid processing events twice. The deduplication service uses `X-Gitlab-Event-UUID`, but duplicate webhooks generate separate UUIDs so both will be processed.

## Troubleshooting

- **File Hook not firing**: Confirm the script is executable (`chmod +x`) and owned by the `git` user. Run `gitlab-rake file_hooks:validate`.
- **Webhook creation fails**: Check `/var/log/gitlab/file_hooks/add_webhooks.log` for error details. Common causes are expired tokens or incorrect API URLs.
- **Agents cannot access project**: Verify the agents are members of the group (not just invited). Check **Group > Members** for confirmed membership.
- **Duplicate webhooks**: If both a manual and File Hook webhook exist, delete the manual one. The File Hook includes an idempotency check to skip creation when the target URL already exists.
- **Webhooks not created after rapid project creation/deletion**: GitLab's `FileHookWorker` enforces a 600-second (10-minute) exclusive lease via Sidekiq. Only one file hook execution is allowed per 10-minute window. During rapid testing (create project, delete, recreate), subsequent hook runs are silently skipped. Wait 10 minutes or restart the GitLab container to clear the lease, then create the project again.
- **Multi-node GitLab**: Deploy the File Hook script to all application and Sidekiq nodes, as any node may process the `project_create` event.
