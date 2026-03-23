<!--
Robot Dev Team Project
File: docs/DASHBOARD_GUIDE.md
Description: Dashboard usage and monitoring guide.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
-->

# Dashboard Usage Guide

The live dashboard provides real-time visibility into webhook processing, agent execution, and kill-switch controls. Enable it when operating the stack interactively or during incident response.

## Enabling the Dashboard

1. Set `LIVE_DASHBOARD_ENABLED=true` in `.env`.
2. Restart the FastAPI service (`./launch-uvicorn-dev` or `docker compose up --build`).
3. Visit `http://localhost:8080/dashboard` in a browser (adjust host/port as needed).

The dashboard serves static assets directly from the FastAPI app; no separate build step is required.

## Features

- **Event Stream Panel** — shows inbound webhook metadata, matching route names, and queued agents.
- **Agent Console Tabs** — render prompt, stdout, and thinking (stderr) streams as they arrive.
- **System Logs** — surface application log lines with level indicators for quick scanning.
- **Status Badges** — highlight running, succeeded, and failed triggers.

Dashboard sessions only display events that occur after the page loads, keeping the view focused on current activity.

## Kill Switch Behaviour

- Clicking the **Kill** button terminates the active agent subprocess immediately.
- Remaining agents for the same webhook event are cancelled before they start.
- The trigger queue resumes with the next webhook once termination completes -- no cross-event cancellation occurs.
- FastAPI responds to GitLab with a status payload indicating the cancellation so follow-up automation can react.

**Post-kill actions (when the agent was assigned to an issue/MR):**

1. A **termination comment** is automatically posted on the GitLab issue/MR under the killed agent's identity, providing a visible record of the kill event.
2. When `ENABLE_AUTO_UNASSIGN=true`, the killed agent is automatically **unassigned** from the issue/MR.

Both actions are best-effort -- if the GitLab API call fails (e.g., network issue or token problem), the kill still completes and the failure is logged.

After using the kill switch, review `run-logs/<uuid>-<project>-<route>-<agent>.out.json` for partial output and diagnostics.

## Troubleshooting

- **Dashboard not available** — confirm `LIVE_DASHBOARD_ENABLED` is `true` and the server restarted with the new configuration.
- **Stale content** — force-refresh the page; hot reloading of backend code can temporarily pause websocket updates.
- **Permission errors on assets** — ensure `run-logs/` and `npm-cache/` directories are writable by the container user.

For more operational practices, see the Logging & Observability section in `README.md`.
