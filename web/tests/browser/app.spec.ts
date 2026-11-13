import { type Page } from "@playwright/test";
import { test, expect } from "./fixtures";
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
test("thumbnail refresh is shared by independent browser contexts", async ({
  page,
  context,
  browser,
}) => {
  const card = page.locator(".book-card").first();
  const cover = card.locator("img");
  const previous = await cover.getAttribute("src");
  const otherContext = await browser.newContext({
    storageState: await context.storageState(),
  });
  try {
    const other = await otherContext.newPage();
    await other.goto(page.url());
    const otherCover = other.locator(".book-card").first().locator("img");
    await expect(otherCover).toHaveAttribute("src", previous!);
    await card.click({ button: "right" });
    await page
      .getByRole("menuitem", { name: "刷新缩略图", exact: true })
      .click();
    await expect(cover).not.toHaveAttribute("src", previous!);
    const refreshed = await cover.getAttribute("src");
    await other.reload();
    await expect(otherCover).toHaveAttribute("src", refreshed!);
    await expect(otherCover).toBeVisible();
    await expect
      .poll(() =>
        otherCover.evaluate((img: HTMLImageElement) => img.naturalWidth),
      )
      .toBeGreaterThan(0);
  } finally {
    await otherContext.close();
  }
});
test("five tag suggestions, choosing one as a search token, whole-token deletion and clear", async ({
  page,
}, info) => {
  const input = page.getByRole("combobox", { name: "搜索书库" });
  const popover = page.locator(".suggestion-popover");
  await input.fill("blu");
  await expect(page.getByRole("option")).toHaveCount(5);
  // Hold the next lookup: the open popover drops the old list and waits for the new one.
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  let intercepted!: () => void;
  const reached = new Promise<void>((resolve) => {
    intercepted = resolve;
  });
  await page.route("**/api/tag-suggestions?*", async (route) => {
    if (new URL(route.request().url()).searchParams.get("query") === "blue")
      intercepted();
    await held;
    await route.continue();
  });
  await input.press("End");
  await input.press("e");
  await expect(popover).toContainText("正在查找…");
  await expect(page.getByRole("option")).toHaveCount(0);
  // The lookup for "blue" was really sent and held, not still waiting on its delay.
  await reached;
  const answered = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/api/tag-suggestions" &&
      url.searchParams.get("query") === "blue" &&
      response.ok()
    );
  });
  release();
  await answered;
  await page.unroute("**/api/tag-suggestions?*");
  await expect(input).toHaveValue("blue");
  await expect(popover).toHaveAttribute("aria-busy", "false");
  await expect(popover.getByRole("option")).toHaveCount(5);
  // A CI run once clicked the option without a token appearing. Record what the click hit
  // and whether a token appeared and was removed again, so a recurrence can tell a lost
  // click from a token cleared after choosing.
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.evaluate(() => {
    const log: string[] = [];
    const start = performance.now();
    const note = (text: string) =>
      log.push(`${(performance.now() - start).toFixed(1)}ms ${text}`);
    const describe = (node: EventTarget | null) => {
      if (!(node instanceof Element)) return String(node);
      const role = node.getAttribute("role");
      return `${node.tagName.toLowerCase()}${role ? `[role=${role}]` : ""}${
        node.className && typeof node.className === "string"
          ? `.${node.className.split(" ").join(".")}`
          : ""
      } "${(node.textContent ?? "").slice(0, 30)}" connected=${node.isConnected}`;
    };
    for (const type of ["pointerdown", "pointerup", "click", "focusout"])
      document.addEventListener(
        type,
        (event) =>
          note(
            `${type} target=${describe(event.target)}${
              event instanceof FocusEvent
                ? ` related=${describe(event.relatedTarget)}`
                : ""
            }`,
          ),
        true,
      );
    const field = document.querySelector<HTMLInputElement>('[role="combobox"]');
    let typed: string | undefined;
    new MutationObserver((records) => {
      for (const record of records) {
        for (const node of record.addedNodes)
          if (
            node instanceof Element &&
            node.matches(".search-token, .suggestion-popover")
          )
            note(`added ${describe(node)}`);
        for (const node of record.removedNodes)
          if (
            node instanceof Element &&
            node.matches(".search-token, .suggestion-popover")
          )
            note(`removed ${node.className}`);
      }
      if (field?.value !== typed) note(`input="${(typed = field?.value)}"`);
    }).observe(document.body, { childList: true, subtree: true });
    (window as unknown as { searchLog: string[] }).searchLog = log;
  });
  // Suggestions show the translated tag only; "blue archive" is 碧蓝档案.
  await popover.getByRole("option").filter({ hasText: "碧蓝档案" }).click();
  try {
    await expect(page.locator(".search-token")).toHaveCount(1);
  } catch (error) {
    const log = await page.evaluate(
      () => (window as unknown as { searchLog: string[] }).searchLog,
    );
    const report = [...log, ...errors.map((item) => `pageerror ${item}`)].join(
      "\n",
    );
    console.log(`search token diagnostics:\n${report}`);
    await info.attach("search-token-diagnostics", {
      body: report,
      contentType: "text/plain",
    });
    throw error;
  }
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
  const language = page.getByRole("combobox", { name: "语言", exact: true });
  await expect(
    page.getByRole("combobox", { name: "来源", exact: true }),
  ).toHaveCount(0);
  await language.click();
  await page.getByRole("option", { name: "英文", exact: true }).click();
  await expect(page.locator(".book-card")).toHaveCount(10);
  // Closing the open list by pressing its trigger again keeps the filters open.
  await language.click();
  // Radix listens for outside presses only once the open list has settled and focused its item.
  await expect(
    page.getByRole("option", { name: "英文", exact: true }),
  ).toBeFocused();
  // The open list hides the page from the accessibility tree; a CSS locator still finds the trigger.
  const box = (await page
    .locator(".filter-popover .select-control")
    .nth(1)
    .boundingBox())!;
  const x = box.x + box.width / 2,
    y = box.y + box.height / 2;
  if (info.project.name === "mobile") await page.touchscreen.tap(x, y);
  else await page.mouse.click(x, y);
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(page.locator(".filter-popover")).toBeVisible();
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
  await expect(page.locator(".selection-toolbar .info-tip")).toHaveCount(0);
  for (const target of [cards.first(), circles.first()]) {
    for (const key of ["Shift+F10", "ContextMenu"]) {
      await target.focus();
      await expect(target).toBeFocused();
      await page.keyboard.press(key);
      await expect(page.getByRole("menu")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("menu")).toHaveCount(0);
      await expect(page.locator(".shelf-item.selected")).toHaveCount(6);
    }
  }
  await cards.first().click({ button: "right" });
  await expect(page.getByRole("menu")).toBeVisible();
  await page.keyboard.press("Escape");
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
  await expect(page.getByRole("slider", { name: "阅读位置" })).toHaveValue("1");
  await expect(page.getByRole("button", { name: /书签/ })).toHaveCount(0);
});
test("settings fit the viewport and OPDS shows this server's address", async ({
  page,
  baseURL,
}, info) => {
  await page.getByRole("link", { name: "设置", exact: true }).click();
  const access = page
    .locator(".settings-category")
    .filter({ hasText: "访问与连接" });
  // The devices list and OPDS open side by side.
  await access.locator("summary").first().click();
  await access
    .locator("summary")
    .filter({ hasText: "外部阅读器（OPDS）" })
    .click();
  await expect(page.getByLabel("OPDS URL")).toHaveValue(
    new URL("/opds", baseURL).href,
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const { version } = await (await page.request.get("/api/settings/summary")).json();
  expect(version).toMatch(/^(main|v\d+\.\d+\.\d+)-[0-9a-f]{7}$/);
  await expect(page.locator(".settings-version")).toHaveText(`版本 ${version}`);
  await page.locator(".settings-version").scrollIntoViewIfNeeded();
  await page.screenshot({ path: `/tmp/mangacouch-settings-version-${info.project.name}.png` });
});
test("dynamic collection is created, renamed inline, frozen and deleted", async ({
  page,
}) => {
  await page.getByRole("link", { name: "合集", exact: true }).click();
  await page.getByRole("button", { name: "新建合集" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("名称", { exact: true }).fill("Browser dynamic");
  await dialog.getByRole("button", { name: "动态", exact: true }).click();
  // The rule's filters open inside the dialog, in view, rather than below its edge.
  await dialog.locator(".library-filters summary").click();
  const category = dialog.getByRole("combobox", { name: "类别", exact: true });
  await expect(category).toBeInViewport();
  await dialog.locator(".library-filters summary").click();
  await expect(category).toBeHidden();
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
  try {
    await menu(page, "移除…");
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByLabel("同时移除文件")).not.toBeChecked();
    await page.mouse.click(2, 2);
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "确认移除" }).click();
    await expect(page.locator(".book-intro")).toHaveCount(0);
    // The success notice stays clear of the header actions.
    await expect(
      page.getByText("已移除 1 本书", { exact: true }),
    ).toBeVisible();
    await page.getByRole("link", { name: "设置", exact: true }).click();
    await page
      .locator(".settings-category")
      .filter({ hasText: "维护与恢复" })
      .locator("summary")
      .first()
      .click();
    // Earlier runs against the shared fixture may have restored records already.
    const restored = page.getByText("已恢复", { exact: true });
    const restore = page
      .getByRole("button", { name: "恢复记录", exact: true })
      .first();
    await expect(restore).toBeVisible();
    const before = await restored.count();
    await restore.click();
    await expect(restored).toHaveCount(before + 1);
    await expect
      .poll(async () => (await page.request.get(`/api/books/${bookId}`)).ok())
      .toBe(true);
  } finally {
    // The fixture library is shared by later tests: never leave the book removed.
    if (!(await page.request.get(`/api/books/${bookId}`)).ok()) {
      const receipts = await (
        await page.request.get("/api/removals?limit=50", {
          headers: { "X-MangaCouch-Request": "1" },
        })
      ).json();
      for (const receipt of receipts.items)
        if (receipt.book_ids.includes(bookId) && receipt.can_restore)
          await page.request.post(`/api/removals/${receipt.id}/restore`, {
            headers: { "X-MangaCouch-Request": "1" },
          });
    }
  }
});

