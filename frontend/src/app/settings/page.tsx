"use client";
import { useEffect, useState } from "react";
import useSWR from "swr";
import { toast } from "sonner";
import { Database, Download, RefreshCw, RotateCcw, Upload } from "lucide-react";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, ErrorState, Skeleton } from "@/components/ui/misc";
import { Field, Input, Textarea } from "@/components/ui/input";
import { Slider, Switch } from "@/components/ui/switch";
import { useJobWatcher } from "@/components/job-watcher";
import { timeAgo, titleCase } from "@/lib/utils";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Settings = Record<string, any>;
type Status = {
  products: { total: number; by_origin: Record<string, number> };
  embedding: { image_backend: string; is_clip: boolean; note: string | null };
  ai: { enabled: boolean; configured_enabled: boolean; api_key_present: boolean; model: string; month_spend_usd: number; monthly_cap_usd: number; remaining_usd: number };
  playwright_enabled: boolean;
};

function Section({ title, description, children, onSave, onReset, dirty }: { title: string; description?: string; children: React.ReactNode; onSave?: () => void; onReset?: () => void; dirty?: boolean }) {
  return (
    <Card className="p-5 md:p-6">
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl">{title}</h2>
          {description && <p className="mt-1 max-w-xl text-sm text-muted-foreground">{description}</p>}
        </div>
        <div className="flex gap-2">
          {onReset && (
            <Button size="sm" variant="ghost" onClick={onReset}>
              <RotateCcw /> Defaults
            </Button>
          )}
          {onSave && (
            <Button size="sm" onClick={onSave} disabled={!dirty}>
              Save
            </Button>
          )}
        </div>
      </div>
      {children}
    </Card>
  );
}

function useSection(all: Settings | undefined, key: string, refresh: () => void) {
  const [value, setValue] = useState<any>(null);
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (all && !dirty) setValue(structuredClone(all[key]));
  }, [all, key, dirty]);
  const update = (patch: any) => {
    setValue((v: any) => ({ ...v, ...patch }));
    setDirty(true);
  };
  const save = async () => {
    try {
      await api.put(`/api/settings/${key}`, value);
      setDirty(false);
      toast.success(`${titleCase(key.replace(/_/g, " "))} saved`);
      refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not save");
    }
  };
  const reset = async () => {
    await api.post(`/api/settings/${key}/reset`);
    setDirty(false);
    refresh();
    toast("Restored defaults");
  };
  return { value, update, save, reset, dirty };
}

function ListInput({ value, onChange, placeholder }: { value: string[]; onChange: (v: string[]) => void; placeholder?: string }) {
  const [text, setText] = useState(value.join(", "));
  useEffect(() => setText(value.join(", ")), [value]);
  return <Input value={text} placeholder={placeholder} onChange={(e) => setText(e.target.value)} onBlur={() => onChange(text.split(",").map((s) => s.trim()).filter(Boolean))} />;
}

function NumberInput({ value, onChange, step = "any" }: { value: number | null; onChange: (v: number) => void; step?: string }) {
  return <Input type="number" step={step} inputMode="decimal" value={value ?? ""} onChange={(e) => onChange(Number(e.target.value))} />;
}

const WEIGHT_HELP: Record<string, string> = {
  taste: "The learned taste model (includes your written preferences as its starting point)",
  rules: "Legacy engine only: your written preferences",
  learned_attributes: "Legacy engine only: attributes learned from decisions",
  visual: "Legacy engine only: image similarity to approved vs rejected items",
  semantic: "Legacy engine only: description similarity",
  quality: "Independent quality evidence",
  value: "Price relative to quality and alternatives",
  wardrobe: "Fills gaps vs adds redundancy",
  seller: "Seller credibility and your priority",
};

