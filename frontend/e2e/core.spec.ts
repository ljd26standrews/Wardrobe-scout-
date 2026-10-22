import { expect, test } from "@playwright/test";
import path from "node:path";

const SHOTS = process.env.E2E_SCREENSHOT_DIR;
const shot = async (page: import("@playwright/test").Page, name: string) => {
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: false });
};

test.describe.configure({ mode: "serial" });

test("discover: love a piece and find it in Saved", async ({ page }) => {
  await page.goto("/");
  const card = page.getByTestId("discover-card");
  await expect(card).toBeVisible();
  const title = await page.getByTestId("product-title").innerText();
  await expect(card.locator("img").first()).toBeVisible();
  // Quality and sizing are one step away, not crowded onto the decision view
  await expect(page.getByTestId("open-details")).toBeVisible();
  await shot(page, "discover-desktop");
  await page.getByTestId("action-love").click();
  await expect(page.getByText("Loved", { exact: true })).toBeVisible();
  await expect(page.getByTestId("product-title")).not.toHaveText(title);
  await page.goto("/saved");
  await expect(page.getByTestId("list-loved")).toContainText("1");
  await expect(page.getByText(title, { exact: false }).first()).toBeVisible();
});

test("discover: a dislike is recorded at once; a reason can follow, and buying reasons stay apart from taste", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("discover-card")).toBeVisible();
  const title = await page.getByTestId("product-title").innerText();
  await page.getByTestId("action-dislike").click();
  await expect(page.getByTestId("product-title")).not.toHaveText(title); // no dialog stands in the way
  await page.getByRole("button", { name: "Add a reason" }).click();
  await page.getByTestId("reason-too_expensive").click();
  await shot(page, "reasons-sheet");
  await page.getByTestId("reject-submit").click();
  await expect(page.getByText("Reasons about buying or quality don't count as disliking the look.")).toBeVisible();
  await page.goto("/insights");
  await expect(page.getByText("Value ceilings")).toBeVisible();
  await expect(page.getByText(/unproven quality above/).first()).toBeVisible();
  await expect(page.getByText("Too expensive for the quality").first()).toBeVisible();
  await shot(page, "insights");
});

test("keyboard shortcuts and skip", async ({ page }) => {
  await page.goto("/");
  const t1 = await page.getByTestId("product-title").innerText();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByTestId("product-title")).not.toHaveText(t1);
  const t2 = await page.getByTestId("product-title").innerText();
  await page.keyboard.press("s");
  await expect(page.getByText("Saved to your shortlist")).toBeVisible();
  await expect(page.getByTestId("product-title")).not.toHaveText(t2);
});

test("discover: a decision re-ranks the queue at once, and Undo puts the piece back", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("discover-card")).toBeVisible();
  const title = await page.getByTestId("product-title").innerText();
  const reranked = page.waitForRequest((r) => r.method() === "GET" && /\/api\/feed\?/.test(r.url()));
  await page.getByTestId("action-like").click();
  await reranked; // not ~22 cards later
  await expect(page.getByTestId("product-title")).not.toHaveText(title);
  await page.getByTestId("undo-last").click();
  await expect(page.getByText("Undone")).toBeVisible();
  await expect(page.getByTestId("product-title")).toHaveText(title);
});

