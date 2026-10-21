"use client";
import useSWR from "swr";
import { toast } from "sonner";
import { AlertTriangle, CheckCircle2, CircleDashed, Pause, Play, RotateCcw, Square } from "lucide-react";
import { api, type Job, type ScanStats, type ScanStatus } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, Spinner } from "@/components/ui/misc";
import { cn, timeAgo } from "@/lib/utils";

const KIND_LABEL: Record<string, string> = {
  scan_seller: "Scanning seller",
  scan_all: "Scanning all sellers",
  analyse_pending: "Continuing analysis",
  reanalyse: "Classifying stored photos",
};

function fmtDuration(s: number): string {
  if (s < 90) return `${Math.max(1, Math.round(s))} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

function eta(stats: ScanStats): string {
  if (!stats.discovery_complete) return "Estimating once the catalogue listing has been read";
  const e = stats.eta_seconds;
  if (!e) return "Estimating…";
  if (e[1] < 5) return "Finishing";
  return `About ${fmtDuration(e[0])}–${fmtDuration(e[1])} remaining`;
}

function Stat({ label, value, hint, testid }: { label: string; value: React.ReactNode; hint?: string; testid?: string }) {
  return (
    <div title={hint}>
      <dt className="label">{label}</dt>
      <dd className="mt-1 tabular-nums text-lg" data-testid={testid}>
        {value}
      </dd>
    </div>
  );
}

function Bar({ value, className }: { value: number; className?: string }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-secondary">
      <div className={cn("h-full rounded-full bg-foreground/80 transition-[width]", className)} style={{ width: `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%` }} />
    </div>
  );
}

function ActiveScan({ job, refresh }: { job: Job; refresh: () => void }) {
  const s = job.stats;
  const control = async (action: "pause" | "resume" | "cancel") => {
    try {
      await api.post(`/api/jobs/${job.id}/control`, { action });
      refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not change the scan");
    }
  };
  const paused = job.control === "pause" || s?.stage === "paused";
  const pending = s?.pending_products ?? 0;
  const analysisFrac = s && s.discovery_complete ? s.products_analysed / Math.max(1, s.products_analysed + pending) : null;
  return (
    <Card className="p-5" data-testid="scan-panel-active">
      <div className="flex flex-wrap items-center gap-3">
        {paused ? <Pause className="size-4" /> : <Spinner />}
        <h3 className="font-serif text-2xl">{KIND_LABEL[job.kind] ?? job.kind}</h3>
        <Badge variant="outline" data-testid="scan-stage">
          {paused ? "paused" : job.control === "cancel" ? "cancelling" : s?.stage ?? job.status}
        </Badge>
        <div className="ml-auto flex gap-1.5">
          {job.status === "running" &&
            (paused ? (
              <Button size="sm" variant="outline" onClick={() => control("resume")} data-testid="scan-resume">
                <Play /> Resume
              </Button>
            ) : (
              <Button size="sm" variant="outline" onClick={() => control("pause")} data-testid="scan-pause">
                <Pause /> Pause
              </Button>
            ))}
          <Button size="sm" variant="ghost" onClick={() => control("cancel")} disabled={job.control === "cancel"} data-testid="scan-cancel">
            <Square /> Cancel
          </Button>
        </div>
      </div>
      {!s ? (
        <p className="mt-3 text-sm text-muted-foreground">{job.message ?? "Starting…"}</p>
      ) : (
        <>
          <div className="mt-5 grid gap-6 md:grid-cols-2">
            <div>
              <p className="label">1 · Catalogue discovery</p>
              <ul className="mt-2 space-y-2 text-sm">
                {(s.sellers ?? []).map((sl) => (
                  <li key={sl.id}>
                    <div className="flex items-center gap-2">
                      {sl.status ? <AlertTriangle className="size-4 text-destructive" /> : sl.discovery_done ? <CheckCircle2 className="size-4 text-positive" /> : <CircleDashed className="size-4 animate-spin text-muted-foreground" />}
                      <span className="truncate">{sl.name}</span>
                      <span className="ml-auto tabular-nums text-xs text-muted-foreground">
                        {sl.pages}
                        {sl.total_pages ? `/${sl.total_pages}` : ""} pages · {sl.discovered} products
                      </span>
                    </div>
                    {!sl.discovery_done && sl.total_pages ? <Bar value={sl.pages / sl.total_pages} className="mt-1" /> : null}
                    {sl.discovery_done && !sl.status && !sl.catalogue_complete && (
                      <p className="mt-0.5 text-xs text-caution">Listing not read to the end (page limit) — removals are not checked.</p>
                    )}
                    {sl.status && <p className="mt-0.5 text-xs text-destructive">{sl.status}</p>}
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-muted-foreground">Discovered products are browsable straight away.</p>
            </div>
            <div>
              <p className="label">2 · Product pages &amp; photos</p>
              <p className="mt-2 text-sm">
                <span className="tabular-nums" data-testid="scan-analysed">{s.products_analysed}</span> analysed
                {s.discovery_complete ? <> · <span className="tabular-nums">{pending}</span> products waiting</> : null}
              </p>
              {analysisFrac !== null ? <Bar value={analysisFrac} className="mt-2" /> : <p className="mt-2 text-xs text-muted-foreground">Progress shown once the listing has been read (the total isn&apos;t known before).</p>}
              <p className="mt-2 text-xs text-muted-foreground" data-testid="scan-eta">
                {eta(s)}
                {s.throughput_per_min ? ` · ${s.throughput_per_min} products/min` : ""}
              </p>
            </div>
          </div>
          <dl className="mt-6 grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4 lg:grid-cols-6">
            <Stat label="Discovered" value={s.discovered} testid="scan-discovered" />
            <Stat label="New" value={s.new} />
            <Stat label="Unchanged, skipped" value={s.unchanged_skipped} hint="Already analysed and unchanged in the listing: no requests made" />
            <Stat label="Product pages read" value={s.details_fetched} hint={`${s.details_not_modified} more answered "not modified" (no download)`} />
            <Stat label="Photos downloaded" value={`${s.images_downloaded}`} hint={`${(s.image_bytes / 1e6).toFixed(1)} MB; ${s.images_reused} reused from other listings`} />
            <Stat label="Size charts excluded" value={s.size_charts_excluded} testid="scan-size-charts" hint="Kept for the sizing section, never used for visual taste" />
            <Stat label="Text & banners excluded" value={s.text_graphics_excluded + s.repeated_excluded} />
            <Stat label="Duplicate links" value={s.duplicates_found} hint="Listings sharing several identical photos or the same purchase link" />
            <Stat label="Removed" value={s.removed} hint="Missing from the full listing and their own page is gone" />
            <Stat label="Requests" value={s.requests ?? 0} />
            <Stat label="Elapsed" value={fmtDuration(s.elapsed_s ?? 0)} />
            <Stat label="Errors" value={s.errors} />
          </dl>
          {s.error_samples?.length > 0 && (
            <details className="mt-4 text-xs text-muted-foreground">
              <summary className="cursor-pointer">Errors and blocked sources ({s.errors})</summary>
              <ul className="mt-2 space-y-1">
                {s.error_samples.map((e) => (
                  <li key={e} className="break-words">
                    {e}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </Card>
  );
}

/** Live scan progress and coverage. Reads everything from the server, so it survives reloads and closed tabs. */
export function ScanPanel({ onChange }: { onChange?: () => void }) {
  const { data, mutate } = useSWR<ScanStatus>("/api/scan/status", {
    refreshInterval: (d) => (d?.active?.length ? 1500 : 10000),
  });
  const refresh = () => {
    mutate();
    onChange?.();
  };
  if (!data) return null;
  const last = data.last;
  const resumable = !data.active.length && data.pending_analysis > 0;
  return (
    <div className="space-y-3" data-testid="scan-panel">
      {data.active.map((j) => (
        <ActiveScan key={j.id} job={j} refresh={refresh} />
      ))}
      {!data.active.length && last && (
        <Card className="flex flex-wrap items-center gap-3 p-4 text-sm" data-testid="scan-panel-last">
          {last.status === "succeeded" ? <CheckCircle2 className="size-4 text-positive" /> : <AlertTriangle className="size-4 text-caution" />}
          <span>
            Last scan {last.status} {last.finished_at ? timeAgo(last.finished_at) : ""}
            {last.stats ? ` · ${last.stats.discovered} discovered, ${last.stats.new} new, ${last.stats.products_analysed} analysed, ${last.stats.size_charts_excluded} size charts excluded` : ""}
          </span>
          <div className="ml-auto flex gap-1.5">
            {resumable && (
              <Button
                size="sm"
                variant="outline"
                data-testid="scan-continue"
                onClick={async () => {
                  await api.post("/api/scan/continue");
                  refresh();
                }}
              >
                <Play /> Continue analysis ({data.pending_analysis})
              </Button>
            )}
            {["failed", "cancelled", "partial"].includes(last.status) && (
              <Button
                size="sm"
                variant="ghost"
                onClick={async () => {
                  await api.post(`/api/jobs/${last.id}/retry`);
                  refresh();
                }}
              >
                <RotateCcw /> Retry
              </Button>
            )}
          </div>
        </Card>
      )}
      {data.sellers.some((s) => s.products > 0) && (
        <Card className="p-4">
          <p className="label">Catalogue coverage</p>
          <ul className="mt-3 space-y-2 text-sm">
            {data.sellers
              .filter((s) => s.products > 0)
              .map((s) => {
                const done = s.states.analysed + s.states.deep;
                return (
                  <li key={s.id} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1">
                    <span className="truncate">{s.name}</span>
                    <span className="text-xs tabular-nums text-muted-foreground">
                      {done}/{s.products} analysed
                      {s.states.discovered + s.states.partial ? ` · ${s.states.discovered + s.states.partial} pending` : ""}
                      {s.catalogue_complete === false ? " · listing not fully read" : s.catalogue_complete ? " · whole listing read" : ""}
                    </span>
                    <div className="col-span-2">
                      <Bar value={done / Math.max(1, s.products)} />
                    </div>
                  </li>
                );
              })}
          </ul>
        </Card>
      )}
    </div>
  );
}
