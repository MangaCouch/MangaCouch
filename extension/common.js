export const api = globalThis.browser ?? globalThis.chrome;

// A problem the user fixes in Settings (address, key or origin permission),
// as opposed to a network or library failure.
export class ConfigError extends Error {}

export function t(key, substitutions) {
  return api.i18n?.getMessage(key, substitutions) || key;
}

// Fill every element with data-i18n / data-i18n-aria-label from the locale.
export function localize(root) {
  if (api.i18n?.getUILanguage)
    root.documentElement.lang = api.i18n.getUILanguage();
  for (const node of root.querySelectorAll("[data-i18n]"))
    node.textContent = t(node.dataset.i18n);
  for (const node of root.querySelectorAll("[data-i18n-aria-label]"))
    node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel));
}

export function serverOrigin(value) {
  let url;
  try {
    url = new URL(String(value ?? "").trim());
  } catch {
    throw new ConfigError(t("errorOrigin"));
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !["", "/"].includes(url.pathname)
  )
    throw new ConfigError(t("errorOrigin"));
  return url.origin;
}

export function galleryURL(value) {
  let url;
  try {
    url = new URL(String(value ?? ""));
  } catch {
    throw new Error(t("errorGallery"));
  }
  const path =
    url.hostname === "nhentai.net"
      ? /^\/g\/[1-9]\d*\/?$/
      : ["e-hentai.org", "exhentai.org"].includes(url.hostname)
        ? /^\/g\/[1-9]\d*\/[a-f0-9]{1,64}\/?$/i
        : null;
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.port ||
    !path?.test(url.pathname)
  )
    throw new Error(t("errorGallery"));
  return url.origin + url.pathname.replace(/\/?$/, "/");
}

export async function loadConfig() {
  return api.storage.local.get(["server", "key"]);
}

export async function enqueue(config, url, entry = "find_eh") {
  if (!config?.server || !config?.key)
    throw new ConfigError(t("errorConfigure"));
  const origin = serverOrigin(config.server);
  if (!(await api.permissions.contains({ origins: [origin + "/*"] })))
    throw new ConfigError(t("errorPermissionMissing"));
  const body = JSON.stringify({ url: galleryURL(url), entry });
  let response;
  try {
    response = await fetch(origin + "/api/downloads", {
      method: "POST",
      credentials: "omit",
      redirect: "error",
      signal: AbortSignal.timeout(30000),
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + config.key,
      },
      body,
    });
  } catch (error) {
    throw new Error(t("errorNetwork", [error.message]));
  }
  if (response.status === 401 || response.status === 403)
    throw new ConfigError(t("errorKeyRejected"));
  if (!response.ok)
    throw new Error(t("errorLibrary", [String(response.status)]));
  const job = await response.json().catch(() => null);
  if (!job?.id || !job?.state) throw new Error(t("errorResponse"));
  return job;
}

export async function openQueue(config) {
  if (!config?.server) throw new ConfigError(t("errorConfigure"));
  return api.tabs.create({ url: serverOrigin(config.server) + "/#downloads" });
}

// The last context-menu failure, kept for the popup; session storage needs no
// extra permission and is cleared when the browser closes.
const errorStore = () => api.storage.session ?? api.storage.local;

export async function rememberError(message) {
  await errorStore().set({ lastError: message });
  await api.action.setBadgeText({ text: "!" });
  await api.action.setTitle({ title: `${t("extName")}: ${message}` });
}

export async function clearError() {
  await errorStore().remove("lastError");
  await api.action.setBadgeText({ text: "" });
  await api.action.setTitle({ title: t("extName") });
}

export async function lastError() {
  return (await errorStore().get("lastError")).lastError;
}
