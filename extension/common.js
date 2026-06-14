// common.js — shared helpers for the MangaCouch Downloader extension.
//
// This module is imported by both the popup (popup.js) and the service worker
// (background.js). It contains the cross-browser API shim, configuration
// storage, e(x)hentai URL validation, and the small client for the two server
// endpoints the extension depends on. Keeping the API contract in one place
// makes it impossible for the popup and the context menu to drift apart.

// ---------------------------------------------------------------------------
// Cross-browser shim.
// Firefox exposes the promise-based `browser.*` namespace; Chrome exposes
// `chrome.*`. Chrome's MV3 APIs also return promises, so `browser ?? chrome`
// gives us one promise-friendly namespace on both engines.
// ---------------------------------------------------------------------------
export const api = globalThis.browser ?? globalThis.chrome;

// ---------------------------------------------------------------------------
// e(x)hentai gallery URL validation.
//
// A gallery URL looks like:
//   https://e-hentai.org/g/<gid>/<token>/
//   https://exhentai.org/g/<gid>/<token>/
// where <gid> is numeric and <token> is a 10-char lowercase-hex access token.
// A trailing slash is canonical but we tolerate its absence and any query/hash.
// ---------------------------------------------------------------------------
const GALLERY_RE =
  /^https:\/\/e(?:-|x)hentai\.org\/g\/(\d+)\/([0-9a-f]+)\/?(?:[?#].*)?$/i;

/**
 * Validate a URL string and pull out the gallery identifiers.
 * @param {string} url
 * @returns {{ ok: true, gid: string, token: string, domain: string }
 *          | { ok: false, reason: string }}
 */
export function parseGalleryUrl(url) {
  if (!url || typeof url !== "string") {
    return { ok: false, reason: "No URL on the active tab." };
  }
  const match = url.match(GALLERY_RE);
  if (!match) {
    return {
      ok: false,
      reason:
        "This tab is not an e(x)hentai gallery page " +
        "(expected https://e-hentai.org/g/<gid>/<token>/).",
    };
  }
  let domain;
  try {
    domain = new URL(url).hostname;
  } catch {
    return { ok: false, reason: "Malformed URL." };
  }
  return { ok: true, gid: match[1], token: match[2], domain };
}

/** True when `url` is a valid e(x)hentai gallery page. */
export function isGalleryUrl(url) {
  return parseGalleryUrl(url).ok;
}

// ---------------------------------------------------------------------------
// Configuration storage.
//
// We prefer chrome.storage.sync so the server URL + key roam with the user's
// profile, and transparently fall back to chrome.storage.local when sync is
// unavailable or fails (e.g. Firefox without sync configured, or an over-quota
// sync store).
// ---------------------------------------------------------------------------
const CONFIG_KEYS = ["serverUrl", "apiKey"];

function syncArea() {
  return api?.storage?.sync ?? null;
}
function localArea() {
  return api?.storage?.local ?? null;
}

/**
 * Load the saved configuration.
 * @returns {Promise<{ serverUrl: string, apiKey: string }>}
 */
export async function loadConfig() {
  // Try sync first, fall back to local. We read both areas and merge so a value
  // written to either is found, with sync taking precedence.
  const out = { serverUrl: "", apiKey: "" };
  for (const area of [localArea(), syncArea()]) {
    if (!area) continue;
    try {
      const got = await area.get(CONFIG_KEYS);
      for (const k of CONFIG_KEYS) {
        if (typeof got[k] === "string" && got[k] !== "") out[k] = got[k];
      }
    } catch {
      // Ignore a failing area; the other one may still work.
    }
  }
  return out;
}

/**
 * Persist the configuration, preferring sync with a local fallback.
 * @param {{ serverUrl: string, apiKey: string }} config
 */
export async function saveConfig(config) {
  const payload = { serverUrl: config.serverUrl, apiKey: config.apiKey };
  const sync = syncArea();
  if (sync) {
    try {
      await sync.set(payload);
      return;
    } catch {
      // fall through to local
    }
  }
  const local = localArea();
  if (local) {
    await local.set(payload);
    return;
  }
  throw new Error("No storage area available.");
}

/**
 * Normalize a server URL: trim, drop any trailing slash so we can safely append
 * "/api/...". Does not validate reachability.
 * @param {string} serverUrl
 */
export function normalizeServer(serverUrl) {
  return (serverUrl || "").trim().replace(/\/+$/, "");
}

// ---------------------------------------------------------------------------
// Server API client.
//
// Contract (must match the MangaCouch server exactly):
//   POST {server}/api/download
//     headers: Authorization: Bearer <base64(ownerApiKey)>
//              Content-Type: application/json
//     body:    { "url": "<gallery url>", "catid": <number|null> }
//     returns: { "id": <jobId>, "state": "queued", ... }
//
//   GET {server}/api/job/{id}
//     headers: Authorization: Bearer <base64(ownerApiKey)>
//     returns: { id, url, gid, token, state, priority, progress,
//                gp_cost, error }
//     state ∈ queued | running | preparing | done | failed
// ---------------------------------------------------------------------------

/**
 * Build the Authorization header value. The user pastes the *raw* owner API
 * key; the wire format is `Bearer ` + base64 of that key.
 * @param {string} apiKey
 */
export function authHeader(apiKey) {
  return "Bearer " + btoa(apiKey);
}

/** Terminal job states — polling stops once one is reached. */
export const TERMINAL_STATES = new Set(["done", "failed"]);

/**
 * Trigger an Archive Download on the server.
 * @param {object} args
 * @param {string} args.serverUrl  raw configured server URL
 * @param {string} args.apiKey     raw owner API key
 * @param {string} args.url        the gallery URL to download
 * @param {number|null} [args.catid] optional category id
 * @returns {Promise<object>} the created job (at least `{ id, state }`)
 */
export async function startDownload({ serverUrl, apiKey, url, catid = null }) {
  const base = normalizeServer(serverUrl);
  const resp = await fetch(`${base}/api/download`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: authHeader(apiKey),
    },
    body: JSON.stringify({ url, catid }),
  });
  return readJsonOrThrow(resp, "download");
}

/**
 * Fetch the current state of a job.
 * @param {object} args
 * @param {string} args.serverUrl
 * @param {string} args.apiKey
 * @param {string|number} args.id
 * @returns {Promise<object>} the job record
 */
export async function getJob({ serverUrl, apiKey, id }) {
  const base = normalizeServer(serverUrl);
  const resp = await fetch(`${base}/api/job/${encodeURIComponent(id)}`, {
    method: "GET",
    headers: { Authorization: authHeader(apiKey) },
  });
  return readJsonOrThrow(resp, "job");
}

/**
 * Parse a fetch Response as JSON, throwing a descriptive Error on a non-2xx
 * status or unparseable body. We try to surface a server-provided `error`
 * field or message when present.
 * @param {Response} resp
 * @param {string} what  short label for error messages ("download" | "job")
 */
async function readJsonOrThrow(resp, what) {
  let data = null;
  let text = "";
  try {
    text = await resp.text();
    data = text ? JSON.parse(text) : null;
  } catch {
    // leave data null; handled below
  }
  if (!resp.ok) {
    const detail =
      (data && (data.error || data.detail || data.message)) ||
      text ||
      `HTTP ${resp.status}`;
    throw new Error(`${what} request failed (${resp.status}): ${detail}`);
  }
  if (data === null) {
    throw new Error(`${what} request returned an empty or invalid response.`);
  }
  return data;
}
