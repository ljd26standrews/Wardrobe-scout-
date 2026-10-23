"use client";
import { useCallback, useRef } from "react";

// What counts as having seen a piece (backend app/services/exposure.py, which stores the definition with every
// record): at least half of the tile (or of the screen, for a tile taller than it) in view, in a tab you are looking
// at, for DWELL_MS without interruption, or a decision about it while it is on screen. One continuous stretch on
// screen is one exposure, however many feed answers arrive meanwhile; scrolling it away, switching tab or leaving the
// page ends the stretch. Rendering a tile is not an exposure.
export const EXPOSURE_DEFINITION = "exposure-v2";
export const DWELL_MS = 1000;
export const VISIBLE_FRACTION = 0.5;
// A gap shorter than this does not end a stretch: when a feed answer re-ranks the grid, moved tiles are reported out
// of view for a frame or two although they never left the screen.
export const GRACE_MS = 250;

type ExposureEvent = { event_id: string; feed_id: string; product_id: number; surface: "card" | "grid"; dwell_ms: number };
export type ExposureTarget = { productId: number; feedId: string | null | undefined; surface: "card" | "grid" };

// ----------------------------------------------------------------- delivery: once per event, retried until accepted
let queue: ExposureEvent[] = [];
let timer: ReturnType<typeof setTimeout> | null = null;
let backoff = 0;
let sending = false;

function eventId(): string {
  const b = new Uint8Array(16);
  crypto.getRandomValues(b); // works on a plain-http address too, unlike crypto.randomUUID
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("").replace(/^(.{8})(.{4})(.{4})(.{4})(.{12})$/, "$1-$2-$3-$4-$5");
}

function schedule(ms: number) {
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => void flush(), ms);
}

/** Send what is waiting. The server records each event id once, so sending again after a failure (or a lost answer)
 *  never counts an exposure twice. */
export async function flush(keepalive = false): Promise<void> {
  if (sending || !queue.length) return;
  sending = true;
  const batch = queue.slice(0, 100);
  try {
    const res = await fetch("/api/exposures", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ definition: EXPOSURE_DEFINITION, events: batch }),
      keepalive,
    });
    if (res.status === 422) {
      queue = queue.filter((e) => !batch.includes(e)); // not acceptable as sent: retrying would not help
    } else if (!res.ok) {
      throw new Error(String(res.status));
    } else {
      queue = queue.filter((e) => !batch.includes(e));
    }
    backoff = 0;
    if (queue.length) schedule(0);
  } catch {
    backoff = Math.min(30_000, Math.max(1_000, backoff * 2));
    schedule(backoff);
  } finally {
    sending = false;
  }
}

function record(e: Omit<ExposureEvent, "event_id">) {
  queue.push({ ...e, event_id: eventId() });
  if (!backoff) schedule(300);
}

if (typeof window !== "undefined") {
  // Leaving the page: send what is waiting with a request that outlives it.
  window.addEventListener("pagehide", () => void flush(true));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") void flush(true);
  });
}

// ----------------------------------------------------------------- stretches on screen
type Stretch = { complete: () => void };
const onScreen = new Map<number, Stretch>(); // product id -> the stretch it is in now

/** You decided about this piece, which is on screen (the card, or the tile you acted on): it was seen, however
 *  briefly. Completes its stretch on screen, or records it at once if the browser has not reported it in view yet. */
export function exposeNow(productId: number, shown: Omit<ExposureTarget, "productId">) {
  const stretch = onScreen.get(productId);
  if (stretch) stretch.complete();
  else if (shown.feedId) record({ feed_id: shown.feedId, product_id: productId, surface: shown.surface, dwell_ms: 0 });
}

/** A ref for a piece's tile or card: records an exposure under the rule above. The feed a stretch is credited to is
 *  the one on screen when it began, so a refreshed feed answer neither restarts it nor counts it again. */
export function useExposure(target: ExposureTarget | null) {
  const latest = useRef(target);
  latest.current = target;
  const productId = target?.productId;
  const surface = target?.surface;
  const cleanup = useRef<(() => void) | null>(null);
  return useCallback(
    (el: Element | null) => {
      cleanup.current?.();
      cleanup.current = null;
      if (!el || productId == null || !surface || typeof IntersectionObserver === "undefined") return;
      let inView = false;
      let start: number | null = null;
      let feed: string | null = null;
      let done = false;
      let t: ReturnType<typeof setTimeout> | null = null;
      let gap: ReturnType<typeof setTimeout> | null = null;
      const complete = () => {
        if (start == null || done || !feed) return;
        done = true;
        record({ feed_id: feed, product_id: productId, surface, dwell_ms: Math.round(performance.now() - start) });
      };
      const begin = () => {
        if (start != null) return;
        start = performance.now();
        feed = latest.current?.feedId ?? null;
        done = false;
        t = setTimeout(complete, DWELL_MS);
        onScreen.set(productId, { complete });
      };
      const end = () => {
        if (t) clearTimeout(t);
        if (gap) clearTimeout(gap);
        t = gap = null;
        start = null;
        if (onScreen.get(productId)?.complete === complete) onScreen.delete(productId);
      };
      const update = () => {
        if (inView && document.visibilityState === "visible") {
          if (gap) clearTimeout(gap); // back within the grace period: the same stretch
          gap = null;
          begin();
        } else if (start != null && !gap) {
          gap = setTimeout(end, GRACE_MS);
        }
      };
      const io = new IntersectionObserver(
        ([entry]) => {
          const vh = entry.rootBounds?.height || window.innerHeight;
          inView = entry.isIntersecting && (entry.intersectionRatio >= VISIBLE_FRACTION || entry.intersectionRect.height >= VISIBLE_FRACTION * vh);
          update();
        },
        { threshold: [0, 0.25, 0.5, 0.75, 1] },
      );
      io.observe(el);
      document.addEventListener("visibilitychange", update);
      cleanup.current = () => {
        io.disconnect();
        document.removeEventListener("visibilitychange", update);
        end();
      };
    },
    [productId, surface],
  );
}
