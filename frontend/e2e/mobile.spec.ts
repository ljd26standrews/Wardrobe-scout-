import { expect, test } from "@playwright/test";
import path from "node:path";

const SHOTS = process.env.E2E_SCREENSHOT_DIR;

test("iPhone: discover layout, gallery swipe area, bottom navigation", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("discover-card")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Mobile" })).toBeVisible();
  await expect(page.getByTestId("action-love")).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "discover-iphone.png") });
  await page.getByTestId("action-maybe").click();
  await expect(page.getByText("Saved to Maybe")).toBeVisible();
  await page.getByRole("button", { name: "More" }).click();
  await page.getByRole("link", { name: "Sellers" }).click();
  await expect(page).toHaveURL(/sellers/);
  const overflow2 = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow2).toBeLessThanOrEqual(0);
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, "sellers-iphone.png"), fullPage: true });
});
