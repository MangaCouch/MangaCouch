import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures";

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await page
    .getByLabel("书库口令", { exact: true })
    .fill("browser-fixture-only");
  await page.getByRole("button", { name: "打开书库" }).click();
  await expect(page.locator(".book-card")).toHaveCount(24);
});

test("URL filters survive tag history and remain isolated from a plain new tab", async ({
  page,
  context,
}, info) => {
  await page.getByLabel("筛选与排序", { exact: true }).click();
  await page.getByRole("button", { name: "未读", exact: true }).click();
  await page.getByLabel("筛选与排序", { exact: true }).click();
  await expect(page).toHaveURL(/reading=unread/);
  const filtered = page.url();
  const other = await context.newPage();
  await other.goto(new URL("/", page.url()).href);
  await expect(other.locator(".book-card")).toHaveCount(24);
  expect(new URL(other.url()).search).toBe("");
  await page.locator(".book-card").first().click();
  await expect(page.locator(".book-intro")).toBeVisible();
  const detail = page.url();
  await page.locator(".tag-pill").first().click();
  await expect(page).toHaveURL(/query=/);
  expect(new URL(page.url()).searchParams.has("reading")).toBe(false);
  await page.goBack();
  await expect(page).toHaveURL(detail);
  await page.goBack();
  await expect(page).toHaveURL(filtered);
  await expect(page.locator(".book-card")).toHaveCount(24);
  await page.locator(".wordmark").click();
  await expect.poll(() => new URL(page.url()).search).toBe("");
  await page.screenshot({
    path: `/tmp/mc-e1-library-after-${info.project.name}.png`,
  });
  await other.close();
});

test("reader return consumes history and route buttons keep their full size", async ({
  page,
}, info) => {
  await page.locator(".book-card").filter({ hasText: "3页" }).first().click();
  await expect(page.locator(".book-intro")).toBeVisible();
  const detail = page.url();
  const read = page
    .locator(".book-intro a[data-route-link]")
    .filter({ hasText: /开始阅读|继续阅读|重新阅读/ });
  const metrics = await read.evaluate((el) => ({
    height: el.getBoundingClientRect().height,
    font: getComputedStyle(el).fontSize,
  }));
  expect(Math.round(metrics.height)).toBeGreaterThanOrEqual(40);
  expect(parseFloat(metrics.font)).toBeGreaterThanOrEqual(14);
  await page.screenshot({
    path: `/tmp/mc-e1-detail-after-${info.project.name}.png`,
  });
  await read.click();
  await expect(page.locator(".frame-ready")).toBeVisible();
  await page.getByRole("button", { name: "返回详情", exact: true }).click();
  await expect(page).toHaveURL(detail);
  await page.goBack();
  await expect(page.locator(".book-card")).toHaveCount(24);
  expect(new URL(page.url()).hash).toBe("");
});

test("failed chunks stay in the app until manual reload", async ({ page }) => {
  let documents = 0;
  page.on("request", (request) => {
    if (request.isNavigationRequest()) documents++;
  });
  await page.route(/\/assets\/Settings-.*\.js$/, (route) => route.abort());
  await page.getByRole("link", { name: "设置", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "页面资源加载失败" }),
  ).toBeVisible();
  expect(documents).toBe(0);
  await page.unroute(/\/assets\/Settings-.*\.js$/);
  await page.getByRole("button", { name: "重新载入", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "设置", exact: true }),
  ).toBeVisible();
  expect(documents).toBe(1);
});

test("rapid blocked navigation never reloads or loses editor input", async ({
  page,
}) => {
  await page.locator(".book-card").first().click();
  await page.getByRole("button", { name: "书籍操作", exact: true }).click();
  await page.getByRole("menuitem", { name: "编辑元数据", exact: true }).click();
  const input = page.locator(".metadata-form input").first();
  await input.fill("preserved draft");
  let documents = 0;
  page.on("request", (request) => {
    if (request.isNavigationRequest()) documents++;
  });
  const detail = page.url();
  await page.evaluate(() => {
    location.hash = "#settings";
    setTimeout(() => {
      location.hash = "#downloads";
    }, 0);
  });
  await expect(page).toHaveURL(detail);
  await page.evaluate(() => history.back());
  await expect(page).toHaveURL(detail);
  await expect(input).toHaveValue("preserved draft");
  expect(documents).toBe(0);
});

test("library restoration waits for late layout and paging starts at the top", async ({
  page,
}) => {
  await page.addStyleTag({ content: "* {overflow-anchor:none !important;}" });
  await page.locator(".book-card").last().scrollIntoViewIfNeeded();
  await expect
    .poll(() => page.evaluate(() => window.scrollY))
    .toBeGreaterThan(100);
  const saved = await page.evaluate(() => window.scrollY);
  await page.locator(".book-card").last().click();
  await expect(page.locator(".book-intro")).toBeVisible();
  await page.route("**/api/books?reading=in_progress*", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 300));
    await route.continue();
  });
  await page.goBack();
  await expect(page.locator(".book-card")).toHaveCount(24);
  await expect
    .poll(() => page.evaluate(() => window.scrollY))
    .toBeCloseTo(saved, 0);
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page).toHaveURL(/page=2/);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
});

