"use client";
import Link from "next/link";
import useSWR from "swr";
import { toast } from "sonner";
import { Undo2 } from "lucide-react";
import { api } from "@/lib/api";
import { Card, ErrorState, Meter, Skeleton } from "@/components/ui/misc";
import { cn, formatPrice, timeAgo, titleCase } from "@/lib/utils";

type Attr = { key: string; label: string; affinity: number; evidence: number; examples: string[] };
type InsightsResponse = {
  learning_strength: number;
  stage: string;
  decisions: { informative: number; positive: number; negative: number; seed_examples: number };
  visual_examples: { positive: number; negative: number };
  attributes: Record<string, Attr[]>;
  price_ceilings: Record<string, number>;
  price_tolerance: Record<string, number>;
  redundant_categories: Record<string, number>;
  rejection_reasons: Record<string, number>;
  construction_concerns: number;
  palette_neutral_share: number | null;
  effective_weights: Record<string, number>;
  weights: Record<string, number>;
  embedding: { image_backend: string; is_clip: boolean; note: string | null };
  top_explained: { id: number; title: string; score: number; contributions: Record<string, number>; reasons: string[]; cautions: string[]; image: string | null }[];
};
type FeedbackRow = { id: number; product_id: number | null; action: string; reasons: string[]; note: string | null; title: string | null; seller: string | null; created_at: string };

const GROUP_LABEL: Record<string, string> = { colour: "Colours", category: "Garment types", fibre: "Materials", brand: "Brands / styles", seller: "Sellers", fit: "Fit & silhouette", logo: "Branding visibility" };
const COMPONENT_LABEL: Record<string, string> = {
  rules: "Written preferences",
  learned_attributes: "Learned attributes",
  visual: "Visual similarity",
  semantic: "Description similarity",
  quality: "Quality evidence",
  value: "Value",
  wardrobe: "Wardrobe fit",
  seller: "Seller credibility",
};

function Stat({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <Card className="p-4">
      <p className="label">{label}</p>
      <p className="mt-2 font-serif text-4xl leading-none tabular-nums">{value}</p>
      {sub && <p className="mt-2 text-xs text-muted-foreground">{sub}</p>}
    </Card>
  );
}

/** Diverging bar: blue = attraction, clay = aversion, neutral axis at zero. Value printed in ink with sign. */
function DivergingBar({ a }: { a: Attr }) {
  const v = Math.max(-1, Math.min(1, a.affinity));
  const pct = Math.abs(v) * 50;
  return (
    <div className="group grid grid-cols-[minmax(0,8rem)_1fr_3rem] items-center gap-3 py-1.5" title={`${a.label}: ${v >= 0 ? "+" : ""}${v.toFixed(2)} from ${a.evidence} weighted decision(s)${a.examples.length ? ` — e.g. ${a.examples.join(", ")}` : ""}`}>
      <span className="truncate text-[13px]">{titleCase(a.label)}</span>
      <div className="relative h-3">
        <div className="absolute inset-y-[-3px] left-1/2 w-px bg-border" />
        <div
          className={cn("absolute top-0 h-full transition-all group-hover:opacity-80", v >= 0 ? "left-1/2 rounded-r-[4px] bg-divpos" : "right-1/2 rounded-l-[4px] bg-divneg")}
          style={{ width: `${Math.max(pct, 1)}%` }}
        />
      </div>
      <span className="text-right text-xs tabular-nums text-muted-foreground">
        {v >= 0 ? "+" : "−"}
        {Math.abs(v).toFixed(2)}
      </span>
    </div>
  );
}