test("grid view, side panel with a shareable address, and Browse search", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("radio", { name: "Grid" }).click();
  const tile = page.getByTestId("product-card").first();
  await expect(tile).toBeVisible();
  await tile.getByRole("link").first().click();
  const panel = page.getByTestId("piece-panel");
  await expect(panel).toBeVisible();
  await expect(page).toHaveURL(/[?&]piece=\d+/);
  await shot(page, "discover-grid-panel");
  await page.reload(); // the address opens the same piece
  await expect(page.getByTestId("piece-panel")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("piece-panel")).toHaveCount(0);
  await expect(page).not.toHaveURL(/piece=/);
  await page.getByTestId("product-card").first().getByRole("link").first().click();
  await expect(page.getByTestId("piece-panel")).toBeVisible();
  await page.goBack(); // Back closes the panel and keeps the grid
  await expect(page.getByTestId("piece-panel")).toHaveCount(0);
  await expect(page.getByTestId("discover-grid")).toBeVisible();
  await page.getByRole("radio", { name: "One at a time" }).click();

  await page.goto("/browse");
  await expect(page.getByTestId("product-card").first()).toBeVisible();
  await page.getByTestId("browse-search").fill("cardigan");
  await expect(page.getByTestId("product-card")).toHaveCount(1);
  await expect(page).toHaveURL(/q=cardigan/); // a reload or Back returns the same results
  await shot(page, "browse");
});

