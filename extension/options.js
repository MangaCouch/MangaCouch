import {
  api,
  checkConnection,
  loadConfig,
  localize,
  serverOrigin,
  t,
} from "./common.js";
const el = (id) => document.getElementById(id);
localize(document);
function showConnection(state, text) {
  el("connection").hidden = false;
  el("indicator").dataset.state = state;
  el("connectionText").textContent = text;
}
// Tells whether the library answers and accepts the key. Runs after a save,
// when the page opens with a configured library, and from the Check button.
let verification = 0;
function invalidate() {
  verification++;
  el("connection").hidden = true;
  el("check").disabled = false;
}
el("server").oninput = invalidate;
el("key").oninput = invalidate;
async function verify(config) {
  const serial = ++verification;
  el("check").disabled = true;
  showConnection("busy", t("statusChecking"));
  try {
    const principal = await checkConnection(config);
    if (serial !== verification) return;
    const kind = t(`keyKind_${principal.kind}`);
    showConnection("ok", t("statusConnected", [principal.label || kind, kind]));
  } catch (error) {
    if (serial !== verification) return;
    showConnection("error", error.message);
  } finally {
    if (serial === verification) el("check").disabled = false;
  }
}
loadConfig()
  .then(async (config) => {
    el("server").value = config.server || "";
    el("key").value = config.key || "";
    if (config.server && config.key) await verify(config);
  })
  .catch(() => {
    el("status").textContent = t("errorLoadSettings");
  });
// Checks the values as typed; a new address still needs Save to grant access.
el("check").onclick = () => {
  invalidate();
  const key = el("key").value.trim();
  if (!key) return showConnection("error", t("errorKeyRequired"));
  return verify({ server: el("server").value, key });
};
el("form").onsubmit = async (event) => {
  event.preventDefault();
  invalidate();
  const serial = verification;
  try {
    const server = serverOrigin(el("server").value),
      key = el("key").value.trim();
    if (!key || /[\r\n]/.test(key)) throw new Error(t("errorKeyRequired"));
    if (!(await api.permissions.request({ origins: [server + "/*"] })))
      throw new Error(t("errorPermissionDenied"));
    const previous = await loadConfig();
    await api.storage.local.setAccessLevel({
      accessLevel: "TRUSTED_CONTEXTS",
    });
    await api.storage.local.set({ server, key });
    let status = t("statusSaved");
    if (previous.server && previous.server !== server) {
      try {
        if (
          !(await api.permissions.remove({ origins: [previous.server + "/*"] }))
        )
          throw new Error("Permission was not removed");
      } catch {
        status = t("statusSavedPermissionKept");
      }
    }
    if (serial !== verification) return;
    el("status").textContent = status;
    await verify({ server, key });
  } catch (error) {
    if (serial === verification) el("status").textContent = error.message;
  }
};