test("notices never cover the header actions", async ({ page }) => {
  // Refreshing thumbnails shows a notice; every header control stays clickable meanwhile.
  await page.locator(".book-card").first().click({ button: "right" });
  await page.getByRole("menuitem", { name: "刷新缩略图", exact: true }).click();
  const notice = page.locator("[data-sonner-toast]").filter({
    hasText: "缩略图将重新生成",
  });
  await expect(notice).toBeVisible();
  // Let the notice finish sliding in (its life is 4.5 s).
  await page.waitForTimeout(700);
  // Opening the menu may have scrolled the header away; bring it back.
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  // The notice starts below the header bar, not over it.
  const head = (await page.locator(".site-head").boundingBox())!;
  expect((await notice.boundingBox())!.y).toBeGreaterThanOrEqual(
    head.y + head.height,
  );
  // Whatever the pointer meets at each control's centre is that control, not the notice
  // (a pointer resting on a notice also pauses its timer, so a covered control stayed
  // covered).
  const controls = page.locator(
    ".site-head > .wordmark, .site-head > .actions > :is(button, a)",
  );
  expect(await controls.count()).toBeGreaterThanOrEqual(5);
  for (const control of await controls.all()) {
    const box = (await control.boundingBox())!;
    expect(
      await control.evaluate(
        (node, [x, y]) => {
          const hit = document.elementFromPoint(x!, y!);
          return hit && node.contains(hit)
            ? "control"
            : (hit?.outerHTML.slice(0, 200) ?? "nothing");
        },
        [box.x + box.width / 2, box.y + box.height / 2],
      ),
    ).toBe("control");
  }
  const settings = page.getByRole("link", { name: "设置", exact: true });
  await settings.click({ timeout: 2500 });
  await expect(
    page.getByText("缩略图将重新生成", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "设置", exact: true }),
  ).toBeVisible();
});

