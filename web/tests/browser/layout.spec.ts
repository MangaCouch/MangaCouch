import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures";

let cookies:
  | Awaited<ReturnType<import("@playwright/test").BrowserContext["cookies"]>>
  | undefined;
test.beforeEach(async ({ page, context }) => {
  if (cookies) await context.addCookies(cookies);
  await page.goto("/");
  if (!cookies) {
    await page
      .getByLabel("书库口令", { exact: true })
      .fill("browser-fixture-only");
    await page.getByRole("button", { name: "打开书库" }).click();
    await expect(page.locator(".book-card").first()).toBeVisible();
    cookies = await context.cookies();
  }
  await expect(page.locator(".book-card").first()).toBeVisible();
});
async function detail(page: Page) {
  const { items: books } = await (
    await page.request.get("/api/books?limit=50")
  ).json();
  const book = books.find(
    (value: { content: { page_count: number } }) =>
      value.content.page_count === 22,
  );
  await page.goto(`/#book/${book.id}`);
  await expect(page.locator(".book-intro")).toBeVisible();
  return book.id as string;
}

test("library and compact cards reflow at content widths and expanded text spacing", async ({
  page,
}) => {
  for (const width of [320, 390, 600, 840, 1200, 1600]) {
    await page.setViewportSize({ width, height: 900 });
    await expect
      .poll(() => page.evaluate(() => document.documentElement.scrollWidth))
      .toBeLessThanOrEqual(width);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addStyleTag({
    content:
      "* { line-height: 1.5 !important; letter-spacing: .12em !important; word-spacing: .16em !important; } p { margin-bottom: 2em !important; }",
  });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth))
    .toBeLessThanOrEqual(390);
  await page.evaluate(() => {
    localStorage.setItem("mangacouch:shelf-view", "list");
    localStorage.setItem("mangacouch:language", "en");
  });
  await page.reload();
  await page.setViewportSize({ width: 320, height: 844 });
  await expect(page.locator(".compact-shelf .book-card").first()).toBeVisible();
  const escaped = await page
    .locator(".compact-shelf .book-card")
    .evaluateAll((cards) =>
      cards.flatMap((card) => {
        const edge = card.getBoundingClientRect().right;
        return Array.from(card.querySelectorAll(".pill"))
          .filter((pill) => pill.getBoundingClientRect().right > edge + 1)
          .map((pill) => pill.textContent);
      }),
    );
  expect(escaped).toEqual([]);
});

test("dialogs return keyboard focus to direct and menu triggers", async ({
  page,
}) => {
  const signOut = page.getByRole("button", { name: "退出登录", exact: true });
  await signOut.click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(signOut).toBeFocused();
  await detail(page);
  const actions = page.getByRole("button", { name: "书籍操作", exact: true });
  await actions.click();
  await page.getByRole("menuitem", { name: "编辑元数据", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(actions).toBeFocused();
});

test("rating is a single half-step control and organizing waits for a slow save", async ({
  page,
}) => {
  const id = await detail(page);
  const rating = page.getByRole("slider", { name: "我的评分", exact: true });
  const box = await rating.boundingBox();
  expect(box!.width).toBeGreaterThanOrEqual(24);
  expect(box!.height).toBeGreaterThanOrEqual(24);
  // Hold the rating save until leaving the page has had to wait for it.
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  let saving!: () => void;
  const started = new Promise<void>((resolve) => {
    saving = resolve;
  });
  await page.route("**/api/books/*/personal?*", async (route) => {
    saving();
    await held;
    await route.continue();
  });
  await rating.focus();
  await rating.press("End");
  await rating.press("ArrowLeft");
  await expect(rating).toHaveValue("4.5");
  await page.getByRole("link", { name: "MangaCouch", exact: true }).click();
  await started;
  await expect(page.locator(".book-intro")).toBeVisible();
  await expect(page.locator(".book-select")).toHaveCount(0);
  release();
  await page.locator(".book-select").first().click();
  await expect(page.locator(".shelf.selection-mode")).toBeVisible();
  const saved = await (await page.request.get(`/api/books/${id}`)).json();
  expect(saved.personal.rating).toBe(4.5);
});

test("thumbnail pagination survives small viewport changes and retains its first page across a breakpoint", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await detail(page);
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "阅读第 9 页", exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 391, height: 844 });
  await expect(
    page.getByRole("link", { name: "阅读第 9 页", exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 840, height: 900 });
  await expect(
    page.getByRole("link", { name: "阅读第 9 页", exact: true }),
  ).toBeVisible();
});

test("reader settings remain clickable in short landscape windows", async ({
  page,
}) => {
  const id = await detail(page);
  await page.goto(`/#read/${id}`);
  await page.getByRole("button", { name: "阅读设置", exact: true }).click();
  for (const height of [375, 320]) {
    await page.setViewportSize({ width: 844, height });
    await expect(page.locator(".reader-bottom-zone")).toBeHidden();
    for (const control of await page
      .locator(".reader-settings [role=combobox]")
      .all()) {
      await control.scrollIntoViewIfNeeded();
      await control.click();
      await expect(page.getByRole("listbox")).toBeVisible();
      await page.keyboard.press("Escape");
    }
  }
});

