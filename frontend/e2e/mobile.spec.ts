import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

const SHOTS = process.env.E2E_SCREENSHOT_DIR;

/** Controls smaller than 44 x 44 px inside `scope` (touch screens: Apple's minimum target). */
async function smallTargets(page: Page, scope: string) {
  return page.evaluate((sel) => {
    const out: string[] = [];
    for (const root of Array.from(document.querySelectorAll(sel))) {
      for (const el of Array.from(root.querySelectorAll<HTMLElement>("button, a[href], [role=radio], [role=tab]"))) {
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        if (r.width < 43.5 || r.height < 43.5) out.push(`${(el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 30)} ${Math.round(r.width)}x${Math.round(r.height)}`);
      }
    }
    return out;
  }, scope);
}

test("iPhone: Discover layout, bottom navigation, decisions within thumb reach", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("discover-card")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Mobile" })).toBeVisible();
  await expect(page.getByTestId("action-love")).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "discover-iphone.png") });
  expect(await smallTargets(page, 'nav[aria-label="Mobile"]')).toEqual([]);
  expect(await smallTargets(page, '[data-testid="discover-card"] .fixed')).toEqual([]); // the decision bar
  const title = await page.getByTestId("product-title").innerText();
  await page.getByTestId("action-later").tap();
  await expect(page.getByText("Saved to Later: you like the look, not buying now")).toBeVisible();
  await expect(page.getByTestId("product-title")).not.toHaveText(title);
  await page.getByTestId("undo-last-mobile").tap(); // Undo stays one tap away on a phone
  await expect(page.getByTestId("product-title")).toHaveText(title);

  await page.getByRole("button", { name: "More" }).tap();
  await page.getByRole("link", { name: "Sellers" }).tap();
  await expect(page).toHaveURL(/sellers/);
  const overflow2 = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow2).toBeLessThanOrEqual(0);
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "sellers-iphone.png"), fullPage: true });
});

test("iPhone: a piece opens as its own page, and Back returns to the list", async ({ page }) => {
  await page.goto("/browse");
  const tile = page.getByTestId("product-card").first();
  await expect(tile).toBeVisible();
  expect(await smallTargets(page, '[data-testid="product-card"]')).toEqual([]);
  await tile.getByRole("link").first().tap();
  await expect(page).toHaveURL(/\/product\/\d+/); // no side panel on a phone
  await expect(page.getByRole("heading", { name: "Sizing" })).toBeVisible();
  await page.getByRole("button", { name: "Back" }).tap();
  await expect(page).toHaveURL(/\/browse/);
});
