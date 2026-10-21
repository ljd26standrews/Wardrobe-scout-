// Screenshots of the running app's core screens at phone, tablet and desktop widths, for before/after review.
// Uses the SYNTHETIC demo catalogue served by `E2E_SERVE_ONLY=1 ./scripts/e2e.sh` (never your real data).
//   node scripts/screens.mjs http://localhost:13000 ../docs/redesign/screens/before
// Optional env: PW_EXECUTABLE_PATH (Chromium), SCREENS_PREPARE=0 to skip the demo decisions.
import { mkdirSync } from "node:fs";
import path from "node:path";
import { chromium } from "@playwright/test";

const base = process.argv[2] ?? "http://localhost:13000";
const out = path.resolve(process.argv[3] ?? "screens");
mkdirSync(out, { recursive: true });

const WIDTHS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "tablet", width: 768, height: 1024 },
  { name: "phone", width: 390, height: 844 },
];

const browser = await chromium.launch({ executablePath: process.env.PW_EXECUTABLE_PATH || undefined });
const setup = await browser.newPage({ baseURL: base });

// A few ordinary decisions so Saved and Insights have something to show (the same for every run).
const feed = await (await setup.request.get("/api/feed?limit=24")).json();
const ids = feed.items.map((p) => p.id);
if (process.env.SCREENS_PREPARE !== "0" && ids.length >= 6) {
  const decide = (product_id, action, reasons = []) => setup.request.post("/api/feedback", { data: { product_id, action, reasons } });
  await decide(ids[0], "love");
  await decide(ids[1], "like");
  await decide(ids[2], "like");
  await decide(ids[3], "maybe");
  await decide(ids[4], "dislike", ["colour"]);
}
const products = await (await setup.request.get("/api/products?sort=newest&page_size=40")).json();
const productId = products.items.find((p) => p.images?.length)?.id ?? products.items[0]?.id;
await setup.close();

const screens = [
  { name: "discover", url: "/" },
  { name: "discover-grid", url: "/", storage: { "ws-discover-mode": '"grid"' } },
  { name: "product", url: `/product/${productId}` },
  { name: "saved", url: "/saved" },
  { name: "browse", url: "/browse" },
  { name: "wardrobe", url: "/wardrobe" },
  { name: "sellers", url: "/sellers" },
  { name: "insights", url: "/insights" },
  { name: "evaluate", url: "/evaluate" },
  { name: "settings", url: "/settings" },
];

for (const scheme of ["light", "dark"]) {
  for (const w of WIDTHS) {
    if (scheme === "dark" && w.name !== "desktop" && w.name !== "phone") continue;
    const context = await browser.newContext({ viewport: { width: w.width, height: w.height }, colorScheme: scheme, reducedMotion: "reduce" });
    for (const s of screens) {
      if (scheme === "dark" && !["discover", "product", "saved"].includes(s.name)) continue;
      const page = await context.newPage();
      if (s.storage) {
        await page.addInitScript((kv) => Object.entries(kv).forEach(([k, v]) => localStorage.setItem(k, v)), s.storage);
      }
      await page.goto(base + s.url, { waitUntil: "networkidle" });
      await page.waitForTimeout(700);
      const file = path.join(out, `${w.name}-${scheme === "dark" ? "dark-" : ""}${s.name}.png`);
      await page.screenshot({ path: file });
      await page.close();
    }
    await context.close();
  }
}
await browser.close();
console.log(`screenshots in ${out}`);
