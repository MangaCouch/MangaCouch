import { test, expect } from "./fixtures";

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await page
    .getByLabel("书库口令", { exact: true })
    .fill("browser-fixture-only");
  await page.getByRole("button", { name: "打开书库" }).click();
  await expect(page.locator(".book-card")).toHaveCount(24);
});

test("collection returns from a book to its saved scroll position", async ({
  page,
}) => {
  const books = (await (await page.request.get("/api/books?limit=200")).json())
    .items.slice(0, 24);
  const collection = {
    id: "scroll-review",
    name: "Scroll review",
    kind: "static",
    revision: 1,
    count: books.length,
    after: null,
    items: books.map((book: { id: string }, position: number) => ({
      book_id: book.id,
      position,
      book,
    })),
  };
  await page.route("**/api/collections/scroll-review?*", (route) =>
    route.fulfill({ json: collection }),
  );
  await page.route("**/api/collections/scroll-review/context*", (route) =>
    route.fulfill({
      json: {
        ...collection,
        continue_id: books[0].id,
        continue_state: "unread",
        all_completed: false,
        index: 0,
        previous_id: null,
        next_id: books[1].id,
      },
    }),
  );
  await page.goto("/#collection/scroll-review");
  await expect(page.locator(".collection-row")).toHaveCount(24);
  const book = page.locator(".collection-book").nth(15);
  await book.scrollIntoViewIfNeeded();
  const before = await page.evaluate(() => window.scrollY);
  expect(before).toBeGreaterThan(500);
  await book.click();
  await expect(page.locator(".book-intro")).toBeVisible();
  await page.goBack();
  await expect(page.locator(".collection-row")).toHaveCount(24);
  await expect
    .poll(async () =>
      Math.abs((await page.evaluate(() => window.scrollY)) - before),
    )
    .toBeLessThan(5);
});
