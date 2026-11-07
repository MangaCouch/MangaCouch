import { test, expect, type Page } from "@playwright/test";

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
  const { books } = await (
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
  await page.route("**/api/books/*/personal", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 900));
    await route.continue();
  });
  await rating.focus();
  await rating.press("End");
  await rating.press("ArrowLeft");
  await expect(rating).toHaveValue("4.5");
  await page.getByRole("button", { name: "整理", exact: true }).click();
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