/** A new collection holding the first three books, in that order. */
async function threeBookCollection(page: Page) {
  const headers = {
    "X-MangaCouch-Request": "1",
    Origin: new URL(page.url()).origin,
  };
  const books: { id: string }[] = (
    await (await page.request.get("/api/books?limit=3")).json()
  ).items;
  const created = await (
    await page.request.post("/api/collections", {
      headers,
      data: { name: `History ${Date.now()}` },
    })
  ).json();
  let revision = created.revision;
  for (const book of books) {
    const response = await page.request.post(
      `/api/collections/${created.id}/items?expected_revision=${revision}`,
      { headers, data: { book_id: book.id } },
    );
    expect(response.ok()).toBe(true);
    revision = (await response.json()).revision;
  }
  return { id: created.id as string, books: books.map((book) => book.id) };
}
const hash = (page: Page) => new URL(page.url()).hash;
async function readFromDetails(page: Page) {
  await page
    .locator(".book-intro a[data-route-link]")
    .filter({ hasText: /开始阅读|继续阅读|重新阅读/ })
    .click();
  await expect(page.locator(".frame-ready")).toBeVisible();
}
async function nextBook(page: Page, id: string) {
  const book = await (await page.request.get(`/api/books/${id}`)).json();
  await page.getByRole("button", { name: "下一本", exact: true }).click();
  await expect.poll(() => hash(page)).toContain(`#read/${id}/in/`);
  // The URL changes before the previous book's pending progress save finishes.
  await expect(page.locator(".reader-name strong")).toHaveText(
    book.metadata.title,
  );
  await expect(page.locator(".frame-ready")).toBeVisible();
}

test("after next book in a collection, returning to details and browser back both lead to the collection", async ({
  page,
}) => {
  const { id, books } = await threeBookCollection(page);
  await page.goto("about:blank");
  await page.goto(`/#collection/${id}`);
  await page.locator(".collection-book").first().click();
  await expect(page.locator(".book-intro")).toBeVisible();
  await readFromDetails(page);
  await nextBook(page, books[1]);
  await page.getByRole("button", { name: "返回详情", exact: true }).click();
  await expect.poll(() => hash(page)).toBe(`#book/${books[1]}/in/${id}`);
  await expect(page.locator(".book-intro")).toBeVisible();
  await expect(page.locator(".back-link")).toHaveText("合集");
  await page.goBack();
  await expect.poll(() => hash(page)).toBe(`#collection/${id}`);
  await expect(page.locator(".collection-book")).toHaveCount(3);
  // The browser's own back from the reader goes the same way.
  await page.locator(".collection-book").first().click();
  await readFromDetails(page);
  await nextBook(page, books[1]);
  await page.goBack();
  await expect.poll(() => hash(page)).toBe(`#book/${books[1]}/in/${id}`);
  await expect(page.locator(".book-intro")).toBeVisible();
  await page.goBack();
  await expect.poll(() => hash(page)).toBe(`#collection/${id}`);
});

test("the reader's collection button returns to the collection without a way back into the reader", async ({
  page,
}) => {
  const { id, books } = await threeBookCollection(page);
  await page.goto("about:blank");
  await page.goto(`/#book/${books[0]}/in/${id}`);
  await expect(page.locator(".book-intro")).toBeVisible();
  await readFromDetails(page);
  await nextBook(page, books[1]);
  await page.locator(".collection-reader > button").click();
  await expect.poll(() => hash(page)).toBe(`#collection/${id}`);
  await expect(page.locator(".collection-book")).toHaveCount(3);
  await page.goBack();
  await expect.poll(() => hash(page)).toBe("#collections");
});

test("a filtered library survives details, reader, return and browser back", async ({
  page,
}) => {
  await page.getByLabel("筛选与排序", { exact: true }).click();
  await page.getByRole("button", { name: "未读", exact: true }).click();
  await page.getByLabel("筛选与排序", { exact: true }).click();
  await expect(page).toHaveURL(/reading=unread/);
  const filtered = page.url();
  await page.locator("button.book-card").first().click();
  await expect(page.locator(".book-intro")).toBeVisible();
  const detail = page.url();
  await readFromDetails(page);
  await page.getByRole("button", { name: "返回详情", exact: true }).click();
  await expect(page).toHaveURL(detail);
  await page.goBack();
  await expect(page).toHaveURL(filtered);
  await expect(page.locator("button.book-card").first()).toBeVisible();
});

test("a directly opened reader link returns to its details, then the library", async ({
  page,
}) => {
  const id = (await (await page.request.get("/api/books?limit=1")).json())
    .items[0].id;
  await page.goto("about:blank");
  await page.goto(`/#read/${id}/page/2`);
  await expect(page.locator(".frame-ready")).toBeVisible();
  await page.getByRole("button", { name: "返回详情", exact: true }).click();
  await expect.poll(() => hash(page)).toBe(`#book/${id}`);
  await page.goBack();
  await expect.poll(() => hash(page)).toBe("");
  await expect(page.locator(".book-card")).toHaveCount(24);
});

test("the logo on the plain library adds no history entries", async ({
  page,
}) => {
  const before = await page.evaluate(() => history.length);
  await page.locator(".wordmark").click();
  await page.locator(".wordmark").click();
  await expect(page.locator(".book-card")).toHaveCount(24);
  expect(await page.evaluate(() => history.length)).toBe(before);
});

test("retired filter storage is removed at startup and reader settings stay", async ({
  page,
}) => {
  await page.evaluate(() => {
    localStorage.setItem(
      "mangacouch:library-filters",
      JSON.stringify({ reading: "unread" }),
    );
    sessionStorage.setItem(
      "mangacouch:library-view",
      JSON.stringify({ page: 3 }),
    );
    localStorage.setItem("mangacouch:reading-mode", "scroll");
  });
  await page.reload();
  await expect(page.locator(".book-card")).toHaveCount(24);
  expect(
    await page.evaluate(() => [
      localStorage.getItem("mangacouch:library-filters"),
      sessionStorage.getItem("mangacouch:library-view"),
      localStorage.getItem("mangacouch:reading-mode"),
    ]),
  ).toEqual([null, null, "scroll"]);
  expect(new URL(page.url()).search).toBe("");
});
