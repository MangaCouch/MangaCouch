// background.js — MV3 service worker.
//
// Responsibilities (kept deliberately small):
//   1. Register a context-menu entry on e(x)hentai gallery pages that triggers
//      the same download flow as the popup button.
//   2. Relay a "startDownload" message (so other parts could request a download
//      without duplicating the API client).
//
// The service worker has no DOM, so it gives feedback via the toolbar action
// badge (no extra "notifications" permission needed — we stay at activeTab +
// storage + contextMenus). For full job progress, users open the popup.

import {
  api,
  loadConfig,
  isGalleryUrl,
  startDownload,
  getJob,
  TERMINAL_STATES,
} from "./common.js";

const MENU_ID = "mangacouch-download-gallery";
const POLL_INTERVAL_MS = 2000;
const MAX_POLLS = 150; // ~5 min ceiling so a stuck job doesn't poll forever.

// ---------------------------------------------------------------------------
// Context menu lifecycle.
// Created on install/startup, shown only on e(x)hentai gallery URLs via the
// documentUrlPatterns matcher.
// ---------------------------------------------------------------------------
function createMenu() {
  // Remove first to avoid "duplicate id" errors across reloads.
  api.contextMenus.removeAll(() => {
    api.contextMenus.create({
      id: MENU_ID,
      title: "Download this gallery to MangaCouch",
      contexts: ["page", "link"],
      documentUrlPatterns: [
        "https://e-hentai.org/g/*",
        "https://exhentai.org/g/*",
      ],
      // Also allow right-clicking a gallery *link* on a listing page.
      targetUrlPatterns: [
        "https://e-hentai.org/g/*",
        "https://exhentai.org/g/*",
      ],
    });
  });
}

api.runtime.onInstalled.addListener(createMenu);
api.runtime.onStartup.addListener(createMenu);

// ---------------------------------------------------------------------------
// Context menu click → download.
// Prefer the clicked link URL (linkUrl) when present, else the page URL.
// ---------------------------------------------------------------------------
api.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== MENU_ID) return;
  const url = info.linkUrl || info.pageUrl || tab?.url || "";
  runDownload(url).catch((err) => {
    console.error("MangaCouch download failed:", err);
    flashBadge("ERR", "#c62828");
  });
});

// ---------------------------------------------------------------------------
// Message relay: lets other extension surfaces ask the worker to download.
//   chrome.runtime.sendMessage({ type: "startDownload", url })
// Returns { ok, id } or { ok: false, error }.
// ---------------------------------------------------------------------------
api.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "startDownload") return false;
  runDownload(msg.url)
    .then((job) => sendResponse({ ok: true, id: job.id, state: job.state }))
    .catch((err) => sendResponse({ ok: false, error: err.message }));
  // Returning true keeps the message channel open for the async response.
  return true;
});

// ---------------------------------------------------------------------------
// The shared download routine used by both the context menu and the relay.
// ---------------------------------------------------------------------------
async function runDownload(url) {
  if (!isGalleryUrl(url)) {
    throw new Error("Not an e(x)hentai gallery URL.");
  }

  const config = await loadConfig();
  if (!config.serverUrl || !config.apiKey) {
    flashBadge("CFG", "#b26a00");
    // Open options so the user can configure the server.
    if (api.runtime.openOptionsPage) api.runtime.openOptionsPage();
    throw new Error("Server URL / API key not configured.");
  }

  flashBadge("…", "#b26a00");

  const job = await startDownload({
    serverUrl: config.serverUrl,
    apiKey: config.apiKey,
    url,
    catid: null,
  });

  if (job.id === undefined || job.id === null) {
    flashBadge("ERR", "#c62828");
    throw new Error("Server did not return a job id.");
  }

  // Poll to a terminal state in the background, updating the badge.
  if (!TERMINAL_STATES.has(job.state)) {
    pollToCompletion(config, job.id).catch((err) =>
      console.error("MangaCouch poll error:", err),
    );
  } else {
    reflectTerminal(job.state);
  }

  return job;
}

// Poll GET /api/job/{id} until terminal or the poll ceiling is reached.
async function pollToCompletion(config, id) {
  for (let i = 0; i < MAX_POLLS; i++) {
    await sleep(POLL_INTERVAL_MS);
    let job;
    try {
      job = await getJob({
        serverUrl: config.serverUrl,
        apiKey: config.apiKey,
        id,
      });
    } catch (err) {
      // Keep polling through transient errors.
      console.warn("MangaCouch poll retry:", err.message);
      continue;
    }
    if (TERMINAL_STATES.has(job.state)) {
      reflectTerminal(job.state);
      return;
    }
    // Show a short "running" hint on the badge.
    flashBadge("DL", "#b26a00", false);
  }
  // Timed out waiting; clear the badge rather than imply failure.
  clearBadge();
}

function reflectTerminal(state) {
  if (state === "done") flashBadge("OK", "#2e7d32");
  else flashBadge("ERR", "#c62828");
}

// ---------------------------------------------------------------------------
// Toolbar badge feedback helpers.
// ---------------------------------------------------------------------------
function flashBadge(text, color, autoClear = true) {
  try {
    api.action.setBadgeBackgroundColor({ color });
    api.action.setBadgeText({ text });
    if (autoClear) {
      // Clear "result" badges after a few seconds; progress badges persist.
      setTimeout(clearBadge, 5000);
    }
  } catch {
    // action may be unavailable in some contexts; ignore.
  }
}

function clearBadge() {
  try {
    api.action.setBadgeText({ text: "" });
  } catch {
    /* ignore */
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
