import { test, expect, type Page } from "@playwright/test";
const writeHeaders = {
  "X-MangaCouch-Request": "1",
  Origin: "http://127.0.0.1:8793",
};
async function menu(page: Page, label: string) {
  await page.getByRole("button", { name: "书籍操作", exact: true }).click();
  await page.getByRole("menuitem", { name: label, exact: true }).click();
}
async function openBook(page: Page) {
  await page.locator(".book-card").filter({ hasText: "3页" }).first().click();
  await expect(page.locator(".book-intro")).toBeVisible();
}
let sessionCookies:
  | Awaited<ReturnType<import("@playwright/test").BrowserContext["cookies"]>>
  | undefined;
test.beforeEach(async ({ page, context }) => {
  if (sessionCookies) await context.addCookies(sessionCookies);
  await page.goto("/");
  if (!sessionCookies) {
    await page
      .getByLabel("书库口令", { exact: true })
      .fill("browser-fixture-only");
    await page.getByRole("button", { name: "打开书库" }).click();
    await expect(page.locator(".book-card")).toHaveCount(24);
    sessionCookies = await context.cookies();
  }
  await expect(page.locator(".book-card")).toHaveCount(24);
});
test("stable five-item suggestions, explicit token selection, whole-token deletion and clear", async ({
  page,
}) => {
  const input = page.getByRole("combobox", { name: "搜索书库" });
  await input.fill("blu");
  await expect(page.getByRole("option")).toHaveCount(5);
  await page.route("**/api/tag-suggestions?*", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 350));
    await route.continue();
  });
  await input.press("End");
  await input.press("e");
  await expect(page.getByRole("listbox")).toBeVisible();
  await expect(page.getByRole("option")).toHaveCount(5);
  await page.getByRole("option").filter({ hasText: "blue archive" }).click();
  await expect(page.locator(".search-token")).toHaveCount(1);
  await expect(page.locator(".book-card")).toHaveCount(6);
  await page.locator(".search-token").click();
  await page.keyboard.press("Backspace");
  await expect(page.locator(".search-token")).toHaveCount(0);
  await expect(page.locator(".book-card")).toHaveCount(24);
  await input.fill("no-match-fixture");
  await expect(page.locator(".book-card")).toHaveCount(0);
  await page.getByRole("button", { name: "清空搜索" }).click();
  await expect(page.locator(".book-card")).toHaveCount(24);
});
test("pagination and themed filter dropdown", async ({ page }, info) => {
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(page.locator(".book-card")).toHaveCount(7);
  await page.getByRole("button", { name: "跳转页码" }).click();
  await page.getByLabel("跳到页", { exact: true }).fill("1");
  await page.getByRole("button", { name: "跳转", exact: true }).click();
  await expect(page.locator(".book-card")).toHaveCount(24);
  if (info.project.name === "desktop") {
    await page.locator("h1").click();
    await page.keyboard.press("ArrowRight");
    await expect(page.locator(".book-card")).toHaveCount(7);
    await page.keyboard.press("ArrowLeft");
    await expect(page.locator(".book-card")).toHaveCount(24);
  }
  await page.locator(".library-filters summary").click();
  await page.getByRole("combobox", { name: "语言", exact: true }).click();
  await page.getByRole("option", { name: "英文", exact: true }).click();
  await expect(page.locator(".book-card")).toHaveCount(10);
  await page.getByRole("button", { name: "清除筛选", exact: true }).click();
  await expect(page.locator(".book-card")).toHaveCount(24);
});
test("selection stays in the heading, Shift adds ranges, global select-all and Escape", async ({
  page,
}, info) => {
  test.skip(
    info.project.name === "mobile",
    "Desktop keyboard and drag interactions",
  );
  const cards = page.locator(".book-card"),
    circles = page.locator(".book-select");
  const y = await cards
    .first()
    .evaluate((el) => (el.parentElement as HTMLElement).offsetTop);
  await circles.nth(0).click();
  await circles.nth(2).click({ modifiers: ["Shift"] });
  await circles.nth(4).click();
  await circles.nth(6).click({ modifiers: ["Shift"] });
  await expect(page.locator(".shelf-item.selected")).toHaveCount(6);
  await page.locator(".shelf-heading").scrollIntoViewIfNeeded();
  expect(
    await cards
      .first()
      .evaluate((el) => (el.parentElement as HTMLElement).offsetTop),
  ).toBeCloseTo(y, 0);
  await page.locator("h1").click();
  await page.keyboard.press("Control+A");
  await expect(page.locator(".shelf-item.selected")).toHaveCount(24);
  await page.keyboard.press("Escape");
  await expect(page.locator(".selection-toolbar")).toHaveCount(0);
  await cards.first().click({ button: "right" });
  await expect(page.getByRole("menu")).toBeVisible();
  await page.keyboard.press("Escape");
});
test("detail thumbnails, real Radix dialog dropdown and drag-out text selection", async ({
  page,
}, info) => {
  await openBook(page);
  await expect(page.locator(".page-thumbnails img")).toHaveCount(3);
  await menu(page, "编辑元数据");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog
    .getByRole("combobox")
    .filter({ hasText: "中文" })
    .or(dialog.getByRole("combobox").filter({ hasText: "日文" }))
    .or(dialog.getByRole("combobox").filter({ hasText: "英文" }))
    .first()
    .click();
  await page.getByRole("option", { name: "其他", exact: true }).click();
  await expect(dialog).toBeVisible();
  if (info.project.name === "desktop") {
    const box = await dialog.locator("input").first().boundingBox();
    await page.mouse.move(box!.x + 20, box!.y + 15);
    await page.mouse.down();
    await page.mouse.move(5, 5, { steps: 10 });
    await page.mouse.up();
    await expect(dialog).toBeVisible();
  }
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  // The changed language makes the form dirty: closing asks before dropping it.
  await expect(dialog.getByText("放弃未保存的修改？")).toBeVisible();
  await dialog.getByRole("button", { name: "放弃修改", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await menu(page, "编辑元数据");
  await expect(
    page.getByRole("dialog").getByRole("combobox").first(),
  ).not.toHaveText("其他");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.locator(".page-thumbnails a").nth(1).click();
  await expect(page.getByRole("slider", { name: "阅读进度" })).toHaveValue("1");
  await expect(page.getByRole("button", { name: /书签/ })).toHaveCount(0);
});
test("settings save without a save button and mobile layout stays inside viewport", async ({
  page,
}) => {
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(page.locator(".settings-category")).toHaveCount(5);
  const before = await (await page.request.get("/api/preferences")).json();
  await page
    .locator(".settings-category")
    .first()
    .getByRole("combobox")
    .nth(1)
    .click();
  await page.getByRole("option", { name: "原始优先" }).click();
  await expect
    .poll(
      async () =>
        (await (await page.request.get("/api/preferences")).json()).settings
          .title_preference,
    )
    .toBe("original_title");
  const access = page
    .locator(".settings-category")
    .filter({ hasText: "访问与连接" });
  await access.locator("summary").first().click();
  // OPDS is its own collapsible section now; open it next to the devices list.
  await access
    .locator("summary")
    .filter({ hasText: "外部阅读器（OPDS）" })
    .click();
  await expect(page.getByLabel("OPDS URL")).toHaveValue(
    "http://127.0.0.1:8793/opds",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const latest = await (await page.request.get("/api/preferences")).json();
  await page.request.put("/api/preferences", {
    headers: writeHeaders,
    data: { settings: before.settings, expected_revision: latest.revision },
  });
});
test("dynamic collection is created, renamed inline, frozen and deleted", async ({
  page,
}) => {
  await page.getByRole("button", { name: "合集", exact: true }).click();
  await page.getByRole("button", { name: "新建合集" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("名称", { exact: true }).fill("Browser dynamic");
  await dialog.getByRole("button", { name: "动态", exact: true }).click();
  await dialog.getByRole("button", { name: "创建", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Browser dynamic" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Browser dynamic", exact: true })
    .click();
  await page.getByRole("textbox", { name: "合集名称" }).fill("Renamed dynamic");
  await page.getByRole("textbox", { name: "合集名称" }).press("Enter");
  await expect(
    page.getByRole("heading", { name: "Renamed dynamic" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "合集操作" }).click();
  await page.getByRole("menuitem", { name: "转换成静态合集" }).click();
  await expect(page.locator(".drag-handle").first()).toBeVisible();
  await page.getByRole("button", { name: "合集操作" }).click();
  await page.getByRole("menuitem", { name: "删除合集", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认删除" })
    .click();
  await expect(
    page.getByRole("heading", { name: "合集", exact: true }),
  ).toBeVisible();
});
test("removal dialog records to recycle bin and restores without deleting the archive", async ({
  page,
}) => {
  await openBook(page);
  const bookId = page.url().split("#book/")[1]!;
  await menu(page, "移除");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("同时移除文件")).not.toBeChecked();
  await page.mouse.click(2, 2);
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "确认移除" }).click();
  await expect(page.locator(".book-intro")).toHaveCount(0);
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page
    .locator(".settings-category")
    .filter({ hasText: "维护与恢复" })
    .locator("summary")
    .first()
    .click();
  await page
    .getByRole("button", { name: "恢复记录", exact: true })
    .first()
    .click();
  await expect(page.getByText("已恢复", { exact: true })).toBeVisible();
  await expect
    .poll(async () => (await page.request.get(`/api/books/${bookId}`)).ok())
    .toBe(true);
});
test("bulk state applies only the selected books", async ({ page }) => {
  await page.locator(".book-select").first().click();
  await page.getByRole("button", { name: "批量操作", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "已读", exact: true }).click();
  await dialog.getByRole("button", { name: "应用", exact: true }).click();
  await expect(
    dialog.getByRole("button", { name: "应用", exact: true }),
  ).toBeEnabled();
  await expect(dialog.getByRole("status")).toContainText("完成");
  await dialog
    .getByRole("button", { name: "关闭", exact: true })
    .first()
    .click();
});
test("tag editor stages namespace and value without dismissing the editor", async ({
  page,
}) => {
  await openBook(page);
  await menu(page, "编辑元数据");
  const dialog = page.getByRole("dialog");
  const namespace = dialog.getByRole("combobox", { name: "标签命名空间" });
  await namespace.fill("artist:");
  const value = dialog.getByRole("combobox", { name: "标签值" });
  await value.fill("fixture custom");
  await value.press("Escape");
  await expect(value).toHaveValue("");
  await expect(dialog).toBeVisible();
  await value.fill("fixture custom");
  await value.press("Enter");
  await expect(dialog.locator(".tag-editor")).toContainText("fixture custom");
  await dialog
    .getByRole("button", { name: "关闭", exact: true })
    .first()
    .click();
});
test("download is recorded before remote completion and supports nHentai choice", async ({
  page,
}) => {
  await page.getByRole("button", { name: "下载", exact: true }).click();
  await page
    .getByRole("textbox", { name: "作品链接" })
    .fill("https://nhentai.net/g/200/");
  await page
    .getByRole("button", { name: "寻找 E(x)Hentai 版本", exact: true })
    .click();
  await page.getByRole("button", { name: "继续", exact: true }).click();
  await expect(page.locator(".download-row")).toHaveCount(1);
  await expect(page.locator(".download-candidates")).toBeVisible({
    timeout: 15000,
  });
  await expect(
    page.locator(".source-candidate").first().getByRole("link").first(),
  ).toHaveAttribute("href", "https://nhentai.net/g/200/");
  await page
    .locator(".source-candidate")
    .first()
    .getByRole("button", { name: "选择", exact: true })
    .click();
  await expect(page.locator(".download-candidates")).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
test("rapid turns retain the old frame, show a preview and settle on decoded full-size pages", async ({
  page,
}, info) => {
  test.skip(
    info.project.name === "mobile",
    "Held keyboard repeat is a desktop interaction",
  );
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const requests: number[] = [];
  await page.route("**/api/books/*/pages/*", async (route) => {
    const index = Number(
      new URL(route.request().url()).pathname.split("/").at(-1),
    );
    requests.push(index);
    const response = await route.fetch();
    if (index > 0) await gate;
    await route.fulfill({ response }).catch(() => {});
  });
  try {
    const catalog = await (
      await page.request.get("/api/books?limit=200")
    ).json();
    const book = catalog.books.find(
      (item: { content: { page_count: number } }) =>
        item.content.page_count === 22,
    );
    expect(book).toBeDefined();
    await page.goto(`/#book/${book.id}`);
    await page.locator(".page-thumbnails a").first().click();
    await expect(page.locator(".frame-ready")).toBeVisible();
    await page.locator(".reader-name strong").click();
    await page.keyboard.down("ArrowRight");
    await expect(page.getByRole("slider", { name: "阅读进度" })).toHaveValue(
      "1",
    );
    for (let i = 0; i < 12; i++) {
      await page.waitForTimeout(25);
      await page.keyboard.down("ArrowRight");
    }
    await page.keyboard.up("ArrowRight");
    const target = Number(
      await page.getByRole("slider", { name: "阅读进度" }).inputValue(),
    );
    expect(target).toBeGreaterThan(1);
    expect(target).toBeLessThan(9);
    await expect(
      page.locator(".frame-loading .retained-frame img"),
    ).toBeVisible();
    await expect(page.locator(".reader-preview img")).toHaveAttribute(
      "src",
      new RegExp(`/thumbnails/${target}(\\?|$)`),
    );
    expect(requests.length).toBeLessThan(9);
    release();
    await expect(
      page.locator(".frame-ready .retained-frame img"),
    ).toHaveAttribute("alt", `第 ${target + 1}`);
    await page.keyboard.press("ArrowLeft");
    await expect(page.locator(".frame-ready")).toBeVisible();
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
  }
});

test("task menu, settings summaries and section geometry stay visible", async ({
  page,
}) => {
  await page.getByRole("button", { name: "任务队列", exact: true }).click();
  await expect(page.locator(".task-popover")).toBeVisible();
  await expect(page.locator(".task-popover")).toContainText("等待");
  await page.keyboard.press("Escape");
  await openBook(page);
  const blocks = await page.locator(".detail > .panel").evaluateAll((nodes) =>
    nodes.map((node) => {
      const b = node.getBoundingClientRect();
      return { top: b.top, bottom: b.bottom };
    }),
  );
  for (let i = 1; i < blocks.length; i++)
    expect(blocks[i]!.top - blocks[i - 1]!.bottom).toBeGreaterThanOrEqual(19);
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(page.locator(".settings-category > h2")).toHaveCount(5);
  await expect(page.locator(".settings-category > summary")).toHaveCount(0);
  await expect(
    page
      .locator(".settings-section")
      .filter({ hasText: "本子去重" })
      .locator("summary small"),
  ).toContainText("已分析");
  await expect(page.locator(".settings-section[open]")).toHaveCount(0);
});

test("long titles and English compact pills stay within the viewport", async ({
  page,
}) => {
  await page.getByRole("button", { name: "紧凑列表" }).click();
  await page.evaluate(() => localStorage.setItem("mangacouch:language", "en"));
  await page.reload();
  await expect(page.locator('.app-shell[lang="en"]')).toBeVisible();
  const overlapping = await page
    .locator(".compact-shelf .book-pills")
    .evaluateAll((rows) =>
      rows.some((row) => {
        const boxes = Array.from(row.children).map((child) =>
          child.getBoundingClientRect(),
        );
        return boxes.some((box, i) =>
          boxes
            .slice(i + 1)
            .some(
              (other) =>
                box.left < other.right &&
                box.right > other.left &&
                box.top < other.bottom &&
                box.bottom > other.top,
            ),
        );
      }),
    );
  expect(overlapping).toBe(false);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.evaluate(() => localStorage.setItem("mangacouch:language", "zh"));
  await page.reload();
  await page.route(/\/api\/books\/[^/?]+$/, async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.metadata.title = "Long title 无空格长标题".repeat(80);
    await route.fulfill({ response, json: data });
  });
  await openBook(page);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.locator(".page-thumbnails a").first().click();
  await expect(page.locator(".frame-ready")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("blocked back navigation preserves both history entries", async ({
  page,
}) => {
  await openBook(page);
  const detailURL = page.url();
  await menu(page, "编辑元数据");
  await page
    .locator(".metadata-form input")
    .first()
    .fill("Unsaved history guard draft");
  await page.evaluate(() => history.back());
  await expect(
    page
      .getByText("请先完成正在进行的操作或处理未保存的修改，再离开此页。", {
        exact: true,
      })
      .first(),
  ).toBeVisible();
  await expect(page).toHaveURL(detailURL);
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "关闭", exact: true })
    .click();
  // Closing the dirty editor asks first; discarding the draft unblocks navigation.
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "放弃修改", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.evaluate(() => history.back());
  await expect(page.locator(".book-card")).toHaveCount(24);
  await page.evaluate(() => history.forward());
  await expect(page).toHaveURL(detailURL);
  await expect(page.locator(".book-intro")).toBeVisible();
});