test("reader shows WebP without conversion requests", async ({ page }) => {
  const requests: string[] = [];
  await page.route("**/api/books/*/pages/*", async (route) => {
    requests.push(route.request().url());
    await route.fulfill({
      status: 200,
      contentType: "image/webp",
      body: Buffer.from(
        "UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoBAAEAAUAmJaQAA3AA/v02aAA=",
        "base64",
      ),
    });
  });
  await detail(page);
  await page.locator(".page-thumbnails a").first().click();
  const shown = page.locator(".page-image.image-ready img").first();
  await expect
    .poll(() => shown.evaluate((image: HTMLImageElement) => image.naturalWidth))
    .toBeGreaterThan(0);
  await expect(
    page.locator(".page-transition-status[role=alert], .page-error"),
  ).toHaveCount(0);
  expect(requests.length).toBeGreaterThan(0);
  for (const request of requests) {
    const url = new URL(request);
    expect(url.pathname).toMatch(/\/pages\/\d+$/);
    expect([...url.searchParams.keys()]).toEqual(["v"]);
    expect(url.searchParams.get("v")).toMatch(/^[0-9a-f]{16}$/);
  }
});

test("download titles wrap and task links locate downloads and expand cache", async ({
  page,
}) => {
  const title = "VeryLongUnbrokenGalleryTitle".repeat(25);
  const job = {
    id: "target-download",
    input_url: "https://e-hentai.org/g/100/abcdef/",
    source: { site: "ehentai", gallery_id: 100, gallery_token: "abcdef" },
    state: "needs_approval",
    revision: 1,
    received: 0,
    total: null,
    book_id: null,
    error_code: null,
    retry_at: 0,
    details: {
      selected: { metadata: { title }, fetched_at: 1 },
      quote: { original_gp: 25, resample_gp: 0, balance_gp: 0 },
    },
  };
  await page.route("**/api/tasks", (route) =>
    route.fulfill({
      json: {
        downloads: {
          running: 0,
          waiting: 1,
          completed: 0,
          total: 26,
          items: [{ ...job, title }],
        },
        scan: null,
        source_checks: {},
        thumbnails: { state: "active", done: 1, total: 10 },
      },
    }),
  );
  await page.route("**/api/downloads?*", (route) =>
    route.fulfill({
      json: {
        items: [
          job,
          ...Array.from({ length: 5 }, (_, i) => ({
            ...job,
            id: `other-${i}`,
          })),
        ],
        after: null,
        total: 26,
        page: 2,
        scope: "active",
        through: 26,
      },
    }),
  );
  await page.route("**/api/downloads/target-download", (route) =>
    route.fulfill({ json: job }),
  );
  await page.getByRole("button", { name: "任务队列", exact: true }).click();
  await page
    .locator(".task-popover .task-row")
    .filter({ hasText: title })
    .click();
  await expect(page).toHaveURL(/#downloads\/target-download$/);
  const target = page.locator("#download-target-download");
  await expect(target).toBeFocused();
  await expect(target).toHaveAttribute("aria-current", "true");
  for (const width of [320, 390, 750, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await expect
      .poll(() => page.evaluate(() => document.documentElement.scrollWidth))
      .toBeLessThanOrEqual(width);
    const link = target.locator(".download-title > a");
    const box = await link.boundingBox();
    expect(box!.height).toBeGreaterThan(40);
  }
  await page.getByRole("button", { name: "任务队列", exact: true }).click();
  await page
    .locator(".task-popover .task-row")
    .filter({ hasText: "生成缩略图" })
    .click();
  await expect(page).toHaveURL(/#settings\/cache$/);
  const cache = page
    .locator("details")
    .filter({ has: page.getByRole("heading", { name: "缓存", exact: true }) });
  await expect(cache).toHaveAttribute("open", "");
  await expect(cache.locator(".settings-section-body")).toBeVisible();
  await page.setViewportSize({ width: 320, height: 900 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth))
    .toBeLessThanOrEqual(320);
});

test("info tips open by touch or keyboard, stay in bounds and return focus", async ({
  page,
}, info) => {
  await page.goto("/#settings");
  const cache = page.getByRole("button", { name: "关于缓存", exact: true });
  const section = cache.locator("xpath=ancestor::details");
  await expect(section).not.toHaveAttribute("open", "");
  if (info.project.name === "mobile") await cache.tap();
  else {
    await cache.focus();
    await page.keyboard.press("Enter");
  }
  const tip = page.getByRole("dialog", { name: "关于缓存", exact: true });
  await expect(tip).toContainText("缩略图");
  await expect(section).not.toHaveAttribute("open", "");
  const bounds = await tip.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(
    page.viewportSize()!.width,
  );
  await page.keyboard.press("Escape");
  await expect(tip).toBeHidden();
  await expect(cache).toBeFocused();
  await page.getByText("来源账户与下载", { exact: true }).click();
  const comments = page.getByRole("checkbox", {
    name: "后台检查也获取评论",
    exact: true,
  });
  const before = await comments.isChecked();
  const help = page.getByRole("button", { name: "关于后台评论" });
  if (info.project.name === "mobile") await help.tap();
  else await help.click();
  await expect(
    page.getByRole("dialog", { name: "关于后台评论" }),
  ).toContainText("每本书多一次请求");
  expect(await comments.isChecked()).toBe(before);
});

test("removal distinguishes recoverable records from permanent file deletion", async ({
  page,
}) => {
  await detail(page);
  await page.getByRole("button", { name: "书籍操作", exact: true }).click();
  await page.getByRole("menuitem", { name: "移除…", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("7 天内恢复");
  await dialog.getByRole("checkbox", { name: "同时移除文件" }).check();
  await expect(dialog).toContainText("文件将从磁盘永久删除，无法恢复");
  await expect(
    dialog.getByRole("button", { name: "移除并删除文件", exact: true }),
  ).toBeVisible();
  await expect(dialog).not.toContainText("7 天内恢复");
  await dialog.getByRole("checkbox", { name: "同时移除文件" }).uncheck();
  await expect(
    dialog.getByRole("button", { name: "确认移除", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
});
