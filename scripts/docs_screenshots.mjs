// Re-shoots the README screenshots in docs/screenshots/{zh,en}/ from the invented sample library
// (tests/ui_browser_server.py --showcase). Run from the repository root after building the web app:
//
//   npm --prefix web run build
//   MANGACOUCH_BROWSER_CHANNEL=chrome node scripts/docs_screenshots.mjs [zh|en ...]
//
// MANGACOUCH_BROWSER_CHANNEL picks an installed browser (chrome, msedge); without it Playwright's
// own Chromium is used (`npx --prefix web playwright install chromium`). Each language gets a fresh
// server on a free port, so the shots never depend on each other or on a running library.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const { chromium } = createRequire(join(root, "web", "package.json"))(
  "playwright",
);
const PASSCODE = "browser-fixture-only";
// The 24-page book shown on the detail and reader screenshots.
const FEATURED = "山间来信";
// Ports the browser tests and other local tools use; never take them.
const DESKTOP = { width: 1440, height: 960 };
const LABELS = {
  zh: {
    search: "搜索书库",
    activity: "阅读记录",
    library: "书库",
    approve: "确认下载",
  },
  en: {
    search: "Search library",
    activity: "Activity",
    library: "Library",
    approve: "Download",
  },
};

/** Starts the showcase server and resolves with its URL and a stop function. */
async function startServer() {
  const child = spawn(
    "uv",
    [
      "run",
      "python",
      "tests/ui_browser_server.py",
      "--port",
      "0",
      "--showcase",
    ],
    { cwd: root, stdio: ["ignore", "pipe", "inherit"] },
  );
  const url = await new Promise((resolve, reject) => {
    let output = "";
    child.once("exit", (code) =>
      reject(new Error(`showcase server exited with ${code}`)),
    );
    child.stdout.on("data", (chunk) => {
      output += chunk;
      const line = output.split("\n").find((item) => item.startsWith("{"));
      if (line) resolve(JSON.parse(line).url);
    });
  });
  for (let attempt = 0; ; attempt++) {
    try {
      if ((await fetch(url)).ok) break;
    } catch (error) {
      if (attempt > 100) throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  return {
    url,
    stop: () =>
      new Promise((resolve) => {
        child.once("exit", resolve);
        child.kill("SIGINT");
      }),
  };
}

/** Background work (cover rendering, source checks) shows a count in the header; wait it out. */
async function settle(page) {
  // The header learns about running tasks from its first poll, so let that land before checking.
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(1000);
  await page
    .locator(".task-count")
    .waitFor({ state: "detached", timeout: 180_000 });
  await page.evaluate(() => document.fonts.ready);
  await page.waitForLoadState("networkidle");
  // Lazy images below the fold never load; only those on screen must have painted.
  await page.waitForFunction(() =>
    [...document.images].every((image) => {
      const box = image.getBoundingClientRect();
      const visible =
        box.bottom > 0 &&
        box.top < innerHeight &&
        box.right > 0 &&
        box.left < innerWidth;
      return !visible || image.naturalWidth > 0;
    }),
  );
  await page.waitForTimeout(300);
}

async function shoot(page, lang, name) {
  await settle(page);
  // Park the pointer on the empty left margin so no header button shows its hover state.
  if (name !== "reader") await page.mouse.move(8, 600);
  const path = join(root, "docs", "screenshots", lang, `${name}.png`);
  await page.screenshot({ path, animations: "disabled", caret: "hide" });
  console.log(`  ${lang}/${name}.png`);
}

async function go(page, hash) {
  await page.goto("/" + hash);
}

async function run(browser, lang) {
  const label = LABELS[lang];
  const server = await startServer();
  try {
    const context = await browser.newContext({
      baseURL: server.url,
      viewport: DESKTOP,
      locale: "zh-CN",
      colorScheme: "light",
    });
    const page = await context.newPage();
    await page.goto("/");
    // Sign-in is rate limited: sign in once per server and reuse the session everywhere.
    await page.getByLabel("书库口令", { exact: true }).fill(PASSCODE);
    await page.getByRole("button", { name: "打开书库" }).click();
    await page.locator(".book-card").first().waitFor();
    if (lang === "en") {
      await go(page, "#settings");
      await page.getByLabel("界面语言").click();
      await page.getByRole("option", { name: "English" }).click();
      await page.getByRole("heading", { name: "Settings" }).waitFor();
    }
    await go(page, "");
    await page.locator(".book-card").first().waitFor();

    await shoot(page, lang, "library");

    const search = page.getByRole("combobox", { name: label.search });
    await search.click();
    await search.pressSequentially("s", { delay: 50 });
    await page.getByRole("listbox").waitFor();
    await shoot(page, lang, "search");
    await search.press("Escape");
    await search.fill("");

    await page.getByRole("link", { name: label.activity }).click();
    await page.locator(".back-link", { hasText: label.library }).waitFor();
    await shoot(page, lang, "activity");

    // The featured book: found through the search box, like a reader would.
    await go(page, "");
    await search.fill(FEATURED);
    await search.press("Enter");
    await page.locator(".book-card", { hasText: FEATURED }).first().click();
    await page.locator(".book-intro").waitFor();
    const featured = new URL(page.url()).hash.split("/")[1];
    await shoot(page, lang, "detail");

    await go(page, "");
    await search.fill("");
    await search.press("Enter");
    await page.locator(".book-card").nth(1).waitFor();
    await go(page, "#downloads");
    // Both sample E-Hentai archives cost GP, so they wait for approval.
    await page
      .locator(".batch-urls")
      .fill(
        "https://e-hentai.org/g/101/abcdef/\nhttps://e-hentai.org/g/200/abcdef/",
      );
    await page.locator(".batch-urls").press("ControlOrMeta+Enter");
    await page.getByRole("button", { name: label.approve }).first().waitFor({
      timeout: 60_000,
    });
    await page.locator(".download-approval").nth(1).waitFor({
      timeout: 60_000,
    });
    // Leave and come back to drop the "added" confirmations above the list.
    await go(page, "");
    await page.locator(".book-card").first().waitFor();
    await go(page, "#downloads");
    await page.locator(".download-approval").nth(1).waitFor();
    // Bring both pending downloads into view.
    await page.evaluate(() => {
      const row = document.querySelector(".download-row");
      if (row) scrollTo(0, row.getBoundingClientRect().top + scrollY - 100);
    });
    await shoot(page, lang, "downloads");

    const state = await context.storageState();

    const dark = await browser.newContext({
      baseURL: server.url,
      viewport: DESKTOP,
      locale: "zh-CN",
      colorScheme: "dark",
      storageState: state,
    });
    const darkPage = await dark.newPage();
    await darkPage.goto("/");
    await darkPage.locator(".book-card").first().waitFor();
    await shoot(darkPage, lang, "dark");
    await dark.close();

    const mobile = await browser.newContext({
      baseURL: server.url,
      viewport: { width: 390, height: 844 },
      deviceScaleFactor: 2,
      isMobile: true,
      hasTouch: true,
      locale: "zh-CN",
      colorScheme: "light",
      storageState: state,
    });
    const mobilePage = await mobile.newPage();
    await mobilePage.goto("/");
    await mobilePage.locator(".book-card").first().waitFor();
    await shoot(mobilePage, lang, "mobile");
    await mobile.close();

    // Last: opening the reader saves reading progress.
    const reader = await browser.newContext({
      baseURL: server.url,
      viewport: { width: 1080, height: 760 },
      locale: "zh-CN",
      colorScheme: "light",
      storageState: state,
    });
    await reader.addInitScript(() => {
      localStorage.setItem("mangacouch:reading-mode", "spread");
      localStorage.setItem("mangacouch:direction", "ltr");
    });
    const readerPage = await reader.newPage();
    await readerPage.goto(`/#read/${featured}/page/5`);
    await readerPage.locator(".frame-ready").first().waitFor();
    await readerPage.mouse.move(540, 380);
    await shoot(readerPage, lang, "reader");
    await reader.close();
    await context.close();
  } finally {
    await server.stop();
  }
}

const languages = process.argv.slice(2).length
  ? process.argv.slice(2)
  : ["zh", "en"];
const browser = await chromium.launch({
  channel: process.env.MANGACOUCH_BROWSER_CHANNEL || undefined,
});
try {
  for (const lang of languages) {
    if (!LABELS[lang]) throw new Error(`Unknown language ${lang}`);
    mkdirSync(join(root, "docs", "screenshots", lang), { recursive: true });
    console.log(`Screenshots (${lang})`);
    await run(browser, lang);
  }
} finally {
  await browser.close();
}
