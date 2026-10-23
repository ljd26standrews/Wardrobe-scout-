"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import useSWR from "swr";
import { toast } from "sonner";
import { ClipboardCheck, Undo2 } from "lucide-react";
import type { Product } from "@/lib/api";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chips";
import { EmptyState, ErrorState, Meter, Skeleton } from "@/components/ui/misc";
import { Gallery } from "@/components/product/gallery";
import { formatPrice, titleCase } from "@/lib/utils";

type EvalStatus = { judgements: number; liked: number; sellers: number; categories: number; remaining: number; needed: number; ready: boolean };
// Blinded on purpose: no seller, ranking reasons or quality scores.
type BlindProduct = Pick<Product, "id" | "title" | "title_original" | "category" | "colours" | "images" | "price" | "currency" | "price_gbp">;
type NextResponse = { product: BlindProduct | null; status: EvalStatus };

const GRADES = [
  { taste: 0, label: "Not for me", key: "1" },
  { taste: 1, label: "Unsure", key: "2" },
  { taste: 2, label: "Like it", key: "3" },
  { taste: 3, label: "Love it", key: "4" },
];
const FLAGS = [
  { name: "owned_similar", label: "I own something similar" },
  { name: "too_expensive", label: "Too expensive" },
  { name: "quality_unclear", label: "Quality unclear" },
] as const;

export default function EvaluatePage() {
  const { data, error, isLoading, mutate } = useSWR<NextResponse>("/api/eval/next");
  const [flags, setFlags] = useState<Record<string, boolean>>({});
  const [last, setLast] = useState<{ id: number; productId: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const product = data?.product;
  const status = data?.status;

  const judge = useCallback(
    async (taste: number) => {
      if (!product || busy) return;
      setBusy(true);
      try {
        const r = await api.post<{ id: number }>("/api/eval/judgements", { product_id: product.id, taste, ...flags });
        setLast({ id: r.id, productId: product.id });
        setFlags({});
        await mutate();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Could not save the judgement");
      } finally {
        setBusy(false);
      }
    },
    [product, busy, flags, mutate],
  );

  const undo = async () => {
    if (last == null || busy) return;
    setBusy(true);
    try {
      // With the graded piece, a repeated Undo (or an id since reused by a newer grade) changes nothing.
      await api.del(`/api/eval/judgements/${last.id}?product_id=${last.productId}`);
      setLast(null);
      toast("Last judgement removed");
    } catch (e) {
      toast.error(e instanceof Error ? `Not undone: ${e.message}` : "Not undone", { action: { label: "Retry", onClick: () => void undo() } });
    } finally {
      setBusy(false);
    }
    mutate();
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.closest("input,textarea,select,[role=dialog]")) return;
      const g = GRADES.find((x) => x.key === e.key);
      if (!g) return;
      e.preventDefault();
      judge(g.taste);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [judge]);

  return (
    <div>
      <PageHeader
        title="Evaluate"
        description="Grade how much you like the look of each piece. Pieces are picked at random, not by the recommender, so a later measurement is fair. Grades never train the recommender. Price, owning something similar and unclear quality are recorded separately and never count as disliking the look."
      />
      {status && (
        <div className="max-w-md" data-testid="eval-status">
          <Meter value={(100 * Math.min(status.judgements, status.needed)) / status.needed} tone={status.ready ? "positive" : "default"} />
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            {status.judgements} judged · {status.liked} liked · {status.sellers} {status.sellers === 1 ? "seller" : "sellers"} · {status.remaining} left in the pool.{" "}
            {status.ready ? (
              <>
                Enough for a first measurement: run <code>python -m eval.real</code> in <code>backend/</code>.
              </>
            ) : (
              <>At least {status.needed} judgements across 3 sellers are needed before a measurement means anything.</>
            )}
          </p>
        </div>
      )}

      {error ? (
        <ErrorState error={error} retry={() => mutate()} />
      ) : isLoading ? (
        <Skeleton className="mt-8 aspect-[4/5] w-full max-w-md" />
      ) : !product ? (
        <EmptyState icon={<ClipboardCheck />} title={status?.judgements ? "Nothing left to judge" : "Nothing to judge yet"}>
          {status?.judgements ? "Every readable, undecided piece has been judged. Scan more sellers to add new pieces to the pool." : "Pieces appear here once scans have collected listings with a price and an analysed photo."}{" "}
          <Link href="/sellers" className="underline underline-offset-2">
            Sellers
          </Link>
        </EmptyState>
      ) : (
        <div className="mt-8 grid grid-cols-[minmax(0,1fr)] gap-6 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] md:gap-12" data-testid="eval-card">
          <Gallery product={product as Product} />
          <div>
            <p className="label">{product.category ? titleCase(product.category) : "Uncategorised"}</p>
            <h2 className="mt-1.5 text-xl font-semibold leading-7 tracking-tight md:text-2xl md:leading-8" data-testid="eval-title">
              {product.title}
            </h2>
            <p className="mt-2 text-sm text-muted-foreground">{formatPrice(product.price, product.currency)}</p>
            <p className="label mt-8 mb-3">How much do you like the look?</p>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {GRADES.map((g) => (
                <Button key={g.taste} variant={g.taste >= 2 ? "default" : "outline"} disabled={busy} data-testid={`grade-${g.taste}`} onClick={() => judge(g.taste)}>
                  {g.label} <span className="ml-1 text-xs opacity-60">{g.key}</span>
                </Button>
              ))}
            </div>
            <p className="label mt-6 mb-3">Separately (optional)</p>
            <div className="flex flex-wrap gap-2">
              {FLAGS.map((f) => (
                <Chip key={f.name} on={!!flags[f.name]} onClick={() => setFlags({ ...flags, [f.name]: !flags[f.name] })}>
                  {f.label}
                </Chip>
              ))}
            </div>
            {last != null && (
              <Button variant="ghost" size="sm" className="mt-6" onClick={undo}>
                <Undo2 /> Undo last judgement
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
