import {
  api,
  ConfigError,
  clearError,
  enqueue,
  loadConfig,
  openQueue,
  rememberError,
  t,
} from "./common.js";
api.runtime.onInstalled.addListener(() => {
  api.contextMenus.removeAll(() =>
    api.contextMenus.create({
      id: "queue",
      title: t("contextMenuTitle"),
      contexts: ["link"],
      targetUrlPatterns: [
        "https://e-hentai.org/g/*",
        "https://exhentai.org/g/*",
        "https://nhentai.net/g/*",
      ],
    }),
  );
});
api.contextMenus.onClicked.addListener(async (info) => {
  if (info.menuItemId !== "queue") return;
  try {
    const config = await loadConfig();
    await enqueue(config, info.linkUrl, "find_eh");
    await clearError();
    await openQueue(config);
  } catch (error) {
    // Settings only helps with configuration problems; other failures are
    // shown by the badge, the toolbar title and the popup.
    await rememberError(error.message);
    if (error instanceof ConfigError) await api.runtime.openOptionsPage();
  }
});