export default function SettingsPage() {
  const { data, error, isLoading, mutate } = useSWR<Settings>("/api/settings");
  const { data: status, mutate: mutateStatus } = useSWR<Status>("/api/status");
  const refresh = () => {
    mutate();
    mutateStatus();
  };
  const taste = useSection(data, "taste_profile", refresh);
  const weights = useSection(data, "ranking_weights", refresh);
  const rec = useSection(data, "recommender", refresh);
  const currency = useSection(data, "currency", refresh);
  const landed = useSection(data, "landed_cost", refresh);
  const ai = useSection(data, "ai", refresh);
  const schedule = useSection(data, "schedule", refresh);
  const community = useSection(data, "community", refresh);
  const watch = useJobWatcher();

  if (error) return <ErrorState error={error} retry={() => mutate()} />;
  if (isLoading || !data || !taste.value || !weights.value) return <Skeleton className="h-[70vh]" />;

  return (
    <div>
      <p className="label">Configuration</p>
      <h1 className="page-title mt-2">Settings</h1>
      <div className="mt-8 grid gap-6">
        <Section title="Taste profile" description="Your starting preferences. They're a cold-start prior — your actual decisions gradually outweigh them." onSave={taste.save} onReset={taste.reset} dirty={taste.dirty}>
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Preferred colours">
              <ListInput value={taste.value.preferred_colours} onChange={(v) => taste.update({ preferred_colours: v })} />
            </Field>
            <Field label="Preferred materials">
              <ListInput value={taste.value.preferred_fibres} onChange={(v) => taste.update({ preferred_fibres: v })} />
            </Field>
            <Field label="Materials to avoid">
              <ListInput value={taste.value.avoid_fibres} onChange={(v) => taste.update({ avoid_fibres: v })} />
            </Field>
            <Field label="Garment types of interest">
              <ListInput value={taste.value.preferred_categories} onChange={(v) => taste.update({ preferred_categories: v })} />
            </Field>
            <Field label="Preferred fits">
              <ListInput value={taste.value.preferred_fits} onChange={(v) => taste.update({ preferred_fits: v })} />
            </Field>
            <Field label="Fits to avoid">
              <ListInput value={taste.value.avoid_fits} onChange={(v) => taste.update({ avoid_fits: v })} />
            </Field>
            <Field label="Favourite brands / design languages">
              <ListInput value={taste.value.favourite_brands} onChange={(v) => taste.update({ favourite_brands: v })} />
            </Field>
            <Field label="Season focus">
              <ListInput value={taste.value.season_focus} onChange={(v) => taste.update({ season_focus: v })} />
            </Field>
            <Field label="Notes" className="md:col-span-2">
              <Textarea value={taste.value.notes} onChange={(e) => taste.update({ notes: e.target.value })} rows={2} />
            </Field>
          </div>
        </Section>

        <Section title="Ranking" description="Adjust how much each signal matters. The Insights page shows the effective weights after learning-strength scaling." onSave={async () => { await weights.save(); await rec.save(); }} onReset={async () => { await weights.reset(); await rec.reset(); }} dirty={weights.dirty || rec.dirty}>
          <div className="grid gap-x-10 gap-y-5 md:grid-cols-2">
            {Object.entries(weights.value as Record<string, number>).map(([k, v]) => (
              <div key={k}>
                <div className="flex justify-between text-sm">
                  <span>{titleCase(k.replace(/_/g, " "))}</span>
                  <span className="tabular-nums text-muted-foreground">{v.toFixed(1)}</span>
                </div>
                <Slider min={0} max={3} step={0.1} value={[v]} onValueChange={([nv]) => weights.update({ [k]: nv })} aria-label={k} />
                <p className="text-xs text-muted-foreground">{WEIGHT_HELP[k]}</p>
              </div>
            ))}
            {rec.value && (
              <>
                <div className="md:col-span-2">
                  <Field label="Recommendation engine" hint="Bayesian learns your taste with uncertainty and was markedly better in evaluation; Legacy is the original hand-weighted score, kept for comparison.">
                    <select
                      className="flex h-10 w-full max-w-sm rounded-lg border border-input bg-card px-3 text-sm"
                      value={rec.value.engine ?? "bayes"}
                      onChange={(e) => rec.update({ engine: e.target.value })}
                      aria-label="Recommendation engine"
                    >
                      <option value="bayes">Bayesian taste model (recommended)</option>
                      <option value="legacy">Legacy hand-weighted score</option>
                    </select>
                  </Field>
                </div>
                <div>
                  <div className="flex justify-between text-sm">
                    <span>Exploration</span>
                    <span className="tabular-nums text-muted-foreground">{Math.round(rec.value.exploration_rate * 100)}%</span>
                  </div>
                  <Slider min={0} max={0.4} step={0.01} value={[rec.value.exploration_rate]} onValueChange={([nv]) => rec.update({ exploration_rate: nv })} aria-label="exploration" />
                  <p className="text-xs text-muted-foreground">Share of suggestions that deliberately test new designers, styles and categories.</p>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Decisions to 50% learned" hint="Higher = slower to trust learned signals">
                    <NumberInput value={rec.value.cold_start_k} step="1" onChange={(v) => rec.update({ cold_start_k: v })} />
                  </Field>
                  <Field label="Skipped items return after (days)">
                    <NumberInput value={rec.value.skip_resurface_days} step="1" onChange={(v) => rec.update({ skip_resurface_days: v })} />
                  </Field>
                </div>
              </>
            )}
          </div>
        </Section>

        <div className="grid gap-6 lg:grid-cols-2">
          <Section title="Currency" onSave={currency.save} dirty={currency.dirty}>
            <Field label="CNY → GBP" hint={`${currency.value?.rate_source ?? ""}${currency.value?.rate_updated_at ? ` · ${timeAgo(currency.value.rate_updated_at)}` : ""}`}>
              <NumberInput value={currency.value?.cny_to_gbp} onChange={(v) => currency.update({ cny_to_gbp: v, rate_source: "set manually" })} />
            </Field>
            <Button
              size="sm"
              variant="outline"
              className="mt-3"
              onClick={async () => {
                try {
                  await api.post("/api/settings/currency/refresh");
                  toast.success("Exchange rate updated");
                  refresh();
                } catch (e) {
                  toast.error(e instanceof Error ? e.message : "Could not fetch rate");
                }
              }}
            >
              <RefreshCw /> Fetch ECB rate
            </Button>
          </Section>

          <Section title="UK landed cost" description="Assumptions used for the estimate on every product." onSave={landed.save} onReset={landed.reset} dirty={landed.dirty}>
            {landed.value && (
              <div className="grid grid-cols-2 gap-3">
                {(
                  [
                    ["domestic_shipping_cny", "China domestic shipping ¥"],
                    ["agent_service_fee_pct", "Agent fee %"],
                    ["intl_base_gbp", "International base £"],
                    ["intl_per_kg_gbp", "International £/kg"],
                    ["vat_pct", "Import VAT %"],
                    ["duty_pct", "Customs duty %"],
                    ["duty_threshold_gbp", "Duty threshold £"],
                    ["carrier_handling_fee_gbp", "Carrier handling £"],
                  ] as const
                ).map(([k, label]) => (
                  <Field key={k} label={label}>
                    <NumberInput value={landed.value[k]} onChange={(v) => landed.update({ [k]: v })} />
                  </Field>
                ))}
              </div>
            )}
          </Section>
        </div>

        <Section title="AI assistance (optional)" description="Translation, photo observations and research summaries. Off by default; every call is cached, rate-limited and capped." onSave={ai.save} dirty={ai.dirty}>
          {ai.value && status && (
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-3 md:col-span-2">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <Badge variant={status.ai.api_key_present ? "positive" : "muted"}>{status.ai.api_key_present ? "API key found" : "No ANTHROPIC_API_KEY in environment"}</Badge>
                  <Badge variant="outline">
                    Spent ${status.ai.month_spend_usd.toFixed(2)} of ${status.ai.monthly_cap_usd} this month
                  </Badge>
                </div>
                <label className="flex items-center justify-between gap-3 text-sm">
                  Enable AI features
                  <Switch checked={ai.value.enabled} onCheckedChange={(v) => ai.update({ enabled: v })} />
                </label>
              </div>
              <Field label="Model">
                <Input value={ai.value.model} onChange={(e) => ai.update({ model: e.target.value })} />
              </Field>
              <Field label="Monthly spending cap (USD)">
                <NumberInput value={ai.value.monthly_cap_usd} onChange={(v) => ai.update({ monthly_cap_usd: v })} />
              </Field>
              <Field label="Input price $/M tokens">
                <NumberInput value={ai.value.price_input_per_mtok} onChange={(v) => ai.update({ price_input_per_mtok: v })} />
              </Field>
              <Field label="Output price $/M tokens">
                <NumberInput value={ai.value.price_output_per_mtok} onChange={(v) => ai.update({ price_output_per_mtok: v })} />
              </Field>
              {(
                [
                  ["use_for_translation", "Translate Chinese listings"],
                  ["use_for_visual_assessment", "Photo observations (labelled as AI, never proof of materials)"],
                  ["use_for_research_summaries", "Summarise community research"],
                ] as const
              ).map(([k, label]) => (
                <label key={k} className="flex items-center justify-between gap-3 text-sm md:col-span-2">
                  {label}
                  <Switch checked={ai.value[k]} onCheckedChange={(v) => ai.update({ [k]: v })} />
                </label>
              ))}
            </div>
          )}
        </Section>

        <div className="grid gap-6 lg:grid-cols-2">
          <Section title="Catalogue refresh" onSave={schedule.save} dirty={schedule.dirty}>
            {schedule.value && (
              <div className="grid gap-4">
                <label className="flex items-center justify-between gap-3 text-sm">
                  Automatic scheduled scans
                  <Switch checked={schedule.value.enabled} onCheckedChange={(v) => schedule.update({ enabled: v })} />
                </label>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Default interval (hours)">
                    <NumberInput value={schedule.value.default_interval_hours} step="1" onChange={(v) => schedule.update({ default_interval_hours: v })} />
                  </Field>
                  <Field label="Max new/updated per scan">
                    <NumberInput value={schedule.value.max_products_per_scan} step="1" onChange={(v) => schedule.update({ max_products_per_scan: v })} />
                  </Field>
                </div>
                <p className="text-xs text-muted-foreground">Scans run one at a time with a polite delay between requests, honour robots.txt and stop on any block or verification page.</p>
              </div>
            )}
          </Section>
          <Section title="Community research" onSave={community.save} dirty={community.dirty}>
            {community.value && (
              <Field label="Subreddits searched" hint="Reddit's public read-only search. Results become leads you review — never automatic evidence.">
                <ListInput value={community.value.subreddits} onChange={(v) => community.update({ subreddits: v })} />
              </Field>
            )}
          </Section>
        </div>

        <Section title="System & data">
          <div className="grid gap-6 md:grid-cols-2">
            <div className="space-y-3 text-sm">
              <p>
                <span className="label mr-2">Image model</span>
                {status?.embedding.image_backend}
              </p>
              {status?.embedding.note && <p className="text-xs leading-relaxed text-muted-foreground">{status.embedding.note}</p>}
              <p>
                <span className="label mr-2">Products</span>
                {status?.products.total} ({Object.entries(status?.products.by_origin ?? {}).map(([k, v]) => `${v} ${k}`).join(", ")})
              </p>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={async () => { const r = await api.post<{ job_id: number }>("/api/maintenance/recompute"); watch(r.job_id, "Recomputing embeddings", refresh); }}>
                  <RefreshCw /> Recompute embeddings
                </Button>
                <Button size="sm" variant="outline" onClick={async () => { const r = await api.post<{ job_id: number }>("/api/maintenance/dedup"); watch(r.job_id, "Re-matching duplicates", refresh); }}>
                  Re-run duplicate matching
                </Button>
              </div>
            </div>
            <div className="space-y-3 text-sm">
              <p className="text-muted-foreground">Back up your preferences, decisions, saved lists, wardrobe (with photos), inspiration and evidence.</p>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" asChild>
                  {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- file download, not a page */}
                  <a href="/api/backup/export" download>
                    <Download /> Export backup
                  </a>
                </Button>
                <label className="inline-flex h-8 cursor-pointer items-center gap-2 rounded-full border px-3.5 text-xs hover:bg-secondary">
                  <Upload className="size-4" /> Import backup
                  <input
                    type="file"
                    accept="application/json,.json"
                    className="hidden"
                    onChange={async (e) => {
                      const f = e.target.files?.[0];
                      if (!f) return;
                      const fd = new FormData();
                      fd.append("file", f);
                      try {
                        const r = await api.form<Record<string, number>>("/api/backup/import", fd);
                        toast.success("Backup merged", { description: Object.entries(r).filter(([, v]) => v).map(([k, v]) => `${v} ${k}`).join(", ") || "nothing new" });
                        refresh();
                      } catch (err) {
                        toast.error(err instanceof Error ? err.message : "Import failed");
                      }
                      e.target.value = "";
                    }}
                  />
                </label>
                <Button size="sm" variant="outline" asChild>
                  {/* eslint-disable-next-line @next/next/no-html-link-for-pages -- file download, not a page */}
                  <a href="/api/backup/database" download>
                    <Database /> Download database
                  </a>
                </Button>
              </div>
            </div>
          </div>
        </Section>
      </div>
    </div>
  );
}
