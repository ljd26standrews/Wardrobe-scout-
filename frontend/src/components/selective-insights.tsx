"use client";
import useSWR from "swr";
import { toast } from "sonner";
import { RefreshCw } from "lucide-react";
import type { ObservedRate, ReliabilityBin, SelectiveStatus } from "@/lib/api";
import { api, MODE_LABEL, MODES } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, Tip } from "@/components/ui/misc";
import { modeRule } from "@/components/product/confidence";
import { timeAgo } from "@/lib/utils";

const pct = (p: number) => `${Math.round(p * 100)}%`;
const capitalise = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);

function StatTile({ label, value, sub }: { label: string; value: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <Card className="p-4">
      <p className="label">{label}</p>
      <p className="mt-2 text-2xl font-medium leading-none">{value}</p>
      {sub && <p className="mt-2 text-xs text-muted-foreground">{sub}</p>}
    </Card>
  );
}

/** Observed approval for one group of served pieces: dot = observed rate, whisker = 95% interval, tick = target. */
function RateRow({ label, r, target, min }: { label: string; r: ObservedRate; target?: number; min: number }) {
  const known = r.rate != null && r.interval != null;
  const text = known
    ? `${r.approved} of ${r.rated} approved · ${pct(r.rate!)} (${Math.round(r.interval![0] * 100)}–${pct(r.interval![1])})`
    : r.rated
      ? `${r.rated} rated; ${min} needed for a rate`
      : "none rated yet";
  return (
    <div className="grid grid-cols-[minmax(0,9rem)_1fr] items-center gap-x-4 gap-y-1 py-2 sm:grid-cols-[minmax(0,9rem)_1fr_14rem]" data-testid="observed-rate">
      <span className="text-[13px]">{label}</span>
      <Tip content={<>{known ? <strong>{pct(r.rate!)} approved</strong> : <strong>No rate yet</strong>} · {text}{target != null ? ` · target ${pct(target)}` : ""}</>}>
        <div className="relative h-4" tabIndex={0} aria-label={`${label}: ${text}`}>
          <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-border" />
          {known && (
            <>
              <div className="absolute top-1/2 h-0.5 -translate-y-1/2 rounded-full bg-chartaccent/45" style={{ left: `${r.interval![0] * 100}%`, width: `${Math.max((r.interval![1] - r.interval![0]) * 100, 0.5)}%` }} />
              <div className="absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-chartaccent ring-2 ring-card" style={{ left: `${r.rate! * 100}%` }} />
            </>
          )}
          {target != null && <div className="absolute inset-y-0 w-0.5 -translate-x-1/2 rounded-full bg-foreground/70" style={{ left: `${target * 100}%` }} />}
        </div>
      </Tip>
      <span className="col-span-2 text-xs tabular-nums text-muted-foreground sm:col-span-1 sm:text-right">{text}</span>
    </div>
  );
}

