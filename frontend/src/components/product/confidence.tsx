"use client";
import { Check, Minus } from "lucide-react";
import type { Confidence } from "@/lib/api";
import { MODE_LABEL, MODES } from "@/lib/api";
import { Tip } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

/** An estimate shown to the nearest 5%: the calibration is not precise enough for more. */
export function approx(p: number) {
  if (p >= 0.95) return "95%+";
  if (p < 0.05) return "under 5%";
  return `≈${Math.round(p * 20) * 5}%`;
}

/** A conservative bound is rounded down, never up. */
export const atLeast = (p: number) => `${Math.floor(p * 100)}%`;

const pct = (p: number) => `${Math.round(p * 100)}%`;

/** A mode's rule in words. At 50% confidence the bound is the estimate itself, so say just that. */
export const modeRule = (target: number, confidence: number) =>
  confidence > 0.5 ? `approval at least ${pct(target)} with ${pct(confidence)} confidence` : `estimated approval at least ${pct(target)}`;

/** Small figure for product cards: only a calibrated estimate, never an uncalibrated score. */
export function ConfidenceBadge({ c }: { c?: Confidence }) {
  if (c?.estimate == null) return null;
  return (
    <Tip
      content={
        <>
          Calibrated approval estimate: about {approx(c.estimate).replace("≈", "")} chance you&apos;d like the look (love or like it)
          {c.bound != null && c.level != null && c.level > 0.5 ? `; with ${pct(c.level)} confidence at least ${atLeast(c.bound)}` : ""}. Calibrated on your own rated pieces.
        </>
      }
    >
      <span className="ml-auto text-xs tabular-nums text-muted-foreground" data-testid="approval-estimate">
        {approx(c.estimate)} est.
      </span>
    </Tip>
  );
}

/** The product page and Discover card: what the gate knows about this piece, in words. */
export function ConfidencePanel({ c }: { c?: Confidence }) {
  if (!c || c.state === "off") return null;
  if (c.estimate == null) {
    return (
      <p className="text-sm leading-relaxed text-muted-foreground" data-testid="confidence-panel">
        {c.state === "pending"
          ? "Match estimates are being updated from your decisions; they appear shortly."
          : c.state === "failed"
            ? "Match estimates couldn't be updated and will be retried shortly. Until then this piece is ranked by your taste, without a percentage."
          : c.state === "unavailable"
            ? "Match estimates are unavailable right now; this piece is ranked by your taste only."
            : "No match estimate yet: Discover needs more of your likes and dislikes before it can say how likely you are to like a piece. This one is ranked by your taste so far."}
        {c.learning && " It's a learning pick, not a recommendation."}
      </p>
    );
  }
  return (
    <div className="space-y-3 text-sm" data-testid="confidence-panel">
      <p>
        <span className="text-3xl font-medium">{approx(c.estimate)}</span>{" "}
        <span className="text-muted-foreground">estimated chance you&apos;d like the look</span>
      </p>
      {c.bound != null && c.level != null && c.level > 0.5 ? (
        <p className="text-muted-foreground">
          Allowing for how unsure the estimate is, it&apos;s at least {atLeast(c.bound)} ({pct(c.level)} confidence). {MODE_LABEL[c.mode]} shows pieces when
          that is at least {pct(c.target ?? 0)}.
        </p>
      ) : (
        c.target != null && <p className="text-muted-foreground">{MODE_LABEL[c.mode]} shows pieces whose estimate is at least {pct(c.target)}.</p>
      )}
      <div className="flex flex-wrap gap-1.5">
        {MODES.map((m) => {
          const ok = c.passes.includes(m);
          return (
            <span key={m} className={cn("inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px]", ok ? "border-positive/40 text-positive" : "text-muted-foreground")}>
              {ok ? <Check className="size-3" /> : <Minus className="size-3" />} {MODE_LABEL[m]}
            </span>
          );
        })}
      </div>
      {c.excluded && (
        <p className="text-xs text-muted-foreground">
          Not eligible in {MODE_LABEL[c.mode]}: {c.excluded.charAt(0).toLowerCase() + c.excluded.slice(1)}.
        </p>
      )}
      {c.learning && <p className="text-xs text-muted-foreground">A learning pick: the app is unsure about pieces like this, so it&apos;s shown to learn your taste, not as a recommendation.</p>}
    </div>
  );
}
