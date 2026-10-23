import { defineConfig, devices } from "@playwright/test";

// Runs against an already-running app (see README → "End-to-end tests"), seeded with the
// SYNTHETIC demo catalogue (backend/scripts/demo_catalogue_server.py) in a throwaway data dir.
export default defineConfig({
  testDir: "./e2e",
  timeout: 45_000,
  fullyParallel: false,
  workers: 1,
  reporter: process.env.GITHUB_ACTIONS ? [["list"], ["github"]] : [["list"]], // failures as annotations in CI
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 }, launchOptions: { executablePath: process.env.PW_EXECUTABLE_PATH || undefined } }, testIgnore: /mobile\.spec\.ts/ },
    { name: "iphone", use: { ...devices["iPhone 13"], browserName: "chromium", launchOptions: { executablePath: process.env.PW_EXECUTABLE_PATH || undefined } }, testMatch: /mobile\.spec\.ts/ },
    { name: "webkit", use: { ...devices["Desktop Safari"], viewport: { width: 1440, height: 900 } }, testIgnore: /mobile\.spec\.ts/ },
    { name: "iphone-webkit", use: { ...devices["iPhone 13"] }, testMatch: /mobile\.spec\.ts/ },
  ],
});
