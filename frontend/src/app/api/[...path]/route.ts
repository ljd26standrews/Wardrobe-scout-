// Runtime proxy: forwards /api/* to the FastAPI backend, adding the per-install API token server-side.
// (Next.js rewrites are fixed at build time; this reads BACKEND_URL and the token when the server runs.)
//
// Optional LAN protection: if WS_ACCESS_CODE is set, every API call must carry the `ws_access` cookie,
// obtained once on the /unlock page. start.sh requires it whenever the app is exposed to your network.
//
// Browser boundary: this proxy holds the backend token, so it only serves pages of this app. Requests from
// other websites (foreign Origin or Sec-Fetch-Site: cross-site) and requests addressed to an unknown host
// name (DNS rebinding) are refused, and CORS headers are never passed back. The bookmarklet is unaffected:
// it calls the backend directly with its own copy of the token.
import { readFileSync } from "node:fs";
import path from "node:path";
import { timingSafeEqual } from "node:crypto";
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const BACKEND = (process.env.BACKEND_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const HOP_BY_HOP = ["connection", "keep-alive", "transfer-encoding", "upgrade", "host", "content-length", "cookie", "x-ws-token", "origin", "referer"];
// Extra host names you reach the app by (comma-separated), e.g. a local DNS name for your computer.
const EXTRA_HOSTS = (process.env.WS_ALLOWED_HOSTS ?? "")
  .split(",")
  .map((h) => h.trim().toLowerCase())
  .filter(Boolean);

function hostName(hostHeader: string): string {
  const h = hostHeader.trim().toLowerCase();
  if (h.startsWith("[")) return h.slice(0, h.indexOf("]") + 1); // [::1]:3000
  return h.replace(/:\d+$/, "");
}

/** Loopback, an IP address (LAN use by address) or a configured name. DNS-rebinding attacks need a domain. */
function allowedHost(hostHeader: string | null): boolean {
  if (!hostHeader) return false;
  const h = hostName(hostHeader);
  if (h === "localhost" || h.endsWith(".localhost") || EXTRA_HOSTS.includes(h)) return true;
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(h)) return true;
  return h.startsWith("[") && h.endsWith("]"); // IPv6 literal
}

/** null if the request may proceed, otherwise the refusal. */
function guard(req: NextRequest): Response | null {
  const host = req.headers.get("host");
  if (!allowedHost(host)) {
    return Response.json({ detail: "Unknown host name. Add it to WS_ALLOWED_HOSTS if this is your computer." }, { status: 403 });
  }
  const site = req.headers.get("sec-fetch-site");
  if (site === "cross-site" || site === "same-site") {
    return Response.json({ detail: "Requests from other websites are not allowed" }, { status: 403 });
  }
  const origin = req.headers.get("origin");
  if (origin) {
    let originHost = "";
    try {
      originHost = new URL(origin).host.toLowerCase();
    } catch {
      originHost = "invalid";
    }
    if (originHost !== (host ?? "").toLowerCase()) {
      return Response.json({ detail: "Requests from other websites are not allowed" }, { status: 403 });
    }
  }
  return null;
}
const ACCESS_CODE = process.env.WS_ACCESS_CODE ?? "";

let cachedToken: string | null = null;
function apiToken(): string | null {
  if (process.env.WS_API_TOKEN) return process.env.WS_API_TOKEN;
  if (cachedToken) return cachedToken;
  const file =
    process.env.WS_API_TOKEN_FILE ??
    path.join(process.env.WS_DATA_DIR ?? path.resolve(process.cwd(), "..", "backend", "data"), "api_token");
  try {
    cachedToken = readFileSync(file, "utf8").trim() || null;
  } catch {
    cachedToken = null; // backend not started yet; retry next request
  }
  return cachedToken;
}

function safeEqual(a: string, b: string) {
  const x = Buffer.from(a);
  const y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

async function unlock(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  if (!ACCESS_CODE) return Response.json({ ok: true, required: false });
  if (typeof body.code !== "string" || !safeEqual(body.code, ACCESS_CODE)) {
    return Response.json({ detail: "Incorrect access code" }, { status: 401 });
  }
  const res = Response.json({ ok: true });
  res.headers.append("Set-Cookie", `ws_access=${encodeURIComponent(ACCESS_CODE)}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000`);
  return res;
}

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path: parts } = await ctx.params;
  const refused = guard(req);
  if (refused) return refused;
  if (parts.length === 1 && parts[0] === "unlock") {
    if (req.method === "POST") return unlock(req);
    return Response.json({ required: !!ACCESS_CODE });
  }
  if (ACCESS_CODE) {
    const cookie = req.cookies.get("ws_access")?.value ?? "";
    if (!safeEqual(decodeURIComponent(cookie), ACCESS_CODE)) {
      return Response.json({ detail: "Locked: enter the access code", locked: true }, { status: 401 });
    }
  }
  const token = apiToken();
  if (!token) {
    return Response.json({ detail: "Backend token not found yet. Is the backend running?" }, { status: 502 });
  }
  const url = `${BACKEND}/api/${parts.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers = new Headers(req.headers);
  HOP_BY_HOP.forEach((h) => headers.delete(h));
  headers.set("X-WS-Token", token);
  const init: RequestInit & { duplex?: "half" } = { method: req.method, headers, redirect: "manual" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = req.body;
    init.duplex = "half";
  }
  try {
    const res = await fetch(url, init);
    if (res.status === 401) cachedToken = null; // token may have been regenerated
    const out = new Headers(res.headers);
    ["content-encoding", "transfer-encoding", "connection"].forEach((h) => out.delete(h));
    // Never grant other origins access to what the proxy fetched with the backend token.
    [...out.keys()].filter((h) => h.startsWith("access-control-")).forEach((h) => out.delete(h));
    return new Response(res.body, { status: res.status, statusText: res.statusText, headers: out });
  } catch {
    return Response.json({ detail: `Backend not reachable at ${BACKEND}. Start it with ./scripts/start.sh (or uvicorn).` }, { status: 502 });
  }
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
