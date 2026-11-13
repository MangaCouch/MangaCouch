import { test, expect } from "./fixtures";
import type { Page } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await page
    .getByLabel("书库口令", { exact: true })
    .fill("browser-fixture-only");
  await page.getByRole("button", { name: "打开书库" }).click();
  await expect(page.locator(".book-card")).toHaveCount(24);
});
async function longBook(page: Page) {
  const catalog = await (await page.request.get("/api/books?limit=200")).json();
  const book = catalog.items.find(
    (item: { content: { page_count: number } }) =>
      item.content.page_count === 22,
  );
  const reset = await page.request.put(
    `/api/books/${book.id}/personal?expected_revision=${book.personal_revision}`,
    {
      headers: {
        "X-MangaCouch-Request": "1",
        Origin: new URL(page.url()).origin,
      },
      data: {
        state: { rating: null, resume_page: null, completed: false },
        record_activity: false,
      },
    },
  );
  expect(reset.ok()).toBe(true);
  return book.id as string;
}
for (const solo of [true, false])
  test(`spread can return after its object URLs were evicted, coverSolo=${solo}`, async ({
    page,
  }) => {
    const id = await longBook(page);
    await page.evaluate((solo) => {
      localStorage.setItem("mangacouch:reading-mode", "spread");
      localStorage.setItem("mangacouch:cover-solo", String(solo));
    }, solo);
    const start = solo ? 11 : 12;
    await page.goto(`/#read/${id}/page/${start + 1}`);
    const images = page.locator(".frame-ready .retained-frame img");
    await expect(images).toHaveCount(2);
    const before = await images.evaluateAll((nodes) =>
      nodes.map((n) => (n as HTMLImageElement).alt),
    );
    await page.locator(".reader-name strong").click();
    await page.keyboard.press("Home");
    await expect(images.first()).toHaveAttribute("alt", "第 1 页");
    await page.getByRole("slider", { name: "阅读位置" }).fill(String(start));
    await expect
      .poll(() =>
        images.evaluateAll((nodes) =>
          nodes.map((n) => (n as HTMLImageElement).alt),
        ),
      )
      .toEqual(before);
    await expect(
      page.locator('.page-transition-status[role="alert"]'),
    ).toHaveCount(0);
  });

test("wide final scroll page resumes at the actual end without saving an earlier page", async ({
  page,
}) => {
  const id = await longBook(page);
  await page.route(new RegExp(`/api/books/${id}(\\?.*)?$`), async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    json.page_sizes = Array.from({ length: 22 }, () => [1500, 500]);
    await route.fulfill({ response, json });
  });
  await page.evaluate(() => {
    localStorage.setItem("mangacouch:reading-mode", "scroll");
    localStorage.setItem("mangacouch:fit", "width");
  });
  await page.goto(`/#read/${id}/page/22`);
  const scroll = page.locator(".scroll-reader");
  await expect(scroll).toBeVisible();
  await expect
    .poll(() =>
      scroll.evaluate((n) =>
        Math.abs(n.scrollTop - (n.scrollHeight - n.clientHeight)),
      ),
    )
    .toBeLessThan(3);
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/books/${id}`)).json()).personal
          .resume_page,
    )
    .toBe(21);
});

test("scroll resume on a short viewport stays on the saved page and does not finish the book", async ({
  page,
}, info) => {
  test.skip(info.project.name === "mobile", "Desktop viewport scenario");
  await page.setViewportSize({ width: 1440, height: 640 });
  const id = await longBook(page);
  await page.route(new RegExp(`/api/books/${id}(\\?.*)?$`), async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const json = await response.json();
    json.page_sizes = Array.from({ length: 22 }, () => [1500, 500]);
    await route.fulfill({ response, json });
  });
  const saves: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "PUT" && request.url().includes("/personal")) {
      const state = request.postDataJSON().state;
      saves.push(`${state.resume_page}${state.completed ? "C" : ""}`);
    }
  });
  await page.evaluate(() => {
    localStorage.setItem("mangacouch:reading-mode", "scroll");
    localStorage.setItem("mangacouch:fit", "width");
  });
  // Saved progress: page 21 of 22 (0-based 20), which is not on the last screen here.
  const book = await (await page.request.get(`/api/books/${id}`)).json();
  const seeded = await page.request.put(
    `/api/books/${id}/personal?expected_revision=${book.personal_revision}`,
    {
      headers: {
        "X-MangaCouch-Request": "1",
        Origin: new URL(page.url()).origin,
      },
      data: {
        state: { rating: null, resume_page: 20, completed: false },
        record_activity: false,
      },
    },
  );
  expect(seeded.ok()).toBe(true);
  await page.goto(`/#read/${id}`);
  const scroll = page.locator(".scroll-reader");
  await expect(scroll).toBeVisible();
  const position = () =>
    scroll.evaluate((node) => ({
      top: Math.round(node.scrollTop),
      max: node.scrollHeight - node.clientHeight,
      target: node.querySelector<HTMLElement>('.scroll-slot[data-page="20"]')
        ?.offsetTop,
    }));
  await expect
    .poll(async () => {
      const value = await position();
      return value.target !== undefined && Math.abs(value.top - value.target);
    })
    .toBeLessThan(3);
  const settled = await position();
  expect(settled.max - settled.top).toBeGreaterThan(100);
  await page.waitForTimeout(1500);
  expect(Math.abs((await position()).top - settled.top)).toBeLessThan(3);
  const personal = (await (await page.request.get(`/api/books/${id}`)).json())
    .personal;
  expect(personal.completed).toBe(false);
  expect(personal.resume_page).toBe(20);
  expect(saves.filter((value) => value.endsWith("C"))).toEqual([]);
});

