// popup.js — drives the browser-action popup.
//
// Flow:
//   1. Load config; if missing, show the "configure me" panel.
//   2. Read the active tab URL, validate it as an e(x)hentai gallery.
//   3. On "Download", POST /api/download, then poll GET /api/job/{id} every ~2s
//      and render the job state until it reaches a terminal state.

import {
  api,
  loadConfig,
  parseGalleryUrl,
  startDownload,
  getJob,
  TERMINAL_STATES,
} from "./common.js";

// ---- element handles --------------------------------------------------------
const els = {
  needsConfig: document.getElementById("needs-config"),
  main: document.getElementById("main"),
  gotoOptions: document.getElementById("goto-options"),
  openOptions: document.getElementById("open-options"),
  tabUrl: document.getElementById("tab-url"),
  validity: document.getElementById("validity"),
  download: document.getElementById("download"),
  job: document.getElementById("job"),
  jobId: document.getElementById("job-id"),
  jobState: document.getElementById("job-state"),
  jobProgress: document.getElementById("job-progress"),
  jobGp: document.getElementById("job-gp"),
  jobError: document.getElementById("job-error"),
};

const POLL_INTERVAL_MS = 2000;

// Module-scoped state for the in-flight job poll, so we can cancel cleanly.
let pollTimer = null;
let config = { serverUrl: "", apiKey: "" };
let activeUrl = "";

// ---- wiring -----------------------------------------------------------------
els.openOptions.addEventListener("click", openOptionsPage);
els.gotoOptions.addEventListener("click", openOptionsPage);
els.download.addEventListener("click", onDownloadClick);

init().catch((err) => {
  // Last-resort surface for unexpected init errors.
  showValidity(`Initialization error: ${err.message}`, false);
});

// ---- init -------------------------------------------------------------------
async function init() {
  config = await loadConfig();

  if (!config.serverUrl || !config.apiKey) {
    els.needsConfig.classList.remove("hidden");
    els.main.classList.add("hidden");
    return;
  }
  els.needsConfig.classList.add("hidden");
  els.main.classList.remove("hidden");

  const tab = await getActiveTab();
  activeUrl = tab?.url ?? "";
  els.tabUrl.textContent = activeUrl || "(no URL)";
  els.tabUrl.title = activeUrl;

  const parsed = parseGalleryUrl(activeUrl);
  if (parsed.ok) {
    showValidity(`Gallery ${parsed.gid} on ${parsed.domain}`, true);
    els.download.disabled = false;
  } else {
    showValidity(parsed.reason, false);
    els.download.disabled = true;
  }
}

// ---- actions ----------------------------------------------------------------
async function onDownloadClick() {
  // Re-validate at click time in case the tab changed.
  const parsed = parseGalleryUrl(activeUrl);
  if (!parsed.ok) {
    showValidity(parsed.reason, false);
    return;
  }

  stopPolling();
  els.download.disabled = true;
  els.download.textContent = "Sending…";
  hideError();
  resetJobView();
  els.job.classList.remove("hidden");
  setState("queued");

  let job;
  try {
    job = await startDownload({
      serverUrl: config.serverUrl,
      apiKey: config.apiKey,
      url: activeUrl,
      catid: null,
    });
  } catch (err) {
    failJob(err.message);
    return;
  }

  els.download.textContent = "Download to MangaCouch";

  if (job.id === undefined || job.id === null) {
    failJob("Server did not return a job id.");
    return;
  }

  renderJob(job);

  // If the very first response is already terminal, don't bother polling.
  if (TERMINAL_STATES.has(job.state)) {
    finishJob(job);
    return;
  }
  startPolling(job.id);
}

// ---- polling ----------------------------------------------------------------
function startPolling(id) {
  pollTimer = setInterval(async () => {
    let job;
    try {
      job = await getJob({
        serverUrl: config.serverUrl,
        apiKey: config.apiKey,
        id,
      });
    } catch (err) {
      // Transient network errors shouldn't kill the poll loop; surface and
      // keep trying. A persistent failure stays visible to the user.
      showError(`Polling error: ${err.message}`);
      return;
    }
    hideError();
    renderJob(job);
    if (TERMINAL_STATES.has(job.state)) {
      finishJob(job);
    }
  }, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function finishJob(job) {
  stopPolling();
  els.download.disabled = false;
  if (job.state === "failed") {
    showError(job.error || "Download failed.");
  }
}

function failJob(message) {
  stopPolling();
  setState("failed");
  showError(message);
  els.download.disabled = false;
  els.download.textContent = "Download to MangaCouch";
}

// ---- rendering --------------------------------------------------------------
function renderJob(job) {
  els.jobId.textContent = job.id ?? "—";
  setState(job.state || "queued");
  els.jobProgress.textContent = formatProgress(job.progress);
  els.jobGp.textContent = formatGp(job.gp_cost);
  if (job.error) {
    showError(job.error);
  }
}

function setState(state) {
  els.jobState.textContent = state;
  // Reset the badge to its base class plus the state-specific modifier so the
  // color reflects the current state.
  els.jobState.className = `badge ${state}`;
}

function formatProgress(progress) {
  if (progress === undefined || progress === null) return "—";
  // Accept either a 0..1 fraction or a 0..100 percentage from the server.
  const n = Number(progress);
  if (Number.isNaN(n)) return String(progress);
  const pct = n <= 1 ? n * 100 : n;
  return `${Math.round(pct)}%`;
}

function formatGp(gp) {
  if (gp === undefined || gp === null) return "—";
  const n = Number(gp);
  return Number.isNaN(n) ? String(gp) : `${n.toLocaleString()} GP`;
}

function resetJobView() {
  els.jobId.textContent = "";
  els.jobProgress.textContent = "—";
  els.jobGp.textContent = "—";
  hideError();
}

// ---- small UI helpers -------------------------------------------------------
function showValidity(text, ok) {
  els.validity.textContent = text;
  els.validity.classList.toggle("ok", ok);
  els.validity.classList.toggle("bad", !ok);
}

function showError(text) {
  els.jobError.textContent = text;
  els.jobError.classList.remove("hidden");
}

function hideError() {
  els.jobError.textContent = "";
  els.jobError.classList.add("hidden");
}

function openOptionsPage() {
  if (api.runtime.openOptionsPage) {
    api.runtime.openOptionsPage();
  } else {
    // Fallback for engines without openOptionsPage.
    window.open(api.runtime.getURL("options.html"));
  }
}

// ---- tab access -------------------------------------------------------------
async function getActiveTab() {
  const tabs = await api.tabs.query({ active: true, currentWindow: true });
  return tabs && tabs[0];
}

// Clean up the poll timer if the popup is closed mid-download.
window.addEventListener("unload", stopPolling);