test("product details: adding independent evidence changes the quality label", async ({ page }) => {
  await page.goto("/browse?q=cardigan");
  await page.getByTestId("product-card").first().getByRole("link").first().click();
  const panel = page.getByTestId("piece-panel");
  await expect(panel.getByText("Unverified").first()).toBeVisible();
  for (let i = 0; i < 2; i++) {
    await panel.getByRole("button", { name: "Add evidence" }).click();
    await page.getByPlaceholder(/QC photos show/).fill("QC photos show neat linking at the cuffs and genuine shell buttons");
    await page.getByPlaceholder("https://…").fill(`https://example.org/qc-thread-${i + 1}`); // two separate reports
    await page.getByRole("button", { name: "Save evidence" }).click();
    await expect(page.getByText("Evidence added").first()).toBeVisible();
  }
  await expect(panel.getByText("Independent buyer report").first()).toBeVisible();
  await expect(panel.getByText(/Limited evidence|Evidence-backed/).first()).toBeVisible();
  await shot(page, "product-panel");
  await panel.getByRole("link", { name: /Open full page/ }).click();
  await expect(page).toHaveURL(/\/product\/\d+/);
  await expect(page.getByRole("heading", { name: "Sizing" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Where to buy" })).toBeVisible();
  await shot(page, "product-detail");
});

test("compare shows same-photo alternative across sellers", async ({ page }) => {
  // Pick the listing that has a shared-photo match. Browse order depends on ranking, which earlier tests'
  // feedback changes, so "the first search result" made this test flaky.
  const found = await (await page.request.get("/api/products?q=two-way%20zip%20wool%20jacket&page_size=12")).json();
  let target: number | null = null;
  for (const p of found.items as { id: number }[]) {
    const cmp = await (await page.request.get(`/api/products/${p.id}/compare`)).json();
    if (JSON.stringify(cmp).includes("shared_photos")) {
      target = p.id;
      break;
    }
  }
  expect(target, "a jacket listing with a shared-photo match").not.toBeNull();
  await page.goto(`/product/${target}`);
  await page.locator('main a[href^="/compare?id="]').first().click();
  await expect(page.getByText(/Several identical photographs/).first()).toBeVisible();
  await expect(page.getByText(/appears to be the same product/)).toBeVisible();
  await shot(page, "compare");
});

test("saved: filter by list, edit purchase notes in place, compare two pieces, remove with Undo", async ({ page }) => {
  await page.goto("/saved");
  await expect(page.getByTestId("saved-grid")).toBeVisible();
  await page.getByTestId("list-loved").click();
  await expect(page).toHaveURL(/list=loved/);
  for (const label of await page.getByTestId("saved-list-label").allInnerTexts()) expect(label).toContain("Loved");
  await page.getByTestId("list-all").click();
  const first = page.getByTestId("product-card").first();
  const title = await first.getByTestId("product-title").innerText();

  await first.getByTestId("saved-edit").click();
  await first.getByTestId("saved-size").fill("M");
  await first.getByTestId("saved-note").fill("Ask for the shoulder width");
  await first.getByTestId("saved-list-select").selectOption("wishlist");
  await first.getByTestId("saved-save").click();
  await expect(page.getByText("Moved to Wishlist")).toBeVisible();
  const card = page.getByTestId("product-card").filter({ hasText: title });
  await expect(card.getByTestId("saved-notes")).toContainText("Size to check: M");
  await expect(card.getByTestId("saved-list-label")).toContainText("Wishlist");
  await shot(page, "saved");

  const picks = page.getByTestId("saved-pick");
  await picks.nth(0).check();
  await picks.nth(1).check();
  await page.getByTestId("compare-go").click();
  await expect(page).toHaveURL(/\/compare\?ids=\d+,\d+/);
  await expect(page.getByTestId("pieces-compare").locator("thead th[scope=col]")).toHaveCount(2);
  await expect(page.getByText("Your notes")).toBeVisible();
  await shot(page, "compare-pieces");

  await page.goto("/saved");
  await card.getByTestId("saved-edit").click();
  await card.getByTestId("saved-remove").click();
  await expect(page.getByText(/^Removed “/)).toBeVisible();
  await expect(page.getByTestId("product-card").filter({ hasText: title })).toHaveCount(0);
  await page.locator("[data-sonner-toast]").filter({ hasText: "Removed" }).getByRole("button", { name: "Undo" }).click();
  await expect(card.getByTestId("saved-notes")).toContainText("Size to check: M"); // back exactly as it was
  await expect(card.getByTestId("saved-list-label")).toContainText("Wishlist");
});

test("sellers dashboard reports genuine scan outcomes", async ({ page }) => {
  await page.goto("/sellers");
  const rows = page.getByTestId("seller-row");
  await expect(rows.filter({ hasText: "Demo Seller A" })).toContainText("Succeeded");
  await expect(rows.filter({ hasText: "Demo Seller D" })).toContainText(/Failed|Blocked/); // nothing listens there
  await expect(rows.filter({ hasText: "MadeByKungFu" })).toContainText("Paused"); // real shops are not contacted in tests
  await expect(page.getByText(/pieces collected by scans/)).toBeVisible();
  await shot(page, "sellers");
});

test("scan panel shows catalogue coverage from the server", async ({ page }) => {
  await page.goto("/sellers");
  const panel = page.getByTestId("scan-panel");
  await expect(panel.getByText("Catalogue coverage")).toBeVisible();
  // The seller may appear twice (active scan card and coverage list) while the demo scan's last steps run.
  await expect(panel.getByText(/Demo Seller A/).first()).toBeVisible();
  await expect(panel.getByText(/analysed/).first()).toBeVisible();
  // Survives a reload: the state lives on the server, not in this tab
  await page.reload();
  await expect(page.getByTestId("scan-panel").getByText("Catalogue coverage")).toBeVisible();
});

test("wardrobe: add an owned piece with a photo", async ({ page }) => {
  await page.goto("/wardrobe");
  await page.getByTestId("add-wardrobe").click();
  await page.locator('input[name="name"]').fill("White Vince tee");
  await page.locator('select[name="category"]').selectOption("t-shirt");
  await page.locator('input[name="colours"]').fill("white");
  await page.locator('input[name="image"]').setInputFiles(path.join(__dirname, "fixtures", "tee.jpg"));
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByText("Added to wardrobe")).toBeVisible();
  await expect(page.getByText("White Vince tee")).toBeVisible();
  await page.getByRole("tab", { name: "Gaps & redundancy" }).click();
  await expect(page.getByText("Missing essentials")).toBeVisible();
  await shot(page, "wardrobe");
});

test("settings: adjust a ranking weight; the photo analysis in use is shown", async ({ page }) => {
  await page.goto("/settings");
  await expect(page.getByTestId("photo-backend")).toContainText(/Lightweight features|CLIP/);
  await expect(page.getByRole("heading", { name: "Ranking" })).toBeVisible();
  const slider = page.getByRole("slider", { name: "visual" });
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await page.getByRole("button", { name: "Save" }).nth(1).click();
  await expect(page.getByText("Ranking weights saved")).toBeVisible();
});
