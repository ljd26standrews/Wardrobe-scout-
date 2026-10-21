import type { NextConfig } from "next";

// /api/* is proxied to FastAPI by src/app/api/[...path]/route.ts (BACKEND_URL, read at runtime).
// A single origin means the app also works when opened from your iPhone on the same Wi-Fi.
const config: NextConfig = {
  reactStrictMode: true,
};

export default config;
