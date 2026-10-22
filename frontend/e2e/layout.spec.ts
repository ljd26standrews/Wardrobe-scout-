import { expect, test, type Page } from "@playwright/test";

// Layout and accessibility checks on the synthetic demo catalogue: widths, zoom, dark mode, reduced motion,
// keyboard focus, accessible names, and the awkward listings from Demo Seller C (very long title, no price,
// a landscape photo, photos that fail to load, a currency without a rate). These read pages; they decide nothing.

const ROUTES = ["/", "/browse", "/saved", "/compare", "/wardrobe", "/sellers", "/insights", "/evaluate", "/settings"];

async function overflowX(page: Page) {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

/** Visible controls without an accessible name (a label, aria-label, aria-labelledby, title or text). */
async function unnamedControls(page: Page) {
  return page.evaluate(() => {
    const out: string[] = [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>("button, a[href], input, select, textarea, [role=radio], [role=tab], [role=switch]"))) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || getComputedStyle(el).visibility === "hidden") continue;
      if (el instanceof HTMLInputElement && (el.type === "hidden" || el.type === "file")) continue;
      const labelled = el.getAttribute("aria-labelledby")?.split(/\s+/).some((id) => document.getElementById(id)?.textContent?.trim());
      const labels = "labels" in el && (el as HTMLInputElement).labels ? Array.from((el as HTMLInputElement).labels!).some((l) => l.textContent?.trim()) : false;
      const name = el.getAttribute("aria-label") || el.getAttribute("title") || el.textContent?.trim() || (el as HTMLInputElement).placeholder;
      if (!labelled && !labels && !name) out.push(el.outerHTML.slice(0, 120));
    }
    return out;
  });
}

for (const width of [390, 768, 1440]) {
  test(`every route fits ${width} px without sideways scrolling, with one main heading`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    for (const route of ROUTES) {
      await page.goto(route);
      await expect(page.locator("main h1").first()).toBeVisible();
      expect(await overflowX(page), `${route} at ${width}px`).toBeLessThanOrEqual(0);
    }
  });
}

test("reflow: Discover and Saved read at 320 px (400% zoom of a 1280 px window)", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  for (const route of ["/", "/saved", "/browse"]) {
    await page.goto(route);
    await expect(page.locator("main h1").first()).toBeVisible();
    expect(await overflowX(page), route).toBeLessThanOrEqual(0);
  }
});

test("text at 200%: the desktop layout still fits when the browser is zoomed", async ({ page }) => {
  await page.setViewportSize({ width: 720, height: 450 }); // a 1440 x 900 window at 200%
  for (const route of ["/", "/saved", "/product/1"]) {
    await page.goto(route);
    expect(await overflowX(page), route).toBeLessThanOrEqual(0);
  }
});

test("keyboard: Skip to content comes first, focus is visible, and controls have names", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await expect(page.getByTestId("discover-card")).toBeVisible();
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to content" });
  await expect(skip).toBeFocused();
  const outline = await skip.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).not.toBe("none");
  for (const route of ["/", "/saved", "/browse", "/settings"]) {
    await page.goto(route);
    await page.waitForLoadState("networkidle");
    expect(await unnamedControls(page), route).toEqual([]);
  }
});

test("dark mode and reduced motion are honoured", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await page.goto("/");
  await expect(page.getByTestId("discover-card")).toBeVisible();
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  const [r, g, b] = bg.match(/\d+/g)!.map(Number);
  expect(r + g + b, `dark background, got ${bg}`).toBeLessThan(120);
  const duration = await page.getByTestId("action-like").evaluate((el) => getComputedStyle(el).transitionDuration);
  expect(parseFloat(duration)).toBeLessThan(0.02);
});

test("awkward listings: long titles wrap, missing photos and prices say so, landscape photos are not cropped", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/browse?q=Extra-long%20title");
  const long = page.getByTestId("product-card").first();
  await expect(long).toBeVisible();
  const box = await long.boundingBox();
  expect(box!.width).toBeLessThanOrEqual(390);
  expect(await overflowX(page)).toBeLessThanOrEqual(0);
  const titleLines = await long.getByTestId("product-title").evaluate((el) => Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight)));
  expect(titleLines).toBeLessThanOrEqual(2); // clamped, not pushing the price away

  await page.goto("/browse?q=询价");
  await expect(page.getByTestId("product-card").first()).toContainText("Price not listed");

  await page.goto("/browse?q=图片加载失败");
  await expect(page.getByTestId("product-card").first()).toContainText(/Photo unavailable|No photo/);

  await page.goto("/browse?q=平铺拍摄");
  const img = page.getByTestId("product-card").first().locator("img").first();
  await expect(img).toBeVisible();
  const fit = await img.evaluate((el) => ({ fit: getComputedStyle(el).objectFit, w: (el as HTMLImageElement).naturalWidth, h: (el as HTMLImageElement).naturalHeight }));
  expect(fit.fit).toBe("contain"); // the whole garment, letterboxed in the well
  expect(fit.w).toBeGreaterThan(fit.h); // it really is a landscape photo

  await page.goto("/browse?q=won");
  const won = page.getByTestId("product-card").first();
  await expect(won).toContainText("89,000");
  await expect(won).not.toContainText("≈ £"); // no rate for won: shown as listed, never converted or assumed
});