test("a missing book in a collection links back to that collection", async ({
  page,
}) => {
  await page.goto("/#read/missing-book/in/some-collection");
  await expect(page.getByRole("alert")).toContainText("不存在");
  const back = page.getByRole("link", { name: "返回合集", exact: true });
  await expect(back).toHaveAttribute("href", "#collection/some-collection");
  await expect(
    page.getByRole("link", { name: "返回书库", exact: true }),
  ).toHaveCount(0);
});

test("a missing reader book offers a library exit without a futile retry", async ({
  page,
}) => {
  await page.goto("/#read/missing-book");
  await expect(page.getByRole("alert")).toContainText("不存在");
  await expect(
    page.getByRole("button", { name: "重试", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("link", { name: "返回书库", exact: true }).click();
  await expect(page.locator(".book-card")).toHaveCount(24);
});

test("a slow progress write does not block returning to the same book details", async ({
  page,
}) => {
  const id = await longBook(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const requests: { record_activity: boolean }[] = [];
  await page.route(`**/api/books/${id}/personal?*`, async (route) => {
    requests.push(route.request().postDataJSON());
    await gate;
    await route.continue();
  });
  await page.goto(`/#read/${id}/page/4`);
  await expect(page.locator(".frame-ready")).toBeVisible();
  await expect.poll(() => requests.length).toBeGreaterThan(0);
  await page.getByRole("button", { name: "返回详情", exact: true }).click();
  await expect(page.locator(".book-intro")).toBeVisible({ timeout: 2000 });
  expect(requests[0].record_activity).toBe(true);
  release();
});

test("leaving a reader releases its fullscreen", async ({ page }, info) => {
  test.skip(info.project.name === "mobile", "Desktop fullscreen API only");
  const id = await longBook(page);
  await page.goto(`/#read/${id}`);
  await expect(page.locator(".frame-ready")).toBeVisible();
  await page.getByRole("button", { name: "全屏", exact: true }).click();
  await expect
    .poll(() => page.evaluate(() => !!document.fullscreenElement))
    .toBe(true);
  await page.getByRole("button", { name: "返回详情", exact: true }).click();
  await expect(page.locator(".book-intro")).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => !!document.fullscreenElement))
    .toBe(false);
});

test("collection navigation and failed-save notice never overlap", async ({
  page,
}) => {
  const id = await longBook(page);
  await page.route(`**/api/books/${id}/personal?*`, (route) =>
    route.fulfill({ status: 400, json: { error: "invalid_input" } }),
  );
  await page.route("**/api/collections/review-collection/context?*", (route) =>
    route.fulfill({
      json: {
        name: "Test collection",
        index: 0,
        count: 1,
        previous_id: null,
        next_id: null,
        continue_id: id,
        all_completed: false,
      },
    }),
  );
  await page.goto(`/#read/${id}/in/review-collection/page/4`);
  await expect(page.locator(".reader-warning")).toBeVisible();
  const nav = await page.locator(".collection-reader").boundingBox();
  const warning = await page.locator(".reader-warning").boundingBox();
  expect(nav).not.toBeNull();
  expect(warning).not.toBeNull();
  expect(warning!.y).toBeGreaterThanOrEqual(nav!.y + nav!.height);
});

test("a book removed while reading explains the return to the library", async ({
  page,
}) => {
  const id = await longBook(page);
  await page.route(`**/api/books/${id}/personal?*`, (route) =>
    route.fulfill({ status: 404, json: { error: "book_not_found" } }),
  );
  await page.goto(`/#read/${id}/page/4`);
  await expect(page.locator(".book-card")).toHaveCount(24);
  await expect(
    page.getByText("这本书已被移除。", { exact: true }),
  ).toBeVisible();
});
