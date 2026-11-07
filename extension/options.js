import { api, loadConfig, localize, serverOrigin, t } from "./common.js";
const el = (id) => document.getElementById(id);
localize(document);
loadConfig()
  .then((config) => {
    el("server").value = config.server || "";
    el("key").value = config.key || "";
  })
  .catch(() => {
    el("status").textContent = t("errorLoadSettings");
  });
el("form").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const server = serverOrigin(el("server").value),
      key = el("key").value.trim();
    if (!key || /[\r\n]/.test(key)) throw new Error(t("errorKeyRequired"));
    if (!(await api.permissions.request({ origins: [server + "/*"] })))
      throw new Error(t("errorPermissionDenied"));
    const previous = await loadConfig();
    if (api.storage.local.setAccessLevel)
      await api.storage.local.setAccessLevel({
        accessLevel: "TRUSTED_CONTEXTS",
      });
    await api.storage.local.set({ server, key });
    if (previous.server && previous.server !== server) {
      try {
        if (
          !(await api.permissions.remove({ origins: [previous.server + "/*"] }))
        )
          throw new Error("Permission was not removed");
      } catch {
        el("status").textContent = t("statusSavedPermissionKept");
        return;
      }
    }
    el("status").textContent = t("statusSaved");
  } catch (error) {
    el("status").textContent = error.message;
  }
};
