"""Robot Dev Team Project
File: app/api/dashboard.py
Description: Routes providing the live dashboard UI and websocket feed.
License: MIT
SPDX-License-Identifier: MIT
Copyright (c) 2025 MCKNLY LLC
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, status
from fastapi.responses import HTMLResponse
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.services.agents import kill_event as kill_agents_event
from app.services.dashboard import dashboard_manager

router = APIRouter()


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Agent Run Log Dashboard</title>
  <style>
    :root {
      color-scheme: dark;
    }
    body {
      margin: 0;
      padding: 0;
      font-family: 'IBM Plex Mono', Menlo, Consolas, monospace;
      background-color: #0f172a;
      color: #e2e8f0;
      display: flex;
      flex-direction: column;
      min-height: 100vh;
    }
    header {
      padding: 20px;
      background: #1e293b;
      border-bottom: 1px solid #334155;
    }
    header h1 {
      margin: 0 0 8px 0;
      font-size: 1.6rem;
      letter-spacing: 0.04em;
    }
    header p {
      margin: 4px 0 0 0;
      color: #94a3b8;
      font-size: 0.9rem;
    }
    main {
      flex: 1;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 18px;
      min-height: 0;
    }
    .panes-wrapper {
      display: flex;
      flex-direction: column;
      gap: 18px;
      flex: 1;
      min-height: 0;
    }
    .status-row {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
      font-size: 0.95rem;
      color: #cbd5f5;
    }
    .status-active {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    #active-agents {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
    }
    .active-agent {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-right: 12px;
      margin-bottom: 6px;
    }
    .status-label {
      color: #94a3b8;
      text-transform: uppercase;
      font-size: 0.75rem;
      letter-spacing: 0.08em;
    }
    .active-indicator {
      display: flex;
      align-items: center;
      gap: 6px;
      color: #38bdf8;
    }
    .active-indicator[hidden] {
      display: none;
    }
    .spinner {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      border: 2px solid rgba(148, 163, 184, 0.35);
      border-top-color: #38bdf8;
      animation: spin 1s linear infinite;
    }
    .active-agent-name {
      color: #86efac;
      font-weight: 600;
    }
    @keyframes spin {
      to {
        transform: rotate(360deg);
      }
    }
    @media (max-width: 768px) {
      body {
        font-size: 1.05rem;
      }
      header {
        padding: 16px;
      }
      header h1 {
        font-size: 1.4rem;
      }
      header p {
        font-size: 1rem;
      }
      main {
        padding: 16px;
        gap: 16px;
      }
      .panes-wrapper {
        gap: 16px;
      }
      .status-row {
        flex-direction: column;
        align-items: flex-start;
        gap: 10px;
        font-size: 1rem;
      }
      .actions {
        margin-left: 0;
        width: 100%;
        justify-content: flex-start;
        flex-wrap: wrap;
        gap: 12px;
      }
      button.toggle-button,
      button.clear-button {
        padding: 8px 16px;
        font-size: 0.95rem;
      }
      .pane {
        min-height: 0;
      }
      .pane-content {
        padding: 18px;
      }
      .pane-title {
        font-size: 1rem;
      }
      .log-entries {
        font-size: 0.95rem;
        gap: 8px;
      }
    }
    .pane {
      background: #111827;
      border: 1px solid #334155;
      border-radius: 10px;
      min-height: 0;
      flex: 0 0 auto;
      max-height: none;
      overflow: hidden;
      resize: vertical;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.25);
      position: relative;
      box-sizing: border-box;
      display: flex;
      flex-direction: column;
    }
    .pane-content {
      flex: 1;
      min-height: 0;
      overflow: auto;
      overflow-anchor: none;
      padding: 14px;
    }
    .resize-handle {
      display: none;
      flex-shrink: 0;
      position: relative;
      height: 44px;  /* 44px meets mobile touch target accessibility guidelines */
      cursor: ns-resize;
      touch-action: none;
      background: linear-gradient(to bottom, transparent 0%, rgba(51, 65, 85, 0.4) 100%);
      border-bottom-left-radius: 10px;
      border-bottom-right-radius: 10px;
      z-index: 3;
    }
    .resize-handle::after {
      content: '';
      position: absolute;
      bottom: 10px;
      left: 50%;
      transform: translateX(-50%);
      width: 48px;
      height: 5px;
      background: #475569;
      border-radius: 3px;
    }
    .resize-handle:active::after {
      background: #64748b;
      transform: translateX(-50%) scaleX(1.1);
    }
    @media (pointer: coarse), (max-width: 768px) {
      .pane {
        resize: none;
      }
      .resize-handle {
        display: block;
      }
    }
    .pane-title {
      margin: 0 0 10px 0;
      font-size: 0.9rem;
      color: #e2e8f0;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      position: sticky;
      top: -14px;
      padding-top: 14px;
      padding-bottom: 6px;
      background: #111827;
      z-index: 2;
    }
    .log-entries {
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-size: 0.85rem;
      line-height: 1.4;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .log-entry {
      padding: 6px 8px;
      border-radius: 6px;
    }
    .stdout .log-entry {
      background: rgba(34, 197, 94, 0.12);
      color: #86efac;
    }
    .stderr .log-entry {
      background: rgba(239, 68, 68, 0.12);
      color: #fca5a5;
    }
    .prompt .log-entry {
      background: rgba(59, 130, 246, 0.12);
      color: #bfdbfe;
    }
    .system .log-entry {
      background: rgba(148, 163, 184, 0.12);
      color: #cbd5f5;
    }
    .timestamp {
      color: #94a3b8;
      margin-right: 8px;
    }
    .agent-tag {
      color: #f8fafc;
      margin-right: 6px;
    }
    .connection-status {
      padding: 2px 6px;
      border-radius: 4px;
      background: #0ea5e9;
      color: #0f172a;
      font-weight: 600;
    }
    .actions {
      margin-left: auto;
      display: flex;
      gap: 10px;
      align-items: center;
    }
    button.toggle-button {
      background: #38bdf8;
      color: #0f172a;
      border: none;
      border-radius: 6px;
      padding: 6px 12px;
      font-size: 0.85rem;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.2s ease, opacity 0.2s ease;
    }
    button.toggle-button.off {
      background: #475569;
      color: #e2e8f0;
      opacity: 0.85;
    }
    button.clear-button {
      background: #22c55e;
      color: #0f172a;
      border: none;
      border-radius: 6px;
      padding: 6px 12px;
      font-size: 0.85rem;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.2s ease;
    }
    button.clear-button:hover {
      background: #16a34a;
    }
    button.kill-button {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: #ef4444;
      color: #f8fafc;
      border: none;
      border-radius: 6px;
      padding: 4px 10px;
      font-size: 0.75rem;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.2s ease, opacity 0.2s ease;
    }
    button.kill-button:hover:not(:disabled) {
      background: #dc2626;
    }
    button.kill-button:disabled {
      opacity: 0.6;
      cursor: wait;
    }
    .kill-button .icon {
      font-size: 0.95rem;
    }
  </style>
</head>
<body>
  <header>
    <h1>Agent Run Log Dashboard</h1>
    <div class=\"status-row\">
      <div><span class=\"status-label\">Connection</span> <span id=\"connection-status\" class=\"connection-status\">Connecting…</span></div>
      <div class=\"status-active\"><span class=\"status-label\">Active Agents</span> <span id=\"active-indicator\" class=\"active-indicator\" hidden><span class=\"spinner\"></span></span> <span id=\"active-agents\">None</span></div>
      <div class=\"actions\">
        <button id=\"follow-toggle\" class=\"toggle-button\">Follow: On</button>
        <button id=\"clear-button\" class=\"clear-button\">Clear</button>
      </div>
    </div>
    <p>Live view of agent stdout, thinking (stderr), prompt feeds, and system logs. Leave this tab open to observe new activity in real time.</p>
  </header>
  <main>
    <div class=\"panes-wrapper\">
    <section class=\"pane stdout\">
      <div class=\"pane-content\">
        <h2 class=\"pane-title\">STDOUT</h2>
        <div id=\"stdout-log\" class=\"log-entries\"></div>
      </div>
      <div class=\"resize-handle\" aria-hidden=\"true\"></div>
    </section>
    <section class=\"pane stderr\">
      <div class=\"pane-content\">
        <h2 class=\"pane-title\">THINKING</h2>
        <div id=\"stderr-log\" class=\"log-entries\"></div>
      </div>
      <div class=\"resize-handle\" aria-hidden=\"true\"></div>
    </section>
    <section class=\"pane prompt\">
      <div class=\"pane-content\">
        <h2 class=\"pane-title\">Prompt</h2>
        <div id=\"prompt-log\" class=\"log-entries\"></div>
      </div>
      <div class=\"resize-handle\" aria-hidden=\"true\"></div>
    </section>
    <section class=\"pane system\">
      <div class=\"pane-content\">
        <h2 class=\"pane-title\">System Logs</h2>
        <div id=\"system-log\" class=\"log-entries\"></div>
      </div>
      <div class=\"resize-handle\" aria-hidden=\"true\"></div>
    </section>
    </div>
  </main>
  <script>
    const MIN_PANE_HEIGHT_DESKTOP = 150;
    const MIN_PANE_HEIGHT_MOBILE = 200;
    const minHeightQuery = window.matchMedia('(max-width: 768px)');
    const panes = Array.from(document.querySelectorAll('.pane'));
    const activeIndicator = document.getElementById('active-indicator');
    let activeResizePane = null;
    let pointerStartHeight = 0;
    const dashboardBasePath = window.location.pathname.replace(/\\/dashboard\\/?$/, '');

    function dashboardUrl(path) {
      const normalized = path.startsWith('/') ? path : `/${path}`;
      return `${dashboardBasePath}${normalized}`;
    }

    function numericValue(value) {
      const parsed = parseFloat(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }

    function getMinPaneHeight() {
      return minHeightQuery.matches ? MIN_PANE_HEIGHT_MOBILE : MIN_PANE_HEIGHT_DESKTOP;
    }

    function computePaneShare() {
      if (panes.length === 0) {
        return null;
      }
      const wrapper = document.querySelector('.panes-wrapper');
      const main = document.querySelector('main');
      const header = document.querySelector('header');
      if (!wrapper || !main) {
        return null;
      }
      const viewportHeight = window.innerHeight;
      const wrapperStyles = window.getComputedStyle(wrapper);
      const mainStyles = window.getComputedStyle(main);
      const headerStyles = header ? window.getComputedStyle(header) : null;
      const headerRect = header ? header.getBoundingClientRect() : null;
      const gap = numericValue(wrapperStyles.rowGap || wrapperStyles.gap || '0');
      const paddingTop = numericValue(mainStyles.paddingTop || '0');
      const marginTop = numericValue(mainStyles.marginTop || '0');
      const paddingBottom = numericValue(mainStyles.paddingBottom || '0');
      const headerHeight = headerRect ? headerRect.height : 0;
      const headerMargin = headerStyles ? numericValue(headerStyles.marginBottom || '0') : 0;
      const available =
        viewportHeight - headerHeight - headerMargin - marginTop - paddingTop - paddingBottom;
      const minPaneHeight = getMinPaneHeight();
      if (available <= 0) {
        return minPaneHeight;
      }
      const gapTotal = gap * Math.max(panes.length - 1, 0);
      // With box-sizing: border-box, height includes padding and border,
      // so we only subtract gaps, not pane extras
      const usable = available - gapTotal;
      if (usable <= 0) {
        return minPaneHeight;
      }
      const share = Math.floor(usable / panes.length);
      if (share <= 0) {
        return minPaneHeight;
      }
      return share;
    }

    function applyPaneLayout(options = {}) {
      const { force = false, resetUserResized = false } = options;
      const share = computePaneShare();
      if (!share) {
        return;
      }
      panes.forEach((pane) => {
        if (force || pane.dataset.userResized !== 'true') {
          pane.style.height = `${share}px`;
        }
        if (resetUserResized) {
          delete pane.dataset.userResized;
          pane.removeAttribute('data-user-resized');
        }
      });
    }

    function finalizeResize() {
      if (!activeResizePane) {
        return;
      }
      const endHeight = activeResizePane.getBoundingClientRect().height;
      if (Math.abs(endHeight - pointerStartHeight) > 1) {
        activeResizePane.dataset.userResized = 'true';
      }
      activeResizePane = null;
      pointerStartHeight = 0;
    }

    requestAnimationFrame(() => {
      applyPaneLayout({ force: true, resetUserResized: true });
    });

    window.addEventListener('load', () => {
      applyPaneLayout();
    });

    window.addEventListener('resize', () => {
      applyPaneLayout();
    });

    if (typeof minHeightQuery.addEventListener === 'function') {
      minHeightQuery.addEventListener('change', () => {
        applyPaneLayout({ force: true });
      });
    } else if (typeof minHeightQuery.addListener === 'function') {
      minHeightQuery.addListener(() => {
        applyPaneLayout({ force: true });
      });
    }

    function handleResizeStart(event) {
      const isPointer = event.type === 'pointerdown';
      const isMouse = event.type === 'mousedown';
      if (isPointer && event.button !== 0) {
        return;
      }
      if (isMouse && event.button !== 0) {
        return;
      }
      activeResizePane = event.currentTarget;
      pointerStartHeight = activeResizePane.getBoundingClientRect().height;
    }

    panes.forEach((pane) => {
      if (window.PointerEvent) {
        pane.addEventListener('pointerdown', handleResizeStart);
      } else {
        pane.addEventListener('mousedown', handleResizeStart);
      }
    });

    if (window.PointerEvent) {
      window.addEventListener('pointerup', finalizeResize);
      window.addEventListener('pointercancel', finalizeResize);
    } else {
      window.addEventListener('mouseup', finalizeResize);
    }
    window.addEventListener('blur', finalizeResize);

    // Custom resize handle support for touch devices.
    // This programmatic resize logic is separate from the native CSS resize tracking
    // above (handleResizeStart/finalizeResize) because mobile browsers do not support
    // touch input on CSS resize handles. The custom handles intercept touch/pointer
    // events and manually set the pane height.
    const resizeHandles = Array.from(document.querySelectorAll('.resize-handle'));
    let touchResizePane = null;
    let touchResizeHandle = null;  // Store handle reference for pointer capture release
    let touchStartY = 0;
    let touchStartHeight = 0;
    let activePointerId = null;  // Track pointer ID to avoid multi-touch jumps

    function handleTouchResizeStart(event) {
      const handle = event.currentTarget;
      const pane = handle.closest('.pane');
      if (!pane) {
        return;
      }
      // For Pointer Events, only respond to the primary pointer (first finger)
      if (event.pointerId !== undefined && !event.isPrimary) {
        return;
      }
      const clientY = event.touches ? event.touches[0].clientY : event.clientY;
      touchResizePane = pane;
      touchResizeHandle = handle;
      touchStartY = clientY;
      touchStartHeight = pane.getBoundingClientRect().height;
      // Store pointer ID to ignore events from other fingers
      activePointerId = event.pointerId !== undefined ? event.pointerId : null;
      // Capture pointer to ensure we receive all events even if finger moves
      // outside the handle or browser tries to interpret as scroll gesture
      if (event.pointerId !== undefined && handle.setPointerCapture) {
        handle.setPointerCapture(event.pointerId);
      }
      // Prevent default and stop propagation to avoid scroll/gesture interference
      if (event.cancelable) {
        event.preventDefault();
      }
      event.stopPropagation();
    }

    function handleTouchResizeMove(event) {
      if (!touchResizePane) {
        return;
      }
      // Ignore events from other pointers (prevents multi-touch jumps)
      if (activePointerId !== null && event.pointerId !== activePointerId) {
        return;
      }
      const clientY = event.touches ? event.touches[0].clientY : event.clientY;
      const deltaY = clientY - touchStartY;
      // Use the smaller of the standard minimum or the initial height, so panes
      // that start smaller than the minimum (due to limited viewport) can still
      // be resized down to their original size without jumping.
      const minPaneHeight = Math.min(getMinPaneHeight(), touchStartHeight);
      // Clamp to max 80% of viewport to prevent a single pane from consuming the screen
      const maxPaneHeight = Math.floor(window.innerHeight * 0.8);
      const newHeight = Math.min(maxPaneHeight, Math.max(minPaneHeight, touchStartHeight + deltaY));
      touchResizePane.style.height = `${newHeight}px`;
      if (event.cancelable) {
        event.preventDefault();
      }
      event.stopPropagation();
    }

    function handleTouchResizeEnd(event) {
      if (!touchResizePane) {
        return;
      }
      // Only respond to the pointer that started the resize
      if (activePointerId !== null && event.pointerId !== activePointerId) {
        return;
      }
      const endHeight = touchResizePane.getBoundingClientRect().height;
      if (Math.abs(endHeight - touchStartHeight) > 1) {
        touchResizePane.dataset.userResized = 'true';
      }
      // Release pointer capture using stored handle reference (not event.target)
      if (activePointerId !== null && touchResizeHandle && touchResizeHandle.releasePointerCapture) {
        try {
          touchResizeHandle.releasePointerCapture(activePointerId);
        } catch (e) {
          // Ignore if capture was already released
        }
      }
      touchResizePane = null;
      touchResizeHandle = null;
      touchStartY = 0;
      touchStartHeight = 0;
      activePointerId = null;
    }

    resizeHandles.forEach((handle) => {
      if (window.PointerEvent) {
        handle.addEventListener('pointerdown', handleTouchResizeStart);
        // Add move/up/cancel listeners on handle for when pointer capture is active
        // (Firefox routes captured events directly to the element, not through window)
        handle.addEventListener('pointermove', handleTouchResizeMove);
        handle.addEventListener('pointerup', handleTouchResizeEnd);
        handle.addEventListener('pointercancel', handleTouchResizeEnd);
        handle.addEventListener('lostpointercapture', handleTouchResizeEnd);
      } else {
        handle.addEventListener('touchstart', handleTouchResizeStart, { passive: false });
        handle.addEventListener('mousedown', handleTouchResizeStart);
      }
    });

    if (window.PointerEvent) {
      // Also listen on window as fallback for when pointer capture is not used
      window.addEventListener('pointermove', handleTouchResizeMove);
      window.addEventListener('pointerup', handleTouchResizeEnd);
      window.addEventListener('pointercancel', handleTouchResizeEnd);
    } else {
      window.addEventListener('touchmove', handleTouchResizeMove, { passive: false });
      window.addEventListener('touchend', handleTouchResizeEnd);
      window.addEventListener('touchcancel', handleTouchResizeEnd);
      window.addEventListener('mousemove', handleTouchResizeMove);
      window.addEventListener('mouseup', handleTouchResizeEnd);
    }

    const streams = {
      stdout: document.getElementById('stdout-log'),
      stderr: document.getElementById('stderr-log'),
      prompt: document.getElementById('prompt-log'),
      system: document.getElementById('system-log')
    };

    // Get the scrollable container for a log element (.pane-content or fallback)
    function getScrollContainer(logElement) {
      const paneContent = logElement.closest('.pane-content');
      return paneContent || logElement.closest('.pane') || logElement;
    }

    function isAtBottom(scrollContainer) {
      const threshold = 30;
      return scrollContainer.scrollHeight - scrollContainer.clientHeight - scrollContainer.scrollTop <= threshold;
    }

    // Track containers currently being scrolled programmatically so the
    // scroll listener does not misinterpret auto-follow scrolls as user
    // scroll-away (fixes auto-follow on mobile browsers).
    const programmaticScrolling = new WeakSet();

    Object.values(streams).forEach((logElement) => {
      if (!logElement) {
        return;
      }
      const scrollContainer = getScrollContainer(logElement);
      logElement.dataset.autoScroll = 'true';
      scrollContainer.addEventListener('scroll', () => {
        if (programmaticScrolling.has(scrollContainer)) return;
        logElement.dataset.autoScroll = isAtBottom(scrollContainer) ? 'true' : 'false';
      });
    });

    let followEnabled = true;

    const followButton = document.getElementById('follow-toggle');
    function updateFollowButton() {
      if (!followButton) {
        return;
      }
      followButton.textContent = followEnabled ? 'Follow: On' : 'Follow: Off';
      followButton.classList.toggle('off', !followEnabled);
    }

    if (followButton) {
      updateFollowButton();
      followButton.addEventListener('click', () => {
        followEnabled = !followEnabled;
        updateFollowButton();
        if (followEnabled) {
          Object.values(streams).forEach((logElement) => {
            if (!logElement) {
              return;
            }
            const scrollContainer = getScrollContainer(logElement);
            logElement.dataset.autoScroll = isAtBottom(scrollContainer) ? 'true' : 'false';
          });
        }
      });
    }

    const clearButton = document.getElementById('clear-button');
    if (clearButton) {
      clearButton.addEventListener('click', () => {
        Object.values(streams).forEach((logElement) => {
          if (!logElement) {
            return;
          }
          const scrollContainer = getScrollContainer(logElement);
          logElement.innerHTML = '';
          logElement.dataset.autoScroll = 'true';
          programmaticScrolling.add(scrollContainer);
          scrollContainer.scrollTop = scrollContainer.scrollHeight;
          requestAnimationFrame(() => {
            programmaticScrolling.delete(scrollContainer);
          });
        });
        panes.forEach((pane) => {
          pane.style.removeProperty('height');
          delete pane.dataset.userResized;
          pane.removeAttribute('data-user-resized');
        });
        applyPaneLayout({ force: true, resetUserResized: true });
      });
    }

    function appendEntry(target, payload) {
      if (!target) {
        return;
      }
      const shouldScroll = followEnabled && target.dataset.autoScroll !== 'false';
      const row = document.createElement('div');
      row.className = 'log-entry';
      if (payload.timestamp) {
        const timestamp = document.createElement('span');
        timestamp.className = 'timestamp';
        timestamp.textContent = payload.timestamp;
        row.appendChild(timestamp);
      }
      if (payload.agent) {
        const agentTag = document.createElement('span');
        agentTag.className = 'agent-tag';
        const label = payload.task ? `${payload.agent} · ${payload.task}` : payload.agent;
        agentTag.textContent = label;
        row.appendChild(agentTag);
      }
      const message = document.createElement('span');
      message.textContent = payload.line || '';
      row.appendChild(message);
      target.appendChild(row);
      if (shouldScroll) {
        requestAnimationFrame(() => {
          const scrollContainer = getScrollContainer(target);
          programmaticScrolling.add(scrollContainer);
          scrollContainer.scrollTop = scrollContainer.scrollHeight;
          requestAnimationFrame(() => {
            programmaticScrolling.delete(scrollContainer);
          });
        });
      }
    }

    async function killAgent(entry, button) {
      const agent = entry.agent || 'unknown';
      const taskLabel = entry.task ? ` (${entry.task})` : '';
      const message = `Kill agent ${agent}${taskLabel}?`;
      if (!window.confirm(message)) {
        return;
      }
      button.disabled = true;
      try {
        const response = await fetch(
          dashboardUrl(`/dashboard/kill/${encodeURIComponent(entry.event_id)}`),
          { method: 'POST' }
        );
        if (!response.ok) {
          let detail = `Kill request failed (${response.status})`;
          try {
            const payload = await response.json();
            if (payload && payload.detail) {
              detail = payload.detail;
            }
          } catch (err) {
            /* ignore json errors */
          }
          throw new Error(detail);
        }
      } catch (err) {
        console.error('Kill request failed', err);
        window.alert(err.message || 'Kill request failed');
        button.disabled = false;
      }
    }

    function updateActiveAgents(activeAgents) {
      const display = document.getElementById('active-agents');
      if (!display) {
        return;
      }
      if (!activeAgents || activeAgents.length === 0) {
        display.textContent = 'None';
        if (activeIndicator) {
          activeIndicator.hidden = true;
        }
        return;
      }
      display.innerHTML = '';
      activeAgents.forEach((entry) => {
        const wrapper = document.createElement('div');
        wrapper.className = 'active-agent';

        const agentName = document.createElement('span');
        agentName.className = 'active-agent-name';
        const agent = entry.agent || 'unknown';
        const task = entry.task ? `(${entry.task})` : '';
        agentName.textContent = `${agent} ${task}`.trim();
        wrapper.appendChild(agentName);

        if (entry.event_id) {
          const killButton = document.createElement('button');
          killButton.type = 'button';
          killButton.className = 'kill-button';
          killButton.innerHTML = '<span class="icon">☠</span> Kill';
          killButton.addEventListener('click', () => killAgent(entry, killButton));
          wrapper.appendChild(killButton);
        }

        display.appendChild(wrapper);
      });
      if (activeIndicator) {
        activeIndicator.hidden = false;
      }
    }

    function setConnectionStatus(text, ok) {
      const el = document.getElementById('connection-status');
      el.textContent = text;
      el.style.background = ok ? '#22c55e' : '#f59e0b';
      el.style.color = ok ? '#0f172a' : '#0f172a';
    }

    function connect() {
      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const wsPath = dashboardUrl('/dashboard/ws');
      const ws = new WebSocket(`${protocol}://${window.location.host}${wsPath}`);

      ws.onopen = () => {
        setConnectionStatus('Live', true);
      };

      ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload.type === 'stream' && streams[payload.stream]) {
            appendEntry(streams[payload.stream], payload);
          } else if (payload.type === 'agent_status') {
            updateActiveAgents(payload.active_agents);
          }
        } catch (err) {
          console.error('Failed to process message', err);
        }
      };

      ws.onclose = () => {
        setConnectionStatus('Reconnecting…', false);
        setTimeout(connect, 2000);
      };

      ws.onerror = () => {
        setConnectionStatus('Error', false);
      };
    }

    connect();
  </script>
</body>
</html>
"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    if not settings.live_dashboard_enabled:
        raise HTTPException(status_code=404, detail="Live dashboard disabled")
    return HTMLResponse(DASHBOARD_HTML)


@router.post("/dashboard/kill/{event_id}")
async def dashboard_kill_agent(event_id: str) -> dict[str, Any]:
    result = await kill_agents_event(event_id)
    if not result["action_taken"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No running agents for event")

    killed_agents = result["killed_agents"]
    if killed_agents:
        agent_list = ", ".join(
            f"{entry['agent']} ({entry['task']})" if entry.get("task") else entry.get("agent", "unknown")
            for entry in killed_agents
        )
    else:
        agent_list = "no running subprocess"

    dashboard_manager.publish_system(
        f"Kill switch invoked for {event_id}: {agent_list}",
        "WARNING",
        "dashboard.kill",
    )

    return {
        "status": "ok",
        **result,
    }


@router.websocket("/dashboard/ws")
async def dashboard_socket(websocket: WebSocket) -> None:
    if not settings.live_dashboard_enabled:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    queue = await dashboard_manager.subscribe()
    try:
        while True:
            message = await queue.get()
            await websocket.send_text(json.dumps(message))
    except WebSocketDisconnect:
        pass
    finally:
        dashboard_manager.unsubscribe(queue)
