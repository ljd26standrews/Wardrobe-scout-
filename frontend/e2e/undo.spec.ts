import { expect, test, type Page } from "@playwright/test";

// Undo: it takes back the decision you made last (not the one whose answer arrived last), puts a piece's saved details
// back exactly, says when it failed and lets you retry, and never overwrites a newer decision made elsewhere. Runs after
// the other specs, on the synthetic demo catalogue.
test.describe.configure({ mode: "serial" });

const card = (page: Page) => page.getByTestId("discover-card");
const shownId = async (page: Page) => Number(await card(page).getAttribute("data-product-id"));

type SavedDetails = { list: string; priority: number; note: string | null; target_size?: string | null };
async function savedOf(page: Page, id: number): Promise<SavedDetails | null> {
  const body = await (await page.request.get("/api/saved")).json();
  const item = (body.items as { id: number; saved: SavedDetails }[]).find((i) => i.id === id);
  return item ? { list: item.saved.list, priority: item.saved.priority, note: item.saved.note, target_size: item.saved.target_size ?? null } : null;
}

/** Undecided pieces, newest first: a fresh one for each test. */
async function undecided(page: Page, n: number): Promise<number[]> {
  const body = await (await page.request.get("/api/products?sort=newest&page_size=200")).json();
  const history = await (await page.request.get("/api/feedback?limit=1000")).json();
  const decided = new Set((history as { product_id: number | null }[]).map((f) => f.product_id));
  const ids = (body.items as { id: number }[]).map((p) => p.id).filter((id) => !decided.has(id));
  expect(ids.length).toBeGreaterThanOrEqual(n);
  return ids.slice(0, n);
}

test("Undo takes back your last decision even when an earlier save is answered later", async ({ page }) => {
  await page.goto("/");
  await expect(card(page)).toBeVisible();
  let posts = 0;
  // The first save is answered 2.5 s late, so the second decision's answer arrives first.
  await page.route("**/api/feedback", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const mine = ++posts;
    const response = await route.fetch();
    if (mine === 1) await new Promise((r) => setTimeout(r, 2500));
    await route.fulfill({ response });
  });
  const a = await shownId(page);
  await page.keyboard.press("s");
  await expect(card(page)).not.toHaveAttribute("data-product-id", String(a));
  const b = await shownId(page);
  await page.keyboard.press("s");
  await expect.poll(() => posts).toBe(2);
  await page.waitForTimeout(3000); // both answers are in: B's first, A's last
  await page.keyboard.press("z");
  await expect(page.getByText("Undone", { exact: true }).first()).toBeVisible();
  await expect.poll(() => savedOf(page, b)).toBeNull(); // B, the last decision you made, is undone
  expect((await savedOf(page, a))?.list).toBe("liked"); // A stands
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.keyboard.press("z"); // and then A
  await expect.poll(() => savedOf(page, a)).toBeNull();
});