test("a missing chunk offers an explicit reload and recovers after an upgrade", async ({
  page,
}) => {
  let missing = true;
  await page.route(/\/assets\/Activity-[^/]+\.js$/, (route) =>
    missing ? route.fulfill({ status: 404, body: "" }) : route.continue(),
  );
  let loads = 0;
  page.on("load", () => loads++);
  await page.getByRole("link", { name: "阅读记录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "页面资源加载失败" }),
  ).toBeVisible();
  // A missing chunk can also mean a temporary outage: stay until the user retries.
  expect(loads).toBe(0);
  missing = false;
  await page.getByRole("button", { name: "重新载入", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "阅读记录", exact: true }),
  ).toBeVisible();
});
test("tag editor stages namespace and value without dismissing the editor", async ({
  page,
}) => {
  await openBook(page);
  await menu(page, "编辑元数据");
  const dialog = page.getByRole("dialog");
  const namespace = dialog.getByRole("combobox", { name: "标签", exact: true });
  await namespace.fill("artist:");
  const value = dialog.getByRole("combobox", { name: "标签值" });
  await value.fill("fixture custom");
  await value.press("Escape");
  await expect(value).toHaveValue("");
  await expect(dialog).toBeVisible();
  await value.fill("fixture custom");
  await value.press("Enter");
  await expect(dialog.locator(".tag-editor")).toContainText("fixture custom");
  // A single click or tap on a tag opens it for editing; Escape leaves it as it was.
  const entry = dialog.locator(".tag-entry input");
  const tag = dialog.getByRole("button", {
    name: "编辑标签 artist:fixture custom",
    exact: true,
  });
  await tag.click();
  await expect(entry).toHaveValue("fixture custom");
  await expect(entry).toBeFocused();
  await entry.press("Escape");
  await expect(entry).toHaveValue("");
  await expect(dialog).toBeVisible();
  await expect(tag).toBeFocused();
  // From the keyboard: F2 edits, Delete removes.
  await tag.press("F2");
  await expect(entry).toHaveValue("fixture custom");
  await entry.press("Escape");
  await tag.press("Delete");
  await expect(dialog.locator(".tag-editor")).not.toContainText(
    "fixture custom",
  );
  await dialog
    .getByRole("button", { name: "关闭", exact: true })
    .first()
    .click();
});
test("download is recorded before remote completion and asks for approval", async ({
  page,
}) => {
  await page.getByRole("link", { name: "下载", exact: true }).click();
  await page
    .getByRole("textbox", { name: "作品链接" })
    .fill("https://e-hentai.org/g/100/abcdef/");
  await page.getByRole("button", { name: "添加", exact: true }).click();
  await expect(page.locator(".download-row")).toHaveCount(1);
  await expect(page.locator(".download-approval")).toBeVisible({
    timeout: 15000,
  });
  await expect(page.locator(".download-title a").first()).toHaveAttribute(
    "href",
    "https://e-hentai.org/g/100/abcdef/",
  );
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
test("rapid turns retain the old frame, show a preview and settle on decoded full-size pages", async ({
  page,
}, info) => {
  test.skip(
    info.project.name === "mobile",
    "Keyboard page turns are a desktop interaction",
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
    const book = catalog.items.find(
      (item: { content: { page_count: number } }) =>
        item.content.page_count === 22,
    );
    expect(book).toBeDefined();
    await page.goto(`/#book/${book.id}`);
    await page.locator(".page-thumbnails a").first().click();
    await expect(page.locator(".frame-ready")).toBeVisible();
    await page.locator(".reader-name strong").click();
    // Quick separate presses: each turns a page while none of the pages loads.
    const target = 6;
    for (let i = 0; i < target; i++) await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("slider", { name: "阅读位置" })).toHaveValue(
      String(target),
    );
    await expect(
      page.locator(".frame-loading .retained-frame img"),
    ).toBeVisible();
    await expect(page.locator(".reader-preview img")).toHaveAttribute(
      "src",
      new RegExp(`/thumbnails/${target}(\\?|$)`),
    );
    expect(requests.length).toBeLessThan(target);
    release();
    await expect(
      page.locator(".frame-ready .retained-frame img"),
    ).toHaveAttribute("alt", `第 ${target + 1} 页`);
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
  await page.getByRole("link", { name: "设置", exact: true }).click();
  await expect(
    page
      .locator(".settings-section")
      .filter({ hasText: "书籍去重" })
      .locator("summary small"),
  ).toContainText("已分析");
  await expect(page.locator(".settings-category > summary")).toHaveCount(0);
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

test("a thumbnail page link is consumed and reload resumes the latest page", async ({
  page,
}) => {
  await openBook(page);
  // Earlier tests in this worker may have finished the shared book, and a finished book saves no
  // progress; start it unread.
  const id = new URL(page.url()).hash.split("/")[1];
  const book = await (await page.request.get(`/api/books/${id}`)).json();
  const reset = await page.request.put(
    `/api/books/${id}/personal?expected_revision=${book.personal_revision}`,
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
  await page.reload();
  await expect(page.locator(".book-intro")).toBeVisible();
  await page.getByRole("link", { name: "阅读第 1 页", exact: true }).click();
  await expect(page).toHaveURL(/#read\/[^/]+$/);
  const position = page.getByRole("slider", { name: "阅读位置" });
  await expect(position).toHaveValue("0");
  await page.keyboard.press("ArrowRight");
  await expect(position).toHaveValue("1");
  await expect
    .poll(async () => {
      const id = new URL(page.url()).hash.split("/")[1];
      const response = await page.request.get(`/api/books/${id}`);
      return (await response.json()).personal.resume_page;
    })
    .toBe(1);
  await page.reload();
  await expect(position).toHaveValue("1");
});

test("bulk download approval uses the confirmation dialog", async ({
  page,
}, info) => {
  const first = info.project.name === "mobile" ? 8100 : 8200;
  const response = await page.request.post("/api/downloads/batch", {
    headers: {
      "X-MangaCouch-Request": "1",
      Origin: new URL(page.url()).origin,
    },
    data: {
      urls: [first, first + 1].map(
        (id) => `https://e-hentai.org/g/${id}/abcdef/`,
      ),
    },
  });
  expect(response.ok()).toBeTruthy();
  const { items } = await response.json();
  await page.goto("/#downloads");
  for (const { job } of items) {
    const row = page.locator(`#download-${job.id}`);
    await expect(
      row.getByRole("button", { name: "确认下载 (25 GP)", exact: true }),
    ).toBeVisible({ timeout: 15000 });
    await row.getByRole("checkbox").check();
  }
  await page.getByRole("button", { name: /确认下载 2 项/ }).click();
  const dialog = page.getByRole("dialog", { name: "确认批量下载" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("50 GP");
  await dialog.getByRole("button", { name: "确认下载", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByText("已完成 2 个，0 个失败", { exact: true }),
  ).toBeVisible();
});

test("duplicate links open a new tab and returning restores expanded settings and page", async ({
  page,
  context,
}, info) => {
  const response = await page.request.get("/api/books?limit=4");
  const { items: books } = await response.json();
  await page.route("**/api/duplicates?*", async (route) => {
    const after = new URL(route.request().url()).searchParams.get("after");
    const pair = after ? books.slice(2, 4) : books.slice(0, 2);
    await route.fulfill({
      json: {
        items: [
          {
            left_id: pair[0].id,
            right_id: pair[1].id,
            books: pair,
            matched_pages: 3,
            coverage: 1,
          },
        ],
        after: after ? null : "page-two",
        comparing: { done: 4, total: 4 },
        indexed_pages: 12,
        total_pages: 12,
      },
    });
  });
  await page.goto("/#settings/duplicates");
  const section = page.locator(".settings-section").filter({
    has: page.getByRole("heading", { name: "书籍去重", exact: true }),
  });
  await expect(section).toHaveAttribute("open", "");
  await section.getByRole("button", { name: "下一页", exact: true }).click();
  const link = section.locator(".duplicate-cover").first();
  const href = await link.getAttribute("href");
  expect(href).toBe(`#book/${books[2].id}`);
  if (info.project.name === "desktop") {
    const popup = context.waitForEvent("page");
    await link.click({ modifiers: ["ControlOrMeta"] });
    const other = await popup;
    await expect(other).toHaveURL(new RegExp(`#book/${books[2].id}$`));
    await expect(page).toHaveURL(/#settings\/duplicates$/);
    await other.close();
  }
  await link.scrollIntoViewIfNeeded();
  const scrollBefore = await page.evaluate(() => window.scrollY);
  await link.click();
  await expect(page.locator(".book-intro")).toBeVisible();
  await page.locator(".back-link").click();
  await expect(section).toHaveAttribute("open", "");
  await expect(section.locator(".duplicate-cover").first()).toHaveAttribute(
    "href",
    href!,
  );
  await expect
    .poll(async () =>
      Math.abs((await page.evaluate(() => window.scrollY)) - scrollBefore),
    )
    .toBeLessThan(20);
  await page.screenshot({
    path: `/tmp/mangacouch-return-${info.project.name}.png`,
    fullPage: true,
  });
});

test("metadata suggests a complete tag from just its value", async ({
  page,
}, info) => {
  await openBook(page);
  await menu(page, "编辑元数据");
  const dialog = page.getByRole("dialog");
  const input = dialog.getByRole("combobox", { name: "标签", exact: true });
  await input.fill("blue");
  const option = dialog.getByRole("option").first();
  await expect(option).toBeVisible();
  await expect(option).toHaveAttribute("title", /^parody:blue/);
  const tag = await option.getAttribute("title");
  await option.click();
  await expect(
    dialog.getByRole("button", { name: `编辑标签 ${tag}`, exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: `/tmp/mangacouch-tags-${info.project.name}.png`,
    fullPage: true,
  });
});

test("download actions preserve the last page and clamp after its rows leave", async ({
  page,
}, info) => {
  const first = info.project.name === "mobile" ? 30000 : 40000;
  const response = await page.request.post("/api/downloads/batch", {
    headers: {
      "X-MangaCouch-Request": "1",
      Origin: new URL(page.url()).origin,
    },
    data: {
      urls: Array.from(
        { length: 41 },
        (_, i) => `https://e-hentai.org/g/${first + i}/abcdef/`,
      ),
    },
  });
  expect(response.ok()).toBeTruthy();
  await page.goto("/#downloads");
  await page.getByRole("button", { name: "末页", exact: true }).click();
  const pagination = page.getByRole("button", { name: "跳转页码" });
  const before = await pagination.innerText();
  const previousPage = Number(before.split("/")[0].trim());
  expect(previousPage).toBeGreaterThan(1);
  await page.locator(".download-tools input[type=checkbox]").check();
  // These inspected tasks await approval; pause must not submit an empty batch.
  await expect(
    page
      .locator(".download-tools")
      .getByRole("button", { name: "暂停", exact: true }),
  ).toBeDisabled();
  await expect(pagination).toHaveText(before);
  await page
    .locator(".download-tools")
    .getByRole("button", { name: "取消", exact: true })
    .click();
  await expect(pagination).toHaveText(
    `${previousPage - 1} / ${previousPage - 1}`,
  );
  await expect(page.locator(".download-row")).toHaveCount(20);
  await page.screenshot({
    path: `/tmp/mangacouch-downloads-${info.project.name}.png`,
    fullPage: true,
  });
});
