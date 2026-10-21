import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatPrice(value: number | null | undefined, currency = "CNY"): string {
  if (value == null) return "—";
  const symbol = currency === "GBP" ? "£" : currency === "USD" ? "$" : currency === "EUR" ? "€" : "¥";
  return `${symbol}${value.toLocaleString("en-GB", { maximumFractionDigits: value < 100 && currency === "GBP" ? 2 : 0 })}`;
}

export function formatGBP(value: number | null | undefined): string {
  if (value == null) return "—";
  return `£${value.toLocaleString("en-GB", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "never";
  const then = new Date(iso.endsWith("Z") ? iso : iso + "Z").getTime();
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} d ago`;
  return new Date(then).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

export function titleCase(s: string | null | undefined): string {
  if (!s) return "";
  return s.replace(/\b\w/g, (c) => c.toUpperCase());
}

export const COLOUR_SWATCH: Record<string, string> = {
  black: "#151515",
  charcoal: "#3b3b3d",
  grey: "#8d8b88",
  white: "#fbfaf7",
  cream: "#efe7d6",
  beige: "#d8c8ad",
  taupe: "#9a8b7d",
  camel: "#b48a5a",
  brown: "#5b4331",
  navy: "#1f2a44",
  blue: "#3c5a8a",
  olive: "#5c5f3a",
  green: "#3f6b4a",
  khaki: "#a59a6e",
  burgundy: "#6b2232",
  red: "#a3302e",
  pink: "#d9a3a8",
  yellow: "#d9b440",
  orange: "#cc7333",
  purple: "#5e4677",
};
