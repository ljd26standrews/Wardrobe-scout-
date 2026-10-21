"use client";
import Link from "next/link";
import { use, useState } from "react";
import useSWR from "swr";
import { toast } from "sonner";
import { ArrowLeft, Bookmark, Clock3, GitCompareArrows, Heart, RefreshCw, ScanEye, ShoppingBag, Trash2, X } from "lucide-react";
import { api, type Product } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ErrorState, Skeleton } from "@/components/ui/misc";
import { Gallery } from "@/components/product/gallery";
import { ProductInfo, Section } from "@/components/product/product-detail-body";
import { AddEvidenceDialog, EvidenceList } from "@/components/product/evidence";
import { RejectDialog } from "@/components/product/reject-dialog";
import { useFeedback } from "@/components/product/use-feedback";
import { useJobWatcher } from "@/components/job-watcher";
import { formatPrice, timeAgo } from "@/lib/utils";

export default function ProductPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data: p, error, isLoading, mutate } = useSWR<Product>(`/api/products/${id}`);
  const [rejecting, setRejecting] = useState(false);
  const feedback = useFeedback(() => mutate());
  const watch = useJobWatcher();

  if (error) return <ErrorState error={error} retry={() => mutate()} />;
  if (isLoading || !p)
    return (
      <div className="grid gap-8 md:grid-cols-2">
        <Skeleton className="aspect-[4/5]" />
        <Skeleton className="h-96" />
      </div>
    );

  return (
    <div>
      <Link href="/browse" className="mb-5 inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> Back
      </Link>
      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 md:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)] md:gap-12">
        <div className="md:sticky md:top-24 md:self-start">
          <Gallery product={p} />
          <div className="mt-4 flex flex-wrap gap-2">
            <Button size="sm" variant={p.saved?.list === "loved" ? "default" : "outline"} onClick={() => feedback(p.id, "love")}>
              <Heart /> Love
            </Button>
            <Button size="sm" variant={p.saved?.list === "liked" ? "default" : "outline"} onClick={() => feedback(p.id, "like")}>
              <Bookmark /> Save
            </Button>
            <Button size="sm" variant={p.saved?.list === "maybe" ? "default" : "outline"} onClick={() => feedback(p.id, "maybe")}>
              <Clock3 /> Maybe
            </Button>
            <Button size="sm" variant="outline" onClick={() => setRejecting(true)}>
              <X /> Dislike
            </Button>
            <Button size="sm" variant="outline" onClick={() => feedback(p.id, "purchased")}>
              <ShoppingBag /> Purchased
            </Button>
            <Button size="sm" variant="outline" asChild>
              <Link href={`/compare?id=${p.id}`}>
                <GitCompareArrows /> Compare
              </Link>
            </Button>
          </div>
        </div>
        <div>
          <ProductInfo product={p} />

          <Section title="Evidence & provenance">
            <div className="mb-3 flex flex-wrap gap-2">
              <AddEvidenceDialog endpoint={`/api/products/${p.id}/evidence`} onAdded={() => mutate()} />
              <Button
                size="sm"
                variant="outline"
                onClick={async () => {
                  try {
                    const r = await api.post<{ job_id: number }>(`/api/products/${p.id}/ai-assess`);
                    watch(r.job_id, "AI photo assessment", () => mutate());
                  } catch (e) {
                    toast.error(e instanceof Error ? e.message : "Unavailable");
                  }
                }}
              >
                <ScanEye /> AI photo check
              </Button>
            </div>
            <EvidenceList
              items={p.evidence ?? []}
              onDelete={async (eid) => {
                await api.del(`/api/evidence/${eid}`);
                mutate();
              }}
            />
            {!!p.missing?.length && (
              <div className="mt-4 rounded-xl bg-muted/60 p-3 text-sm">
                <p className="label mb-2">Missing or unverified</p>
                <ul className="list-inside list-disc space-y-1 text-muted-foreground">
                  {p.missing.map((m) => (
                    <li key={m}>{m}</li>
                  ))}
                </ul>
              </div>
            )}
          </Section>

          {(p.description || p.description_original) && (
            <Section title={`Listing text${p.translation_method && p.translation_method !== "none" ? ` · translated (${p.translation_method})` : ""}`}>
              {p.description && <p className="whitespace-pre-line text-sm leading-relaxed">{p.description}</p>}
              {p.description_original && p.description_original !== p.description && (
                <details className="mt-3 text-sm text-muted-foreground">
                  <summary className="cursor-pointer">Original text</summary>
                  <p className="mt-2 whitespace-pre-line">{p.title_original}{"\n"}{p.description_original}</p>
                </details>
              )}
            </Section>
          )}

          <Section title="Price, stock & history">
            <ul className="space-y-1.5 text-sm">
              {(p.observations ?? []).slice().reverse().map((o, i) => (
                <li key={i} className="flex justify-between gap-3">
                  <span className="text-muted-foreground">{timeAgo(o.observed_at)}</span>
                  <span className="text-right">
                    {formatPrice(o.price, p.currency)} · {o.change ?? "observed"}
                  </span>
                </li>
              ))}
              {!p.observations?.length && <li className="text-muted-foreground">No observations recorded.</li>}
            </ul>
            <p className="mt-3 text-xs text-muted-foreground">
              First seen {timeAgo(p.first_seen_at)} · last seen {timeAgo(p.last_seen_at)} · status {p.status}
              {p.source_url && (
                <>
                  {" · "}
                  <a href={p.source_url} target="_blank" rel="noreferrer" className="underline">
                    source
                  </a>
                </>
              )}
            </p>
            <div className="mt-3 flex gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={!p.source_url && !p.links.weidian}
                onClick={async () => {
                  try {
                    const r = await api.post<{ job_id: number }>(`/api/products/${p.id}/refresh`);
                    watch(r.job_id, "Refreshing listing", () => mutate());
                  } catch (e) {
                    toast.error(e instanceof Error ? e.message : "Cannot refresh");
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
                  if (!confirm("Delete this product record? Feedback history is kept.")) return;
                  await api.del(`/api/products/${p.id}`);
                  window.location.href = "/browse";
                }}
              >
                <Trash2 /> Delete
              </Button>
            </div>
          </Section>
        </div>
      </div>
      <RejectDialog
        open={rejecting}
        title={p.title}
        onOpenChange={setRejecting}
        onSubmit={(reasons, note) => {
          setRejecting(false);
          feedback(p.id, "dislike", reasons, note);
        }}
      />
    </div>
  );
}
