import { expect, test } from "@playwright/test";

test("evaluate: grade a randomly picked piece, blinded, and undo it", async ({ page }) => {
  await page.goto("/evaluate");
  await expect(page.getByTestId("eval-card")).toBeVisible();
  const status = page.getByTestId("eval-status");
  await expect(status).toContainText("0 judged");
  await expect(page.getByText("Why it matches you")).toHaveCount(0); // no ranking reasons
  const title = await page.getByTestId("eval-title").innerText();
  await page.getByRole("button", { name: "Too expensive" }).click();
  await page.getByTestId("grade-2").click();
  await expect(status).toContainText("1 judged");
  await expect(page.getByTestId("eval-title")).not.toHaveText(title);
  await page.getByRole("button", { name: "Undo last judgement" }).click();
  await expect(status).toContainText("0 judged");
  await page.goto("/insights");
  await expect(page.getByTestId("evaluate-link")).toContainText("0 graded");
});
