import { test, expect } from "./fixtures";

test("audit layout, queue order and collection draft protection", async ({
  page,
}, info) => {
  await page.route("**/api/tasks", async (route) => {
    const response = await route.fetch();
    const value = await response.json();
    value.downloads.needs_you = 2;
    await route.fulfill({ response, json: value });
  });
  await page.goto("/");
  await page
    .getByLabel("书库口令", { exact: true })
    .fill("browser-fixture-only");
  await page.getByRole("button", { name: "打开书库" }).click();
  await expect(page.locator(".book-card").first()).toBeVisible();
  const attention = page.locator(".attention-trigger");
  const tasks = page.locator(".task-trigger");
  await expect(attention).toBeVisible();
  expect((await attention.boundingBox())!.x).toBeLessThan(
    (await tasks.boundingBox())!.x,
  );
  const columns = await page
    .locator(".book-grid")
    .evaluate(
      (el) => getComputedStyle(el).gridTemplateColumns.split(" ").length,
    );
  expect(columns).toBe(info.project.name === "mobile" ? 2 : 6);
  await page.screenshot({ path: `/tmp/mc-${info.project.name}-library.png` });
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(page.locator(".book-card").first()).toHaveCSS(
    "color",
    "rgb(228, 236, 229)",
  );
  await page.screenshot({ path: `/tmp/mc-${info.project.name}-dark.png` });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/#settings");
  await expect(page.locator(".settings-category").first()).toBeVisible();
  await page.screenshot({ path: `/tmp/mc-${info.project.name}-settings.png` });
  await page.goto("/#collections");
  await page.getByRole("button", { name: "新建合集", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("名称", { exact: true }).fill("未保存的合集");
  await page.mouse.click(2, 2);
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog.getByText("放弃未保存的修改？")).toBeVisible();
  await dialog.getByRole("button", { name: "继续编辑", exact: true }).click();
  await expect(dialog.getByLabel("名称", { exact: true })).toHaveValue(
    "未保存的合集",
  );
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await dialog.getByRole("button", { name: "放弃修改", exact: true }).click();
  await expect(dialog).not.toBeVisible();
});