/** Out-of-fold estimates against what you actually decided. On the diagonal = calibrated. */
function ReliabilityDiagram({ bins }: { bins: ReliabilityBin[] }) {
  const W = 320, H = 240, L = 38, R = 18, T = 10, B = 30; // prettier-ignore
  const x = (v: number) => L + v * (W - L - R);
  const y = (v: number) => H - B - v * (H - T - B);
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  const pts = [...bins].sort((a, b) => a.mean_estimate - b.mean_estimate);
  return (
    <figure>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full max-w-md" role="img" aria-label="Reliability diagram: estimated against observed approval">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(0)} x2={x(1)} y1={y(t)} y2={y(t)} className="stroke-border" strokeWidth={1} />
            <line x1={x(t)} x2={x(t)} y1={y(0)} y2={y(1)} className="stroke-border" strokeWidth={1} />
            <text x={L - 6} y={y(t) + 3} textAnchor="end" className="fill-muted-foreground text-[9px] tabular-nums">{pct(t)}</text>
            <text x={x(t)} y={H - B + 14} textAnchor="middle" className="fill-muted-foreground text-[9px] tabular-nums">{pct(t)}</text>
          </g>
        ))}
        <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} className="stroke-muted-foreground" strokeWidth={1} />
        <text x={x(0.5)} y={H - 2} textAnchor="middle" className="fill-muted-foreground text-[9px]">approval estimate</text>
        <text x={10} y={y(0.5)} textAnchor="middle" transform={`rotate(-90 10 ${y(0.5)})`} className="fill-muted-foreground text-[9px]">observed approval</text>
        {pts.length > 1 && <polyline points={pts.map((b) => `${x(b.mean_estimate)},${y(b.observed)}`).join(" ")} fill="none" className="stroke-chartaccent" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />}
        {pts.map((b) => (
          <line key={`w${b.lo}`} x1={x(b.mean_estimate)} x2={x(b.mean_estimate)} y1={y(b.interval[0])} y2={y(b.interval[1])} className="stroke-chartaccent/45" strokeWidth={2} strokeLinecap="round" />
        ))}
        {pts.map((b) => (
          <Tip
            key={`d${b.lo}`}
            content={
              <>
                <strong>{pct(b.observed)} approved</strong> (95%: {pct(b.interval[0])}–{pct(b.interval[1])}) · estimate {pct(b.mean_estimate)} · {b.n} pieces
              </>
            }
          >
            <g tabIndex={0} aria-label={`Estimate ${pct(b.mean_estimate)}, observed ${pct(b.observed)} of ${b.n} pieces`} className="cursor-default outline-none">
              <circle cx={x(b.mean_estimate)} cy={y(b.observed)} r={12} fill="transparent" />
              <circle cx={x(b.mean_estimate)} cy={y(b.observed)} r={4.5} className="fill-chartaccent stroke-card" strokeWidth={2} />
            </g>
          </Tip>
        ))}
      </svg>
      <figcaption className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-px w-4 bg-muted-foreground" /> perfectly calibrated
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block size-2 rounded-full bg-chartaccent" /> your rated pieces, with 95% intervals
        </span>
      </figcaption>
      <details className="mt-2 text-xs">
        <summary className="cursor-pointer text-muted-foreground">Show as a table</summary>
        <table className="mt-2 w-full max-w-md text-left tabular-nums">
          <thead className="text-muted-foreground">
            <tr>
              <th className="py-1 font-normal">Estimates</th>
              <th className="py-1 font-normal">Pieces</th>
              <th className="py-1 font-normal">Mean estimate</th>
              <th className="py-1 font-normal">Observed (95%)</th>
            </tr>
          </thead>
          <tbody>
            {pts.map((b) => (
              <tr key={b.lo} className="border-t">
                <td className="py-1">
                  {pct(b.lo)}–{pct(b.hi)}
                </td>
                <td className="py-1">{b.n}</td>
                <td className="py-1">{pct(b.mean_estimate)}</td>
                <td className="py-1">
                  {pct(b.observed)} ({pct(b.interval[0])}–{pct(b.interval[1])})
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}

export function SelectiveInsights() {
  const { data, mutate } = useSWR<SelectiveStatus>("/api/selective/status");
  if (!data) return null;
  if (!data.enabled)
    return (
      <Card className="mt-6 p-5 text-sm text-muted-foreground">Selective recommendation is switched off in Settings; Discover shows the earlier ranked feed with exploration slots.</Card>
    );
  const cal = data.calibration;
  const perf = data.performance;
  const current = data.modes.find((m) => m.mode === data.mode)!;
  const recalibrate = async () => {
    try {
      await api.post("/api/selective/recalibrate");
      toast("Recalibrating from your decisions");
      setTimeout(() => mutate(), 2500);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not start recalibration");
    }
  };
  const activate = async (id: number) => {
    try {
      await mutate(api.post<SelectiveStatus>(`/api/selective/calibrations/${id}/activate`), { revalidate: false });
      toast("Calibration restored");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not restore that calibration");
    }
  };
  return (
    <div data-testid="selective-insights">
      <p className="mt-2 max-w-2xl text-sm leading-relaxed text-muted-foreground">
        Discover recommends a piece only when the model is sure enough you&apos;ll approve of it (love, save or buy). The 80%, 90% and 95% targets are aspirations:
        they count as met only when the observed rates below confirm them, and with few decisions the intervals stay wide.
      </p>
      <div className="mt-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Mode" value={MODE_LABEL[data.mode]} sub={capitalise(modeRule(current.target, current.confidence))} />
        <StatTile
          label="Calibration"
          value={cal.calibration ? `${cal.calibration.n_labels} decisions` : `${cal.labels} of ${cal.needed}`}
          sub={
            cal.calibration
              ? `${cal.calibration.map === "identity" ? "Model probabilities kept" : "Platt-scaled"} · ${timeAgo(cal.calibration.created_at)}` +
                (cal.state === "stale" ? ` · ${cal.since?.new_decisions ?? 0} newer decisions not yet included` : "")
              : cal.state === "pending"
                ? "Calibrating now" + (cal.withdrawn ? ` (the last one was withdrawn: ${cal.withdrawn.label})` : "")
                : cal.state === "failed"
                  ? "The last attempt failed; it will be retried shortly"
                  : `Rated pieces needed (at least ${cal.needed_each} approvals and ${cal.needed_each} rejections)` + (cal.withdrawn ? `. The last calibration was withdrawn: ${cal.withdrawn.label}` : "")
          }
        />
        <StatTile label="Calibration error" value={cal.calibration ? pct(cal.calibration.ece) : "—"} sub="Expected gap between estimate and outcome, out of sample. Lower is better." />
        <StatTile
          label="Ranking separation"
          value={cal.calibration?.auc_out_of_fold != null ? cal.calibration.auc_out_of_fold.toFixed(2) : "—"}
          sub="Chance an approved piece is ranked above a rejected one (0.5 = coin flip), out of sample."
        />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card className="p-5">
          <p className="label mb-1">Observed approval of what was served</p>
          <p className="mb-3 text-xs text-muted-foreground">
            Pieces you were shown and then rated. Ticks mark each mode&apos;s target. {perf.note}
          </p>
          {MODES.map((m) => (
            <RateRow key={m} label={`Passed ${MODE_LABEL[m]}`} r={perf.passed_gate[m]} target={data.modes.find((x) => x.mode === m)?.target} min={perf.min_rated} />
          ))}
          <RateRow label="Learning picks" r={perf.learning_picks} min={perf.min_rated} />
          <RateRow label="Before calibration" r={perf.uncalibrated_ranking} min={perf.min_rated} />
          {(perf.trial.ranked.rated > 0 || perf.trial.selective.rated > 0) && (
            <>
              <p className="label mb-1 mt-4">Trial: the two feeds on alternate days</p>
              <RateRow label="Earlier ranking" r={perf.trial.ranked} min={perf.min_rated} />
              <RateRow label="Selective" r={perf.trial.selective} min={perf.min_rated} />
            </>
          )}
        </Card>
        <Card className="p-5">
          <p className="label mb-1">Calibration check</p>
          {data.reliability?.length ? (
            <>
              <p className="mb-3 text-xs text-muted-foreground">Each dot is a group of your rated pieces, scored by a model that never saw them. Whiskers are 95% intervals: few pieces per group means wide whiskers.</p>
              <ReliabilityDiagram bins={data.reliability} />
            </>
          ) : (
            <p className="text-sm text-muted-foreground">Appears once there are enough rated pieces to calibrate approval estimates ({cal.labels} of {cal.needed}).</p>
          )}
        </Card>
      </div>

      <Card className="mt-6 p-5">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <p className="label">Calibration history</p>
          <Button size="sm" variant="outline" onClick={recalibrate} disabled={!cal.sufficient}>
            <RefreshCw /> Recalibrate now
          </Button>
        </div>
        {data.history.length ? (
          <ul className="divide-y text-sm">
            {data.history.map((h) => (
              <li key={h.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <span>
                  {timeAgo(h.created_at)} · {h.labels} decisions ({h.approvals} approved) · {h.map === "identity" ? "model probabilities" : "Platt-scaled"}
                  <span className="text-muted-foreground"> · Brier {h.brier.toFixed(3)} · calibration error {pct(h.ece)}</span>
                </span>
                {h.active ? (
                  <Badge variant="positive">In use</Badge>
                ) : h.usable ? (
                  <Button size="sm" variant="ghost" onClick={() => activate(h.id)}>
                    Use this one
                  </Button>
                ) : (
                  <Tip content={`Can't be used now: ${h.unusable_reason}.`}>
                    <Badge variant="muted">{h.withdrawn ? "Withdrawn" : "Not usable now"}</Badge>
                  </Tip>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">No calibration yet.</p>
        )}
      </Card>
    </div>
  );
}
