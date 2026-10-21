"use client";
import { Check, Minus } from "lucide-react";
import type { Confidence, Gate } from "@/lib/api";
import { hasEstimates, MODE_LABEL, MODES } from "@/lib/api";
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
          Calibrated approval estimate: about {approx(c.estimate).replace("≈", "")} chance you&apos;d love, save or buy it
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
          ? "Your decisions are being calibrated; approval estimates appear shortly."
          : c.state === "failed"
            ? "Calibrating your decisions failed and will be retried shortly; until then this piece is ranked by taste without a probability."
          : c.state === "unavailable"
            ? "Approval estimates are unavailable right now, so this piece is shown by taste ranking only."
            : "No approval estimate yet: there aren't enough of your rated pieces to calibrate one, so this piece is ranked by taste without a probability."}
        {c.learning && " It's a learning pick, not a recommendation."}
      </p>
    );
  }
  return (
    <div className="space-y-3 text-sm" data-testid="confidence-panel">
      <p>
        <span className="text-3xl font-medium">{approx(c.estimate)}</span>{" "}
        <span className="text-muted-foreground">estimated chance you&apos;d approve (love, save or buy)</span>
      </p>
      {c.bound != null && c.level != null && c.level > 0.5 ? (
        <p className="text-muted-foreground">
          With {pct(c.level)} confidence it&apos;s at least {atLeast(c.bound)}: the figure {MODE_LABEL[c.mode]} mode checks against its {pct(c.target ?? 0)} target.
        </p>
      ) : (
        c.target != null && <p className="text-muted-foreground">{MODE_LABEL[c.mode]} mode recommends pieces whose estimate is at least {pct(c.target)}.</p>
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
      {c.learning && <p className="text-xs text-muted-foreground">A learning pick: the model is unsure about pieces like this, so it&apos;s shown to learn, not as a recommendation.</p>}
    </div>
  );
}

/** One line under the Discover title: what this feed is, and how far calibration has come. */
export function GateStatus({ gate, total }: { gate: Gate; total: number }) {
  if (gate.state === "off") return null;
  const p = gate.progress;
  if (hasEstimates(gate.state)) {
    const cal = gate.calibration;
    const obs = gate.observed;
    const status = (
      <p className="mt-2 text-sm text-muted-foreground" data-testid="gate-status">
        {gate.policy === "learning"
          ? `${gate.shown} learning picks from ${gate.sampling?.pool ?? 0} uncertain pieces`
          : `${gate.shown} of ${total} undecided pieces pass ${gate.mode_label} (${modeRule(gate.target ?? 0, gate.confidence ?? 0)})`}
        {cal && (
          <>
            {" · "}
            <Tip content={`Fitted on ${cal.labels} of your rated pieces (${cal.approvals} approved). Out-of-sample calibration error ${pct(cal.ece)}. The 80/90/95% mode targets are aspirations until your own outcomes confirm them (Insights).`}>
              <span className="underline decoration-dotted underline-offset-2">calibrated on {cal.labels} decisions; targets not yet validated</span>
            </Tip>
          </>
        )}
        {gate.state === "stale" && gate.message && <span className="block">{gate.message}</span>}
      </p>
    );
    if (!obs?.shortfall || !obs.interval) return status;
    return (
      <>
        <p className="mt-2 max-w-2xl rounded-lg bg-caution/10 px-3 py-2 text-sm text-caution" data-testid="gate-shortfall">
          So far {obs.approved} of {obs.rated} rated pieces that passed {gate.mode_label} were approved ({pct(obs.rate ?? 0)}; 95% interval {pct(obs.interval[0])}–
          {pct(obs.interval[1])}), below its {pct(gate.target ?? 0)} target. Its estimates look optimistic for you; a stricter mode or more decisions should help.
        </p>
        {status}
      </>
    );
  }
  const withdrawn = gate.withdrawn ? ` Earlier estimates were withdrawn: ${gate.withdrawn.label}.` : "";
  if (gate.state === "pending") return <p className="mt-2 text-sm text-muted-foreground" data-testid="gate-status">Calibrating approval estimates from your decisions…{withdrawn}</p>;
  if (gate.state === "failed") return <p className="mt-2 text-sm text-muted-foreground" data-testid="gate-status">Calibrating your decisions failed; it will be retried in a few minutes. Nothing is shown as high-confidence meanwhile.{withdrawn}</p>;
  if (gate.state === "unavailable") return <p className="mt-2 text-sm text-muted-foreground" data-testid="gate-status">Approval estimates are unavailable right now; nothing is shown as high-confidence.</p>;
  return (
    <p className="mt-2 text-sm text-muted-foreground" data-testid="gate-status">
      {total} undecided pieces · not yet calibrated
      {p && (
        <>
          {": "}
          {p.labels} of {p.needed} rated pieces needed for approval estimates
          {p.approvals < p.needed_each || p.non_approvals < p.needed_each ? ` (including ${p.needed_each} approvals and ${p.needed_each} rejections)` : ""}
        </>
      )}
      {withdrawn}
    </p>
  );
}
