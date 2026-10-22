import { expect, test } from "@playwright/test";
import path from "node:path";

const SHOTS = process.env.E2E_SCREENSHOT_DIR;
const shot = async (page: import("@playwright/test").Page, name: string) => {
  if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: false });
};

test.describe.configure({ mode: "serial" });

// The demo catalogue has only a handful of decisions, so approval estimates are not yet calibrated.
test("discover: a strict mode explains why it is empty instead of padding the feed", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("gate-status")).toContainText("Your taste ranking");
  await expect(page.getByTestId("discover-card")).toBeVisible(); // Balanced: the taste ranking, labelled
  await expect(page.getByTestId("approval-estimate")).toHaveCount(0); // no percentages without a calibration
  await shot(page, "discover-balanced-uncalibrated");

  await page.getByTestId("mode-high").click();
  await expect(page.getByText("High confidence needs match estimates first")).toBeVisible();
  await expect(page.getByTestId("gate-message")).toContainText("needs more of your likes and dislikes");
  await expect(page.getByTestId("discover-card")).toHaveCount(0);
  await shot(page, "discover-high-uncalibrated");

  await page.getByTestId("tab-learning").click();
  await expect(page.getByText(/Pieces the app is unsure about|No learning picks right now/).first()).toBeVisible();
  await shot(page, "discover-learning");

  await page.getByTestId("tab-foryou").click();
  await page.getByTestId("mode-balanced").click();
  await expect(page.getByTestId("discover-card")).toBeVisible();
});

test("insights shows observed approval and calibration; settings has the selective controls", async ({ page }) => {
  await page.goto("/insights");
  await expect(page.getByTestId("selective-insights")).toBeVisible();
  await expect(page.getByText("Observed approval of what was served")).toBeVisible();
  await expect(page.getByTestId("observed-rate").first()).toBeVisible();
  await shot(page, "insights-selective");
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Selective recommendation" })).toBeVisible();
  await expect(page.getByRole("switch", { name: "Selective recommendation" })).toBeChecked();
});
