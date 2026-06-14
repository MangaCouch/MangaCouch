// options.js — load and save the server URL + owner API key.

import { loadConfig, saveConfig, normalizeServer } from "./common.js";

const serverInput = document.getElementById("server-url");
const keyInput = document.getElementById("api-key");
const revealCheckbox = document.getElementById("reveal-key");
const saveButton = document.getElementById("save");
const status = document.getElementById("status");

// Populate fields from stored config on load.
init().catch((err) => setStatus(`Failed to load settings: ${err.message}`, false));

async function init() {
  const config = await loadConfig();
  serverInput.value = config.serverUrl || "";
  keyInput.value = config.apiKey || "";
}

// Toggle key visibility.
revealCheckbox.addEventListener("change", () => {
  keyInput.type = revealCheckbox.checked ? "text" : "password";
});

saveButton.addEventListener("click", onSave);

// Enter in either field saves.
for (const el of [serverInput, keyInput]) {
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") onSave();
  });
}

async function onSave() {
  const serverUrl = normalizeServer(serverInput.value);
  const apiKey = keyInput.value.trim();

  // Validate non-empty.
  if (!serverUrl) {
    setStatus("Server URL is required.", false);
    serverInput.focus();
    return;
  }
  if (!apiKey) {
    setStatus("Owner API key is required.", false);
    keyInput.focus();
    return;
  }

  // Validate the server URL is a well-formed http(s) URL.
  let parsed;
  try {
    parsed = new URL(serverUrl);
  } catch {
    setStatus("Server URL is not a valid URL.", false);
    serverInput.focus();
    return;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    setStatus("Server URL must start with http:// or https://", false);
    serverInput.focus();
    return;
  }

  try {
    await saveConfig({ serverUrl, apiKey });
    // Reflect the normalized URL back into the field.
    serverInput.value = serverUrl;
    setStatus("Saved.", true);
  } catch (err) {
    setStatus(`Could not save: ${err.message}`, false);
  }
}

function setStatus(text, ok) {
  status.textContent = text;
  status.classList.toggle("ok", ok);
  status.classList.toggle("err", !ok);
}
