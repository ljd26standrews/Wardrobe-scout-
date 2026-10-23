import { expect, test, type Page } from "@playwright/test";

// Exposure (app/services/exposure.py, src/lib/exposure.ts): a piece counts as seen when at least half of it is on
// screen, in a visible tab, for a second, or when you decide about it while it is on screen. These checks run last
// (their decisions change Discover), on the synthetic demo catalogue.
test.describe.configure({ mode: "serial" });

type Summary = { events: number; by_product: Record<string, number>; feeds: string[]; definition: string };
const summary = async (page: Page, since: string): Promise<Summary> => (await page.request.get(`/api/exposures/summary?since=${encodeURIComponent(since)}`)).json();
const ids = (s: Summary) => new Set(Object.keys(s.by_product).map(Number));
/** Whether every one of ``pieces`` has been recorded as seen since ``since``. */
const allSeen = async (page: Page, since: string, pieces: Iterable<number>) => {
  const seen = ids(await summary(page, since));
  return [...pieces].every((id) => seen.has(id));
};

/** Grid tiles by how much of each is on screen now. */
async function tiles(page: Page) {
  const all = await page.evaluate(() => {
    const vh = window.innerHeight;
    const vw = window.innerWidth;
    return [...document.querySelectorAll('[data-testid="discover-grid"] [data-testid="product-card"]')].map((el) => {
      const r = el.getBoundingClientRect();
      const h = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
      const w = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
      return { id: Number(el.getAttribute("data-product-id")), ratio: (h * w) / (r.height * r.width || 1), covers: h >= 0.5 * vh };
    });
  });
  return {
    shown: new Set(all.filter((t) => t.ratio >= 0.6 || t.covers).map((t) => t.id)),
    hidden: new Set(all.filter((t) => t.ratio <= 0.4 && !t.covers).map((t) => t.id)),
    count: all.length,
  };
}

async function grid(page: Page) {
  await page.goto("/");
  await page.getByRole("radio", { name: "Grid" }).click();
  await expect(page.getByTestId("discover-grid")).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));
}

test("an unscrolled grid records only the tiles on screen; scrolling adds the ones that come into view", async ({ page }) => {
  const since = new Date().toISOString();
  await grid(page);
  const first = await tiles(page);
  expect(first.count).toBeGreaterThan(first.shown.size); // there is more below the fold
  await expect.poll(() => allSeen(page, since, first.shown), { timeout: 10_000 }).toBe(true);
  await page.waitForTimeout(1500);
  let s = await summary(page, since);
  expect([...first.hidden].filter((id) => ids(s).has(id))).toEqual([]); // below the fold: not seen
  expect(Object.values(s.by_product).every((n) => n === 1)).toBe(true);

  await page.mouse.wheel(0, 900);
  await page.waitForTimeout(300);
  const second = await tiles(page);
  const newly = [...second.shown].filter((id) => !first.shown.has(id));
  expect(newly.length).toBeGreaterThan(0);
  await expect.poll(() => allSeen(page, since, newly), { timeout: 10_000 }).toBe(true);
  s = await summary(page, since);
  for (const id of first.shown) expect(s.by_product[id]).toBe(1); // scrolled away and not back: still once
  expect(s.definition).toBe("exposure-v2");
});

test("a new feed answer does not count a tile that stayed on screen again", async ({ page }) => {
  await grid(page);
  const before = await tiles(page);
  const since = new Date(Date.now() - 500).toISOString();
  await page.waitForTimeout(1800); // the tiles on screen have been seen
  const last = [...before.shown].at(-1)!;
  const answered = page.waitForResponse((r) => r.url().includes("/api/feed?") && r.request().method() === "GET");
  const scrolled = await page.evaluate(() => window.scrollY);
  // Dislike one tile without moving the page (a pointer would scroll it into view first): the grid re-ranks.
  await page.evaluate((id) => (document.querySelector(`[data-testid="product-card"][data-product-id="${id}"] [data-testid="tile-dislike"]`) as HTMLElement).click(), last);
  await answered; // a new feed answer, under a new feed id
  await page.waitForTimeout(2000);
  expect(await page.evaluate(() => window.scrollY)).toBe(scrolled); // the screen never moved
  const after = await tiles(page);
  const stayed = [...before.shown].filter((id) => id !== last && after.shown.has(id));
  expect(stayed.length).toBeGreaterThan(2);
  const s = await summary(page, since);
  for (const id of stayed) expect(s.by_product[id], `piece ${id}`).toBe(1);
});

test("a failed delivery and its retry record each exposure exactly once", async ({ page }) => {
  let attempts = 0;
  // The first delivery reaches the server but its answer is lost; the browser sends the same events again.
  await page.route("**/api/exposures", async (route) => {
    attempts++;
    if (attempts === 1) {
      await route.fetch();
      await route.fulfill({ status: 503, body: "{}" });
      return;
    }
    await route.continue();
  });
  const since = new Date().toISOString();
  await grid(page);
  const { shown } = await tiles(page);
  await expect.poll(() => attempts, { timeout: 15_000 }).toBeGreaterThanOrEqual(2);
  await expect.poll(() => allSeen(page, since, shown), { timeout: 15_000 }).toBe(true);
  const s = await summary(page, since);
  expect(Object.values(s.by_product).every((n) => n === 1)).toBe(true);
  expect(s.events).toBe(Object.keys(s.by_product).length);
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

test("nothing counts while the tab is hidden; it counts once you are back", async ({ page }) => {
  await grid(page);
  await page.waitForTimeout(1500);
  const hide = (hidden: boolean) =>
    page.evaluate((h) => {
      if (h) Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
      else delete (document as unknown as { visibilityState?: string }).visibilityState;
      document.dispatchEvent(new Event("visibilitychange"));
    }, hidden);
  await hide(true);
  await page.mouse.wheel(0, 900); // new tiles come into view while you are elsewhere
  await page.waitForTimeout(300);
  const since = new Date().toISOString();
  const { shown } = await tiles(page);
  await page.waitForTimeout(2000);
  expect((await summary(page, since)).events).toBe(0);
  await hide(false);
  await expect.poll(() => allSeen(page, since, shown), { timeout: 10_000 }).toBe(true);
});

test("the card counts after a second on screen, or at once when you decide about it", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("radio", { name: "One at a time" }).click();
  const card = page.getByTestId("discover-card");
  await expect(card).toBeVisible();
  const since = new Date().toISOString();
  const a = Number(await card.getAttribute("data-product-id"));
  await expect.poll(async () => (await summary(page, since)).by_product[a], { timeout: 10_000 }).toBe(1);
  await page.keyboard.press("n"); // skip: the next card
  await expect(card).not.toHaveAttribute("data-product-id", String(a));
  const b = Number(await card.getAttribute("data-product-id"));
  await page.keyboard.press("n"); // decided within a moment of seeing it
  await expect.poll(async () => (await summary(page, since)).by_product[b], { timeout: 10_000 }).toBe(1);
});
