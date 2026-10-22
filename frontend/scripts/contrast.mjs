// WCAG contrast of the design-system token pairs (docs/redesign/01-design-system.md), light and dark.
//   node scripts/contrast.mjs        exits 1 if any pair is below its target
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const css = readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), "../src/app/globals.css"), "utf8");

function tokens(selector) {
  const start = css.indexOf(selector);
  const block = css.slice(css.indexOf("{", start) + 1, css.indexOf("}", start));
  return Object.fromEntries([...block.matchAll(/--([\w-]+):\s*([\d.]+)\s+([\d.]+)%\s+([\d.]+)%/g)].map((m) => [m[1], [+m[2], +m[3], +m[4]]]));
}

function hslToRgb([h, s, l]) {
  s /= 100;
  l /= 100;
  const k = (n) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [f(0), f(8), f(4)];
}

function luminance(rgb) {
  const [r, g, b] = rgb.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function ratio(a, b) {
  const [la, lb] = [luminance(hslToRgb(a)), luminance(hslToRgb(b))].sort((x, y) => y - x);
  return (la + 0.05) / (lb + 0.05);
}

// [foreground token, background token, minimum]: 4.5 for text, 3 for UI boundaries and large marks.
const PAIRS = [
  ...["background", "surface", "card", "popover", "secondary", "muted"].flatMap((bg) => [
    ["foreground", bg, 4.5],
    ["muted-foreground", bg, 4.5],
  ]),
  ["muted-foreground", "image-well", 4.5],
  ["primary", "background", 4.5],
  ["primary", "surface", 4.5],
  ["primary-foreground", "primary", 4.5],
  ["destructive", "background", 4.5],
  ["caution", "background", 4.5],
  ["positive", "background", 4.5],
  ["input", "background", 3],
  ["input", "card", 3],
  ["ring", "background", 3],
];

let failed = 0;
for (const [name, selector] of [
  ["light", ":root {"],
  ["dark", ':root[data-theme="dark"] {'],
]) {
  const t = tokens(selector);
  for (const [fg, bg, min] of PAIRS) {
    const r = ratio(t[fg], t[bg]);
    const ok = r >= min;
    failed += ok ? 0 : 1;
    console.log(`${ok ? "ok  " : "FAIL"} ${name.padEnd(5)} ${fg} on ${bg}: ${r.toFixed(2)} (needs ${min})`);
  }
}
process.exit(failed ? 1 : 0);
