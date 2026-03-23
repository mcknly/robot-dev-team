<!--
Robot Dev Team Project
File: docs/ADD_NEW_PROJECT.md
Description: Checklist for adding a new project to an existing deployment.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Adding a New Project

This checklist covers all steps required to connect a new GitLab project to an existing Robot Dev Team deployment. It assumes you already have the webhook service running and at least one project working.

> **First-time setup?** Start with the [Quick Start](../README.md#quick-start) guide in README.md instead.

## Prerequisites

Before you begin, confirm:
- The Robot Dev Team service is running (Docker or local).
- You have admin or maintainer access to the target GitLab project.
- Agent GitLab accounts (`claude`, `gemini`, `codex`) already exist.

## Checklist

### 1. Grant Agent Access

**Option A -- Group membership (recommended for multi-project setups):**
If your project belongs to a GitLab Group that already has agent accounts as members, access is inherited automatically. Skip to step 2.

**Option B -- Per-project invitation:**
1. Navigate to your project in GitLab.
2. Go to **Settings > Members > Invite members**.
3. Add each agent account (`claude`, `gemini`, `codex`) with the **Developer** role.
4. Confirm each agent appears in the member list.

> Agents cannot interact with projects they are not members of. This is the most commonly missed step when adding new projects.

### 2. Configure the Webhook

**Option A -- Automatic via File Hook:**
If you have the File Hook installed (see `docs/GROUP_SETUP.md`), webhooks are created automatically when the project is created under the group namespace. Verify by checking **Settings > Webhooks** in the project.

**Option B -- Manual setup:**
1. Navigate to **Settings > Webhooks** in your GitLab project.
2. Click **Add new webhook**.
3. Set **URL** to `https://<host>/webhooks/gitlab`.
4. Paste the shared secret from `GITLAB_WEBHOOK_SECRET` into **Secret token**.
5. Select events: Issues, Merge requests, Notes (comments).
6. Click **Add webhook**.

See `docs/GITLAB_WEBHOOKS.md` for detailed instructions and tunnel tips.

### 3. Set Up the Local Repository

**Option A -- Auto-clone (recommended):**
If `ENABLE_AUTO_CLONE=true` is set in `.env`, the system clones the repository automatically on the first webhook trigger. No manual action needed.

**Option B -- Manual clone:**
Clone the repository into your projects directory, matching GitLab's namespace structure:
```bash
cd /path/to/projects
mkdir -p <namespace>
git clone https://<gitlab-host>/<namespace>/<project>.git <namespace>/<project>
```

### 4. Add Routing Rules

Add entries for the new project in `config/routes.yaml`. At minimum, you need rules that match the project namespace and map events to agents. See `docs/ROUTES.md` for the full schema and examples.

### 5. Restart and Test

1. If you changed `config/routes.yaml` and `DEBUG_RELOAD_ROUTES=true` is set, routes reload automatically. Otherwise, restart the service.
2. In GitLab, go to your project's **Settings > Webhooks**, find the webhook, and click **Test** to send a test event.
3. Confirm a run log appears in the dashboard (if enabled) and in `run-logs/`.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Webhook returns 401 | Secret token mismatch | Verify `GITLAB_WEBHOOK_SECRET` matches the webhook config |
| No triggers created | Missing or mismatched route | Check `config/routes.yaml` namespace and event patterns |
| Agent runs but cannot comment | Agent not a project member | Invite agents via Project > Members (or inherit from Group) |
| Repository not found | Auto-clone disabled and repo not cloned | Enable `ENABLE_AUTO_CLONE=true` or clone manually |

## Related Documentation

- `docs/GROUP_SETUP.md` -- Group-level setup with automatic webhook provisioning via File Hooks.
- `docs/GITLAB_WEBHOOKS.md` -- Detailed webhook configuration and troubleshooting.
- `docs/ENVIRONMENT.md` -- Environment variable reference.
- `docs/ROUTES.md` -- Routing rule schema and examples.
