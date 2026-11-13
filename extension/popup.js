import {
  api,
  ConfigError,
  enqueue,
  galleryURL,
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
    const job = await enqueue(config, url);
    // The same outcomes, in the same words, as the library's Downloads page.
    el("status").textContent = t(
      job.outcome === "requeued"
        ? "statusRequeued"
        : job.outcome !== "existing"
          ? "statusAdded"
          : job.state === "done"
            ? "statusDownloaded"
            : "statusExisting",
    );
  } catch (error) {
    el("status").textContent = error.message;
  } finally {
    el("send").disabled = false;
  }
};
(async () => {
  try {
    config = await loadConfig();
    if (!config.server || !config.key)
      throw new ConfigError(t("errorConfigure"));
    const [tab] = await api.tabs.query({ active: true, currentWindow: true });
    url = galleryURL(tab?.url || "");
    el("gallery").textContent = url;
    el("send").disabled = false;
  } catch (error) {
    el("status").textContent = error.message;
  }
})();
