import { expect, test } from "@playwright/test";
import path from "node:path";

const SHOTS = process.env.E2E_SCREENSHOT_DIR;
const shot = async (page: import("@playwright/test").Page, name: string) => {
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: false });
};

test.describe.configure({ mode: "serial" });

test("discover: love a product and find it in Saved", async ({ page }) => {
  await page.goto("/");
  const card = page.getByTestId("discover-card");
  await expect(card).toBeVisible();
  const title = await page.getByTestId("product-title").innerText();
  await expect(card.locator("img").first()).toBeVisible();
  await expect(page.getByText("Quality assessment")).toBeVisible();
  await shot(page, "discover-desktop");
  await page.getByTestId("action-love").click();
  await expect(page.getByText("Loved — strong signal recorded")).toBeVisible();
  await expect(page.getByTestId("product-title")).not.toHaveText(title);
  await page.goto("/saved");
  await expect(page.getByRole("tab", { name: /Loved/ })).toBeVisible();
  await expect(page.getByText(title, { exact: false }).first()).toBeVisible();
});

test("discover: reason-aware dislike sets a value ceiling, not a taste dislike", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("discover-card")).toBeVisible();
  await page.getByTestId("action-dislike").click();
  await page.getByTestId("reason-too_expensive").click();
  await shot(page, "reject-dialog");
  await page.getByTestId("reject-submit").click();
  await expect(page.getByText("Recorded as a value ceiling, not a style dislike.")).toBeVisible();
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

test("grid view and browse search", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Grid view" }).click();
  await expect(page.getByTestId("product-card").first()).toBeVisible();
  await page.getByRole("button", { name: "Card view" }).click();
  await page.goto("/browse");
  await expect(page.getByTestId("product-card").first()).toBeVisible();
  await page.getByTestId("browse-search").fill("cardigan");
  await expect(page.getByTestId("product-card")).toHaveCount(1);
  await shot(page, "browse");
});

test("product page: add independent evidence changes the quality label", async ({ page }) => {
  await page.goto("/browse");
  await page.getByTestId("browse-search").fill("cardigan");
  await page.getByTestId("product-card").first().getByRole("link").first().click();
  await expect(page.getByText("Unverified").first()).toBeVisible();
  for (let i = 0; i < 2; i++) {
    await page.getByRole("button", { name: "Add evidence" }).click();
    await page.getByPlaceholder(/QC photos show/).fill("QC photos show neat linking at the cuffs and genuine shell buttons");
    await page.getByPlaceholder("https://…").fill("https://example.org/qc-thread");
    await page.getByRole("button", { name: "Save evidence" }).click();
    await expect(page.getByText("Evidence added").first()).toBeVisible();
  }
  await expect(page.getByText("Independent buyer report").first()).toBeVisible();
  await expect(page.getByText(/Limited evidence|Evidence-backed/).first()).toBeVisible();
  await shot(page, "product-detail");
});

test("compare shows same-photo alternative across sellers", async ({ page }) => {
  await page.goto("/browse");
  await page.getByTestId("browse-search").fill("two-way zip wool jacket");
  await page.getByTestId("product-card").first().getByRole("link").first().click();
  await page.locator('main a[href^="/compare?id="]').first().click();
  await expect(page.getByText("Same photographs — likely same source/batch (unconfirmed)")).toBeVisible();
  await expect(page.getByText(/appears to be the same product/)).toBeVisible();
  await shot(page, "compare");
});

test("sellers dashboard reports genuine scan outcomes", async ({ page }) => {
  await page.goto("/sellers");
  const rows = page.getByTestId("seller-row");
  await expect(rows.filter({ hasText: "Demo Seller A" })).toContainText("Succeeded");
  await expect(rows.filter({ hasText: "MadeByKungFu" })).toContainText(/Failed|Blocked/);
  await expect(page.getByText(/products genuinely collected by scans/)).toBeVisible();
  await shot(page, "sellers");
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

test("settings: adjust a ranking weight", async ({ page }) => {
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Ranking" })).toBeVisible();
  const slider = page.getByRole("slider", { name: "visual" });
  await slider.focus();
  await page.keyboard.press("ArrowRight");
  await page.getByRole("button", { name: "Save" }).nth(1).click();
  await expect(page.getByText("Ranking weights saved")).toBeVisible();
});
