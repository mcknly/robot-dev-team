<!--
Robot Dev Team Project
File: docs/GITLAB_WEBHOOKS.md
Description: GitLab webhook setup and troubleshooting guidance.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# GitLab Webhook Setup

Follow this guide to connect GitLab projects to the webhook automation service.

> **Multi-project setups:** If you manage several projects, consider using a GitLab Group with a File Hook to automate webhook creation across all projects. The File Hook script at `gitlab/file_hooks/add_webhooks.rb` automatically registers webhooks on newly created projects within a group, eliminating the need to repeat the manual per-project setup below. It uses the same shared secret (`GITLAB_WEBHOOK_SECRET`) and event selection, so webhooks stay consistent across all projects. See `docs/GROUP_SETUP.md` for the full setup guide and `gitlab/docker-compose.gitlab.yml` for a reference GitLab CE deployment that includes File Hook support.

## Create the Webhook (Per-Project)

1. Navigate to your GitLab project.
2. Open **Settings → Webhooks** and click **Add new webhook**.
3. Set **URL** to `https://<host>/webhooks/gitlab` (use your public tunnel or reverse proxy when running locally).
4. Paste the shared secret value from `GITLAB_WEBHOOK_SECRET` into **Secret token**.
5. Select the following events:
   - Issues events
   - Merge request events
   - Note (comment) events
6. Enable **SSL verification** if your endpoint serves HTTPS.
7. Click **Add webhook**, then use **Test → Push events** (or another event) to confirm you receive a `200` response.

## Mention-Triggered Routing

- The automation service evaluates routes per event and per mentioned user.
- Multi-mention comments create independent triggers so each agent runs with focused context.
- Ensure `config/routes.yaml` contains entries matching your project namespace and desired agent combinations.

## Local Development Tips

- Use `./launch-uvicorn-dev` to start the FastAPI server at `http://127.0.0.1:8080`.
- Expose the local port externally using a tunnel service (e.g., `ngrok http 8080`) so GitLab can reach it.
- Update the webhook URL to the tunnel address while keeping `X-Gitlab-Token` consistent with `.env`.

## Troubleshooting

- **401 Unauthorized** — the `X-Gitlab-Token` does not match `GITLAB_WEBHOOK_SECRET`.
- **Timeouts** — verify your service is reachable from GitLab; tunnels must stay open and responsive.
- **No triggers created** — check application logs for route misses and confirm the project directory exists under the mounted `projects/` tree (or enable `ENABLE_AUTO_CLONE=true` for automatic cloning).
- **Agent fails to post results** — run `gitlab-connect` manually to ensure agent credentials are valid.
- **Agents run but cannot post comments** — verify the agent accounts have been invited to the project with **Developer** (or higher) role. Navigate to Project > Members and confirm each agent appears in the member list. For multi-project setups, add agents at the GitLab Group level so access is inherited automatically (see `docs/GROUP_SETUP.md`).

For dashboard monitoring and kill switch operations, see `docs/DASHBOARD_GUIDE.md`.
