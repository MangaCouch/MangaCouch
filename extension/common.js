export const api = globalThis.chrome;

// A problem the user fixes in Settings (address, key or origin permission),
// as opposed to a network or library failure.
export class ConfigError extends Error {}

export function t(key, substitutions) {
  return api.i18n.getMessage(key, substitutions) || key;
}

// Fill every element with data-i18n / data-i18n-aria-label from the locale.
export function localize(root) {
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

// Gallery hosts the library accepts, as in its own link parser.
export const EH_HOSTS = [
  "e-hentai.org",
  "www.e-hentai.org",
  "g.e-hentai.org",
  "exhentai.org",
  "www.exhentai.org",
];

export function galleryURL(value) {
  let url;
  try {
    url = new URL(String(value ?? ""));
  } catch {
    throw new Error(t("errorGallery"));
  }
  const path = EH_HOSTS.includes(url.hostname)
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

async function libraryRequest(url, init) {
  for (let attempt = 0; ; attempt++) {
    let response;
    try {
      response = await fetch(url, {
        ...init,
        credentials: "omit",
        redirect: "error",
        signal: AbortSignal.timeout(15000),
      });
    } catch (error) {
      if (error.name === "TimeoutError" || error.name === "AbortError")
        throw new Error(t("errorTimeout"));
      throw new Error(t("errorNetwork", [error.message]));
    }
    if (response.ok) return response;
    const body = await response.json().catch(() => ({}));
    if (body.error === "invalid_host")
      throw new ConfigError(t("errorPublicOrigin"));
    if (response.status === 403 && body.error === "read_only")
      throw new ConfigError(t("errorKeyReadOnly"));
    if ([401, 403].includes(response.status))
      throw new ConfigError(t("errorKeyRejected"));
    const retry = response.headers.get("Retry-After");
    if (response.status === 503 && retry !== null && attempt < 2) {
      const seconds = /^\d+(?:\.\d+)?$/.test(retry)
        ? Number(retry)
        : (Date.parse(retry) - Date.now()) / 1000;
      if (Number.isFinite(seconds) && seconds >= 0 && seconds <= 60) {
        await new Promise((resolve) => setTimeout(resolve, seconds * 1000));
        continue;
      }
    }
    throw new Error(
      body.message || t("errorLibrary", [String(response.status)]),
    );
  }
}

export async function enqueue(config, url) {
  if (!config?.server || !config?.key)
    throw new ConfigError(t("errorConfigure"));
  const origin = serverOrigin(config.server);
  if (!(await api.permissions.contains({ origins: [origin + "/*"] })))
    throw new ConfigError(t("errorPermissionMissing"));
  const body = JSON.stringify({ url: galleryURL(url) });
  const response = await libraryRequest(origin + "/api/downloads", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: "Bearer " + config.key,
    },
    body,
  });
  const job = await response.json().catch(() => null);
  if (!job?.id || !job?.state) throw new Error(t("errorResponse"));
  return job;
}

// Confirms that the saved address and key reach the library and may add
// downloads. Returns the key's principal ({ kind, label, ... }) and changes
// nothing; /api/auth/me is one of the few routes a downloads key may read.
export async function checkConnection(config) {
  if (!config?.server || !config?.key)
    throw new ConfigError(t("errorConfigure"));
  const origin = serverOrigin(config.server);
  if (!(await api.permissions.contains({ origins: [origin + "/*"] })))
    throw new ConfigError(t("errorPermissionMissing"));
  const response = await libraryRequest(origin + "/api/auth/me", {
    headers: { Authorization: "Bearer " + config.key },
  });
  const principal = await response.json().catch(() => null);
  if (typeof principal?.kind !== "string") throw new Error(t("errorResponse"));
  if (principal.kind === "readonly")
    throw new ConfigError(t("errorKeyReadOnly"));
  return principal;
}

export async function openQueue(config) {
  if (!config?.server) throw new ConfigError(t("errorConfigure"));
  return api.tabs.create({ url: serverOrigin(config.server) + "/#downloads" });
}
