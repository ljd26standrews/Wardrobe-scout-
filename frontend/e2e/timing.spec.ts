import { expect, test, type Page } from "@playwright/test";

// Timing cases: decisions faster than the network, answers that arrive late, a save that fails, a reload during a
// scan and a setting changed elsewhere while Discover is open. They run after the other specs (their decisions
// change what Discover shows).
test.describe.configure({ mode: "serial" });

const card = (page: Page) => page.getByTestId("discover-card");
const shownId = async (page: Page) => Number(await card(page).getAttribute("data-product-id"));

async function savedIds(page: Page): Promise<Map<number, string>> {
  const body = await (await page.request.get("/api/saved")).json();
  return new Map((body.items as { id: number; saved: { list: string } }[]).map((i) => [i.id, i.saved.list]));
}

test("fast decisions: several in quick succession are all recorded and none comes back", async ({ page }) => {
  await page.goto("/");
  await expect(card(page)).toBeVisible();
  const ids: number[] = [];
  for (let i = 0; i < 4; i++) {
    const id = await shownId(page);
    ids.push(id);
    await page.keyboard.press("s"); // the next piece shows at once; the save happens in the background
    await expect(card(page)).not.toHaveAttribute("data-product-id", String(id));
  }
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(500);
  expect(ids).not.toContain(await shownId(page));
  const saved = await savedIds(page);
  for (const id of ids) expect(saved.get(id), `piece ${id} saved`).toBe("liked");
});

test("a late answer never brings back a piece you decided on", async ({ page }) => {
  await page.goto("/");
  await expect(card(page)).toBeVisible();
  // Every feed answer is held back for 2.5 s after the server gave it, so an answer computed before a decision
  // arrives after that decision.
  await page.route(/\/api\/feed\?/, async (route) => {
    const response = await route.fetch();
    await new Promise((r) => setTimeout(r, 2500));
    await route.fulfill({ response });
  });
  const a = await shownId(page);
  await page.keyboard.press("s"); // the refresh after this one (still listing B) is held back
  await expect(card(page)).not.toHaveAttribute("data-product-id", String(a));
  const b = await shownId(page);
  // B's own save is slow too, so the held answer that still lists B is the newest one when it lands.
  await page.route("**/api/feedback", async (route) => {
    await new Promise((r) => setTimeout(r, 4000));
    await route.continue();
  });
  await page.keyboard.press("s");
  await expect(card(page)).not.toHaveAttribute("data-product-id", String(b));
  await page.waitForTimeout(3000); // the held answer listing B has arrived
  expect([a, b]).not.toContain(await shownId(page));
  await page.waitForTimeout(6000); // B's save and the refresh after it have landed
  expect([a, b]).not.toContain(await shownId(page));
  await page.unrouteAll({ behavior: "ignoreErrors" });
  const saved = await savedIds(page);
  expect(saved.get(a)).toBe("liked");
  expect(saved.get(b)).toBe("liked");
});

test("a save that fails rolls back: the piece returns and an error says so", async ({ page }) => {
  await page.goto("/");
  await expect(card(page)).toBeVisible();
  await page.route("**/api/feedback", (route) => (route.request().method() === "POST" ? route.fulfill({ status: 500, body: "{}" }) : route.continue()), {
    times: 1,
  });
  const id = await shownId(page);
  await page.keyboard.press("s");
  await expect(page.getByText(/^Not saved:/)).toBeVisible();
  await expect(card(page)).toHaveAttribute("data-product-id", String(id)); // back where it was
  expect((await savedIds(page)).has(id)).toBe(false);
});

test("reloading during a scan keeps its progress (it lives on the server)", async ({ page }) => {
  await page.goto("/sellers");
  await page.getByRole("button", { name: "Scan all" }).click();
  await page.reload();
  await expect(page.getByTestId("scan-panel")).toBeVisible();
  await expect(page.getByTestId("scan-panel-active").or(page.getByTestId("scan-panel-last")).first()).toBeVisible();
  await expect(page.getByTestId("scan-panel-last")).toBeVisible({ timeout: 90_000 }); // and it finishes
});

test("switching selective recommendation off elsewhere is reflected on Discover's next answer", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("mode-balanced")).toBeVisible();
  await expect(card(page)).toBeVisible(); // the first answer has arrived (it was computed before the change)
  const settings = await (await page.request.get("/api/settings")).json();
  try {
    expect((await page.request.put("/api/settings/selective", { data: { ...settings.selective, enabled: false } })).ok()).toBe(true);
    await page.keyboard.press("n"); // skip: the next answer comes from the server
    await expect(page.getByTestId("gate-status")).toContainText("ranked for your taste");
    // The controls of a feature that is now off are gone, so a click cannot quietly switch it back on.
    await expect(page.getByTestId("mode-balanced")).toHaveCount(0);
    await expect(page.getByTestId("tab-learning")).toHaveCount(0);
  } finally {
    await page.request.put("/api/settings/selective", { data: { ...settings.selective, enabled: true } });
  }
});
