import { expect, test } from "@playwright/test";

// The /api proxy holds the backend token: only this app's own pages may use it.
test("proxy refuses other websites and unknown host names", async ({ request }) => {
  const evil = await request.get("/api/backup/export?include_images=false", { headers: { Origin: "https://evil.example" } });
  expect(evil.status()).toBe(403);
  const crossSite = await request.get("/api/backup/export?include_images=false", { headers: { "Sec-Fetch-Site": "cross-site" } });
  expect(crossSite.status()).toBe(403);
  const post = await request.post("/api/scan/continue", { headers: { Origin: "https://evil.example", "Content-Type": "text/plain" } });
  expect(post.status()).toBe(403);
  const own = await request.get("/api/health");
  expect(own.status()).toBe(200);
  expect(own.headers()["access-control-allow-origin"]).toBeUndefined();
});
