"use client";
import Link from "next/link";
import useSWR from "swr";
import { ArrowRight } from "lucide-react";
import type { Comparison } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/misc";
import { formatPrice } from "@/lib/utils";

export function relationTone(rel: string) {
  return rel === "confirmed_identical" || rel === "same_purchase_link" ? "positive" : rel === "shared_photos" ? "caution" : "muted";
}

export function AlternativesPreview({ productId }: { productId: number }) {
  const { data, error, isLoading } = useSWR<Comparison>(`/api/products/${productId}/compare`);
  if (isLoading) return <Skeleton className="h-20" />;
  if (error) return <p className="text-xs text-muted-foreground">Couldn&apos;t load comparisons.</p>;
  const alts = data?.alternatives ?? [];
  if (!alts.length) return <p className="text-sm text-muted-foreground">No comparable listings found in your collected catalogues yet.</p>;
  return (
    <div className="space-y-2">
      {alts.slice(0, 3).map((a) => (
        <Link key={a.product.id} href={`/product/${a.product.id}`} className="flex items-center gap-3 rounded-xl border p-2 hover:bg-secondary/60">
          <div className="size-14 shrink-0 overflow-hidden rounded-lg bg-muted">
            {a.product.images[0]?.src && (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={a.product.images[0].src} alt="" className="h-full w-full object-cover" />
            )}
          </div>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm">{a.product.title}</p>
            <p className="text-xs text-muted-foreground">
              {a.product.seller?.name} · {formatPrice(a.product.price, a.product.currency)}
            </p>
          </div>
          <Badge variant={relationTone(a.relation) as "positive" | "caution" | "muted"} className="hidden sm:inline-flex">
            {a.relation.replace(/_/g, " ")}
          </Badge>
        </Link>
      ))}
      {data && data.verdict.notes[0] && <p className="text-xs leading-relaxed text-muted-foreground">{data.verdict.notes[0]}</p>}
      <Link href={`/compare?id=${productId}`} className="inline-flex items-center gap-1 text-xs underline-offset-4 hover:underline">
        Full comparison <ArrowRight className="size-3" />
      </Link>
    </div>
  );
}