test("a double click on Undo takes back one decision", async ({ page }) => {
  await page.goto("/");
  await expect(card(page)).toBeVisible();
  const a = await shownId(page);
  await page.keyboard.press("s");
  await expect(card(page)).not.toHaveAttribute("data-product-id", String(a));
  const b = await shownId(page);
  await page.keyboard.press("s");
  await expect.poll(() => savedOf(page, b)).not.toBeNull();
  await page.route("**/api/feedback/*", async (route) => {
    await new Promise((r) => setTimeout(r, 600)); // the Undo is on its way when the second click lands
    await route.continue();
  });
  await page.getByTestId("undo-last").dblclick();
  await expect.poll(() => savedOf(page, b)).toBeNull();
  await page.waitForTimeout(1500);
  expect((await savedOf(page, a))?.list).toBe("liked");
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

test("Undo of a dislike puts back the piece's list, priority, note and size exactly", async ({ page }) => {
  const [id] = await undecided(page, 1);
  const details = { list_name: "maybe", priority: 1, note: "Measure the sleeves first", target_size: "M" };
  expect((await page.request.patch(`/api/saved/${id}`, { data: details })).ok()).toBe(true);
  const before = await savedOf(page, id);
  await page.goto(`/product/${id}`);
  await page.getByTestId("detail-dislike").click();
  await expect.poll(() => savedOf(page, id)).toBeNull();
  await page.getByRole("button", { name: "Undo" }).first().click();
  await expect(page.getByText("Undone", { exact: true }).first()).toBeVisible();
  await expect.poll(() => savedOf(page, id)).toEqual(before);
});

test("a failed Undo says so, changes nothing, and can be retried", async ({ page }) => {
  const [id] = await undecided(page, 1);
  await page.goto(`/product/${id}`);
  await page.getByTestId("detail-like").click();
  await expect.poll(() => savedOf(page, id)).not.toBeNull();
  await page.route("**/api/feedback/*", (route) => (route.request().method() === "DELETE" ? route.fulfill({ status: 500, body: "{}" }) : route.continue()), {
    times: 1,
  });
  await page.getByRole("button", { name: "Undo" }).first().click();
  await expect(page.getByText(/^Not undone/).first()).toBeVisible();
  expect((await savedOf(page, id))?.list).toBe("liked"); // nothing changed
  await page.getByRole("button", { name: "Retry" }).first().click();
  await expect(page.getByText("Undone", { exact: true }).first()).toBeVisible();
  await expect.poll(() => savedOf(page, id)).toBeNull();
});

test("Undo in one tab never overwrites a newer decision made in another", async ({ page, context }) => {
  const [id] = await undecided(page, 1);
  await page.goto(`/product/${id}`);
  await page.getByTestId("detail-like").click(); // tab 1: like, with Undo offered
  await expect.poll(() => savedOf(page, id)).not.toBeNull();
  const other = await context.newPage();
  await other.goto(`/product/${id}`);
  await other.getByTestId("detail-dislike").click(); // tab 2: changed your mind
  await expect.poll(() => savedOf(page, id)).toBeNull();
  await page.bringToFront();
  await page.getByRole("button", { name: "Undo" }).first().click(); // tab 1's Undo is out of date
  await expect(page.getByText("Not undone").first()).toBeVisible();
  await expect(page.getByText(/decided about this piece again since/).first()).toBeVisible();
  expect(await savedOf(page, id)).toBeNull(); // the dislike stands
  const history = await (await page.request.get("/api/feedback?limit=1000")).json();
  const latest = (history as { product_id: number; action: string }[]).find((f) => f.product_id === id);
  expect(latest?.action).toBe("dislike");
  await other.close();
});

test("Undo of a move in Saved puts it back, and never over a newer change", async ({ page }) => {
  const [id] = await undecided(page, 1);
  await page.request.patch(`/api/saved/${id}`, { data: { list_name: "maybe", priority: 2, note: "Ask for a fabric photo" } });
  const before = await savedOf(page, id);
  const tile = page.locator(`[data-testid="product-card"][data-product-id="${id}"]`);
  const move = async () => {
    await tile.getByTestId("saved-edit").click();
    await tile.getByTestId("saved-list-select").selectOption("wishlist");
    await tile.getByTestId("saved-save").click();
    await expect(page.getByText(/^Moved to/).first()).toBeVisible();
  };
  await page.goto("/saved");
  await move();
  await page.getByRole("button", { name: "Undo" }).first().click();
  await expect(page.getByText("Undone", { exact: true }).first()).toBeVisible();
  await expect.poll(() => savedOf(page, id)).toEqual(before); // back exactly

  await page.reload();
  await move();
  await page.request.patch(`/api/saved/${id}`, { data: { priority: 3 } }); // a newer change, e.g. in another tab
  await page.getByRole("button", { name: "Undo" }).first().click();
  await expect(page.getByText("Not undone").first()).toBeVisible();
  expect(await savedOf(page, id)).toMatchObject({ list: "wishlist", priority: 3, note: "Ask for a fabric photo" });
});
