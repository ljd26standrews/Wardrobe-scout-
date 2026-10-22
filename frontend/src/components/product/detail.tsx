"use client";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";
import { AlertCircle, Bookmark, Clock3, ExternalLink, GitCompareArrows, Heart, Hourglass, RefreshCw, ScanEye, ShoppingBag, Trash2, X } from "lucide-react";
import { api, SAVED_LIST_LABEL, type Evidence, type Product } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { useJobWatcher } from "@/components/job-watcher";
import { cn, formatPrice, timeAgo, titleCase } from "@/lib/utils";
import { AlternativesPreview } from "./alternatives";
import { ConfidencePanel } from "./confidence";
import { AddEvidenceDialog, EvidenceList } from "./evidence";
import { Gallery } from "./gallery";
import { LinkButtons, OriginBadge, SpecList } from "./meta";
import { Photo } from "./photo";
import { LandedCost, Price } from "./price";
import { QualityPanel } from "./quality";
import { useFeedback, type ActionName } from "./use-feedback";

function Section({ title, children, id }: { title: string; children: React.ReactNode; id?: string }) {
  return (
    <section className="border-t py-5" aria-labelledby={id}>
      <h2 id={id} className="mb-3 text-[15px] font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

/** The decisions you can make about a piece, with the current one shown. Dislike is recorded at once; reasons are
 *  offered afterwards. */
export function DecisionBar({ product, onDone, className }: { product: Product; onDone?: () => void; className?: string }) {
  const feedback = useFeedback({ onDone });
  const list = product.saved?.list;
  const act = (a: ActionName) => feedback(product.id, a, [], undefined, { surface: "detail" }, product.title).catch(() => {});
  const btn = (a: ActionName, label: string, icon: React.ReactNode, on: boolean, testid: string) => (
    <Button size="sm" variant={on ? "default" : "outline"} aria-pressed={on} onClick={() => act(a)} data-testid={testid}>
      {icon} {label}
    </Button>
  );
  return (
    <div className={cn("flex flex-wrap gap-2", className)}>
      {btn("love", "Love", <Heart />, list === "loved", "detail-love")}
      {btn("like", "Like", <Bookmark />, list === "liked" || list === "wishlist", "detail-like")}
      {btn("like_later", "Not now", <Hourglass />, list === "later", "detail-later")}
      {btn("maybe", "Maybe", <Clock3 />, list === "maybe", "detail-maybe")}
      {btn("dislike", "Dislike", <X />, false, "detail-dislike")}
      {btn("purchased", "Bought it", <ShoppingBag />, false, "detail-purchased")}
      <Button size="sm" variant="ghost" asChild>
        <Link href={`/compare?id=${product.id}`}>
          <GitCompareArrows /> Compare offers
        </Link>
      </Button>
    </div>
  );
}

/** Evidence grouped by source: copies and excerpts of one source count once, and a source that says both is shown
 *  as mixed (as the quality score treats it). */
function EvidenceBySource({ items, onDelete }: { items: Evidence[]; onDelete?: (id: number) => void }) {
  const groups = new Map<string, Evidence[]>();
  for (const e of items) {
    const key = e.source_key ?? `item:${e.scope ?? "p"}:${e.id}`;
    groups.set(key, [...(groups.get(key) ?? []), e]);
  }
  if (!items.length) return <p className="text-sm text-muted-foreground">No evidence recorded yet. Unverified means unknown, not poor.</p>;
  return (
    <div className="space-y-3">
      {[...groups.values()].map((g) => {
        const dirs = new Set(g.map((e) => Math.sign(e.polarity)).filter((d) => d !== 0));
        return (
          <div key={`${g[0].scope ?? "p"}-${g[0].id}`}>
            {g.length > 1 && (
              <p className="meta mb-1.5">
                {g.length} excerpts from one source{dirs.size > 1 ? " · says both: counted as mixed" : " · counted once"}
              </p>
            )}
            <EvidenceList items={g} onDelete={onDelete} />
          </div>
        );
      })}
    </div>
  );
}

export function ProductDetail({ product: p, mutate, variant = "page" }: { product: Product; mutate: () => void; variant?: "page" | "panel" }) {
  const watch = useJobWatcher();
  const rec = p.recommendation;
  const [busy, setBusy] = useState(false);
  const panel = variant === "panel";
  return (
    <div className={cn("grid gap-6", !panel && "md:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)] md:gap-10")}>
      <div className={cn(!panel && "md:sticky md:top-6 md:self-start")}>
        <Gallery product={p} />
      </div>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <p className="meta">{[p.brand, p.category && titleCase(p.category)].filter(Boolean).join(" · ") || "Uncategorised"}</p>
          <OriginBadge product={p} />
          {p.saved?.list && <span className="meta">· In {SAVED_LIST_LABEL[p.saved.list] ?? p.saved.list}</span>}
        </div>
        <h1 className="mt-1.5 text-xl font-semibold leading-7 tracking-tight md:text-2xl md:leading-8" data-testid="product-title">
          {p.title}
        </h1>
        <p className="meta mt-1">
          {p.seller ? p.seller.name : "Unknown seller"}
          {p.seller && p.seller.reputation === "concerns" && <span className="text-caution"> · this seller has recorded concerns</span>}
        </p>
        <div className="mt-3 space-y-1">
          <Price product={p} size="lg" />
          <div>
            <LandedCost product={p} />
          </div>
        </div>
        <DecisionBar product={p} onDone={mutate} className="mt-4" />

        {(rec || (p.confidence && p.confidence.state !== "off")) && (
          <Section title={rec?.explore || p.confidence?.learning ? "Why you're seeing this" : "Why it's suggested"} id={`why-${p.id}`}>
            {p.confidence && p.confidence.state !== "off" && <ConfidencePanel c={p.confidence} />}
            {rec && rec.reasons.length > 0 && (
              <ul className="mt-3 space-y-1.5 text-sm leading-relaxed">
                {rec.reasons.map((r) => (
                  <li key={r} className="flex gap-2">
                    <span aria-hidden className="mt-2 size-1.5 shrink-0 rounded-full bg-primary" />
                    {r}
                  </li>
                ))}
              </ul>
            )}
            {rec && rec.cautions.length > 0 && (
              <ul className="mt-3 space-y-1.5 text-sm leading-relaxed text-muted-foreground">
                {rec.cautions.map((c) => (
                  <li key={c} className="flex gap-2">
                    <AlertCircle className="mt-0.5 size-4 shrink-0 text-caution" aria-hidden />
                    {c}
                  </li>
                ))}
              </ul>
            )}
          </Section>
        )}

        <Section title="Sizing" id={`sizing-${p.id}`}>
          <p className="text-sm">{p.sizes.length ? `Listed sizes: ${p.sizes.join(" · ")}` : <span className="text-muted-foreground">Sizes not listed.</span>}</p>
          {!!p.sizing_images?.length && (
            <>
              <p className="meta mb-2 mt-3">Size charts from the seller&apos;s gallery (kept for sizing, never used to judge the look):</p>
              <div className="no-scrollbar flex gap-2 overflow-x-auto" data-testid="sizing-images">
                {p.sizing_images.map((img) =>
                  img.src ? (
                    <a key={img.id} href={img.src} target="_blank" rel="noreferrer" className="w-40 shrink-0" aria-label="Open size chart">
                      <Photo src={img.src} alt="Size chart" className="aspect-[3/4]" />
                    </a>
                  ) : null,
                )}
              </div>
            </>
          )}
        </Section>

        <Section title="Details" id={`details-${p.id}`}>
          <SpecList product={p} />
          {p.analysis_state && p.analysis_state !== "analysed" && p.analysis_state !== "deep" && (
            <p className="meta mt-3" data-testid="analysis-state">
              {p.analysis_state === "discovered" ? "Found in the catalogue listing; its page and photos are still being analysed." : "Partly analysed; the rest is queued."}
            </p>
          )}
        </Section>

        <Section title="Quality evidence" id={`evidence-${p.id}`}>
          <QualityPanel product={p} />
          {p.evidence && (
            <div className="mt-4">
              <EvidenceBySource
                items={p.evidence}
                onDelete={async (eid) => {
                  await api.del(`/api/evidence/${eid}`);
                  mutate();
                }}
              />
            </div>
          )}
          <div className="mt-3 flex flex-wrap gap-2">
            <AddEvidenceDialog endpoint={`/api/products/${p.id}/evidence`} onAdded={mutate} />
            {!panel && (
              <Button
                size="sm"
                variant="ghost"
                onClick={async () => {
                  try {
                    const r = await api.post<{ job_id: number }>(`/api/products/${p.id}/ai-assess`);
                    watch(r.job_id, "Photo check", mutate);
                  } catch (e) {
                    toast.error(e instanceof Error ? e.message : "Unavailable");
                  }
                }}
              >
                <ScanEye /> Photo check (optional)
              </Button>
            )}
          </div>
          {!!p.missing?.length && (
            <div className="mt-4 rounded-md bg-muted p-3 text-sm">
              <p className="label mb-1.5">Missing or unverified</p>
              <ul className="list-inside list-disc space-y-1 text-muted-foreground">
                {p.missing.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            </div>
          )}
        </Section>

        <Section title="Other offers" id={`offers-${p.id}`}>
          <AlternativesPreview productId={p.id} />
        </Section>

        <Section title="Where to buy" id={`links-${p.id}`}>
          <LinkButtons product={p} />
          {panel && (
            <Link href={`/product/${p.id}`} className="mt-3 inline-flex items-center gap-1 text-sm text-primary underline-offset-4 hover:underline">
              Open full page <ExternalLink className="size-3.5" aria-hidden />
            </Link>
          )}
        </Section>

        {!panel && (p.description || p.description_original) && (
          <Section title={`Listing text${p.translation_method && p.translation_method !== "none" ? ` (translated: ${p.translation_method})` : ""}`}>
            {p.description && <p className="whitespace-pre-line text-sm leading-relaxed">{p.description}</p>}
            {p.description_original && p.description_original !== p.description && (
              <details className="mt-3 text-sm text-muted-foreground">
                <summary className="cursor-pointer">Original text</summary>
                <p className="mt-2 whitespace-pre-line">
                  {p.title_original}
                  {"\n"}
                  {p.description_original}
                </p>
              </details>
            )}
          </Section>
        )}

        {!panel && (
          <Section title="Price and stock history">
            <ul className="space-y-1.5 text-sm">
              {(p.observations ?? [])
                .slice()
                .reverse()
                .map((o, i) => (
                  <li key={i} className="flex justify-between gap-3">
                    <span className="text-muted-foreground">{timeAgo(o.observed_at)}</span>
                    <span className="text-right">
                      {formatPrice(o.price, p.currency)} · {o.change ?? "observed"}
                    </span>
                  </li>
                ))}
              {!p.observations?.length && <li className="text-muted-foreground">No observations recorded.</li>}
            </ul>
            <p className="meta mt-3">
              First seen {timeAgo(p.first_seen_at)} · last seen {timeAgo(p.last_seen_at)} · {p.status === "active" ? "for sale" : p.status.replace("_", " ")}
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={busy || (!p.source_url && !p.links.weidian)}
                onClick={async () => {
                  setBusy(true);
                  try {
                    const r = await api.post<{ job_id: number }>(`/api/products/${p.id}/refresh`);
                    watch(r.job_id, "Re-checking the listing", mutate);
                  } catch (e) {
                    toast.error(e instanceof Error ? e.message : "Cannot re-check this listing");
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <RefreshCw /> Re-check listing
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="text-destructive"
                onClick={async () => {
                  if (!confirm("Delete this product record? Your decisions about it are kept.")) return;
                  await api.del(`/api/products/${p.id}`);
                  window.location.href = "/browse";
                }}
              >
                <Trash2 /> Delete record
              </Button>
            </div>
          </Section>
        )}
      </div>
    </div>
  );
}