export default function InsightsPage() {
  const { data, error, isLoading, mutate } = useSWR<InsightsResponse>("/api/insights");
  const { data: history, mutate: mutateHistory } = useSWR<FeedbackRow[]>("/api/feedback?limit=60");

  if (error) return <ErrorState error={error} retry={() => mutate()} />;
  if (isLoading || !data) return <Skeleton className="h-[60vh]" />;
  const groups = Object.entries(data.attributes).filter(([, v]) => v.length);

  return (
    <div>
      <p className="label">What the system has learned</p>
      <h1 className="page-title mt-2">Insights</h1>
      <p className="mt-3 max-w-2xl text-sm leading-relaxed text-muted-foreground">{data.stage}</p>

      <div className="mt-8 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Learned weight" value={`${Math.round(data.learning_strength * 100)}%`} sub="share of ranking driven by your behaviour" />
        <Stat label="Informative decisions" value={data.decisions.informative} sub={`${data.decisions.positive} positive · ${data.decisions.negative} negative`} />
        <Stat label="Visual examples" value={data.visual_examples.positive + data.visual_examples.negative} sub={`${data.visual_examples.positive} approved · ${data.visual_examples.negative} rejected`} />
        <Stat label="Neutral palette share" value={data.palette_neutral_share == null ? "—" : `${Math.round(data.palette_neutral_share * 100)}%`} sub="of positive colour signal" />
      </div>
      {!data.embedding.is_clip && data.embedding.note && <p className="mt-4 rounded-xl bg-muted/70 p-3 text-xs leading-relaxed text-muted-foreground">Image model: {data.embedding.image_backend}. {data.embedding.note}</p>}

      <h2 className="mt-14 text-3xl">Learned preferences</h2>
      <div className="mt-2 flex items-center gap-4 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5"><span className="inline-block h-2.5 w-4 rounded-sm bg-divpos" /> drawn to</span>
        <span className="inline-flex items-center gap-1.5"><span className="inline-block h-2.5 w-4 rounded-sm bg-divneg" /> steer away</span>
        <span>Values shrink towards zero until there is enough evidence.</span>
      </div>
      {groups.length === 0 ? (
        <p className="mt-6 text-sm text-muted-foreground">No learned attributes yet — make some decisions in Discover.</p>
      ) : (
        <div className="mt-6 grid gap-4 md:grid-cols-2">
          {groups.map(([k, attrs]) => (
            <Card key={k} className="p-5">
              <p className="label mb-3">{GROUP_LABEL[k] ?? k}</p>
              {attrs.slice(0, 10).map((a) => (
                <DivergingBar key={a.key} a={a} />
              ))}
            </Card>
          ))}
        </div>
      )}

      <div className="mt-14 grid gap-6 lg:grid-cols-2">
        <Card className="p-5">
          <p className="label mb-1">How the ranking is weighted now</p>
          <p className="mb-4 text-xs text-muted-foreground">
            Base weights from <Link href="/settings" className="underline">Settings</Link>, scaled by learning strength.
          </p>
          {Object.entries(data.effective_weights).map(([k, v]) => (
            <div key={k} className="py-1.5">
              <div className="mb-1 flex justify-between text-[13px]">
                <span>{COMPONENT_LABEL[k] ?? k}</span>
                <span className="tabular-nums text-muted-foreground">
                  {v.toFixed(2)} <span className="opacity-60">/ {data.weights[k]?.toFixed(1)}</span>
                </span>
              </div>
              <Meter value={(v / 3) * 100} />
            </div>
          ))}
        </Card>
        <div className="space-y-6">
          <Card className="p-5">
            <p className="label mb-3">Value ceilings (from “too expensive” rejections)</p>
            {Object.keys(data.price_ceilings).length ? (
              <ul className="space-y-1.5 text-sm">
                {Object.entries(data.price_ceilings).map(([cat, p]) => (
                  <li key={cat} className="flex justify-between">
                    <span>{titleCase(cat)}</span>
                    <span className="text-muted-foreground">unproven quality above ~{formatPrice(p)} is penalised</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">None yet.</p>
            )}
            <p className="mt-3 text-xs text-muted-foreground">These only affect value — they never count as disliking the style.</p>
          </Card>
          <Card className="p-5">
            <p className="label mb-3">Rejection reasons given</p>
            {Object.keys(data.rejection_reasons).length ? (
              <ul className="space-y-1.5 text-sm">
                {Object.entries(data.rejection_reasons).map(([r, n]) => (
                  <li key={r} className="flex justify-between">
                    <span>{r}</span>
                    <span className="tabular-nums text-muted-foreground">{n}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">None yet.</p>
            )}
          </Card>
        </div>
      </div>

      {data.top_explained.length > 0 && (
        <>
          <h2 className="mt-14 text-3xl">Why the top picks rank highly</h2>
          <div className="mt-6 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {data.top_explained.map((t) => {
              const contribs = Object.entries(t.contributions).filter(([, v]) => Math.abs(v) > 0.001).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
              const max = Math.max(...contribs.map(([, v]) => Math.abs(v)), 0.001);
              return (
                <Card key={t.id} className="flex gap-4 p-4">
                  <div className="h-28 w-20 shrink-0 overflow-hidden rounded-lg bg-muted">
                    {t.image && (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={t.image} alt="" className="h-full w-full object-cover" />
                    )}
                  </div>
                  <div className="min-w-0 flex-1">
                    <Link href={`/product/${t.id}`} className="line-clamp-2 text-sm hover:underline">
                      {t.title}
                    </Link>
                    <div className="mt-2 space-y-1">
                      {contribs.slice(0, 4).map(([k, v]) => (
                        <div key={k} className="grid grid-cols-[6.5rem_1fr] items-center gap-2 text-[11px]" title={`${COMPONENT_LABEL[k]}: ${v > 0 ? "+" : ""}${v.toFixed(3)}`}>
                          <span className="truncate text-muted-foreground">{COMPONENT_LABEL[k]}</span>
                          <div className="h-1.5 rounded-full bg-secondary">
                            <div className={cn("h-full rounded-full", v >= 0 ? "bg-divpos" : "bg-divneg")} style={{ width: `${(Math.abs(v) / max) * 100}%` }} />
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </Card>
              );
            })}
          </div>
        </>
      )}

      <h2 className="mt-14 text-3xl">Decision history</h2>
      <ul className="mt-4 divide-y rounded-2xl border bg-card">
        {(history ?? []).map((f) => (
          <li key={f.id} className="flex items-start gap-3 p-3 text-sm">
            <span className={cn("mt-0.5 w-20 shrink-0 text-xs uppercase tracking-label", f.action === "dislike" ? "text-divneg" : f.action === "skip" || f.action === "unsave" ? "text-muted-foreground" : "text-divpos")}>{f.action}</span>
            <div className="min-w-0 flex-1">
              {f.product_id ? (
                <Link href={`/product/${f.product_id}`} className="hover:underline">
                  {f.title ?? "product"}
                </Link>
              ) : (
                <span>{f.title}</span>
              )}
              <p className="text-xs text-muted-foreground">
                {[f.seller, f.reasons.join(", "), f.note].filter(Boolean).join(" · ")} · {timeAgo(f.created_at)}
              </p>
            </div>
            <button
              aria-label="Undo decision"
              className="text-muted-foreground hover:text-foreground"
              onClick={async () => {
                await api.del(`/api/feedback/${f.id}`);
                toast("Decision removed");
                mutateHistory();
                mutate();
              }}
            >
              <Undo2 className="size-4" />
            </button>
          </li>
        ))}
        {!history?.length && <li className="p-4 text-sm text-muted-foreground">No decisions yet.</li>}
      </ul>
    </div>
  );
}
