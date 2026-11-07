import {
  api,
  ConfigError,
  clearError,
  enqueue,
  galleryURL,
  lastError,
  loadConfig,
  localize,
  openQueue,
  t,
} from "./common.js";
const el = (id) => document.getElementById(id);
let config, url;
localize(document);
el("settings").onclick = () => api.runtime.openOptionsPage();
el("queue").onclick = async () => {
  try {
    await openQueue(config);
  } catch (error) {
    el("status").textContent = error.message;
  }
};
el("send").onclick = async () => {
  el("send").disabled = true;
  try {
    const entry = document.querySelector('input[name="entry"]:checked').value;
    await enqueue(config, url, entry);
    el("status").textContent = t("statusAdded");
  } catch (error) {
    el("status").textContent = error.message;
  } finally {
    el("send").disabled = false;
  }
};
(async () => {
  try {
    const previous = await lastError();
    if (previous) {
      el("status").textContent = t("errorLast", [previous]);
      await clearError();
    }
    config = await loadConfig();
    if (!config.server || !config.key)
      throw new ConfigError(t("errorConfigure"));
    const [tab] = await api.tabs.query({ active: true, currentWindow: true });
    url = galleryURL(tab?.url || "");
    el("gallery").textContent = url;
    el("choice").hidden = !url.startsWith("https://nhentai.net/");
    el("send").disabled = false;
  } catch (error) {
    el("status").textContent = error.message;
  }
})();
