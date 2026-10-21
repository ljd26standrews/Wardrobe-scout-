"use client";
import Link from "next/link";
import { Heart, Sparkles } from "lucide-react";
import type { Product } from "@/lib/api";
import { Gallery } from "./gallery";
import { OriginBadge, Swatches } from "./meta";
import { QualityStatus } from "./quality";
import { formatGBP, formatPrice } from "@/lib/utils";

export function ProductCard({ product, footer, onOpen }: { product: Product; footer?: React.ReactNode; onOpen?: () => void }) {
  const rec = product.recommendation;
  return (
    <article className="group animate-fade-up" data-testid="product-card">
      <div className="relative">
        <Gallery product={product} rounded="rounded-xl" />
        <div className="pointer-events-none absolute left-2.5 top-2.5 flex flex-col items-start gap-1">
          {product.is_new && <span className="rounded-full bg-background/90 px-2 py-0.5 text-[10px] font-medium uppercase tracking-label backdrop-blur">New</span>}
          {rec?.explore && (
            <span className="inline-flex items-center gap-1 rounded-full bg-background/90 px-2 py-0.5 text-[10px] font-medium uppercase tracking-label backdrop-blur">
              <Sparkles className="size-3" /> Explore
            </span>
          )}
          {product.saved?.list === "loved" && <Heart className="size-4 fill-love text-love" />}
        </div>
      </div>
      <Link href={`/product/${product.id}`} onClick={onOpen} className="mt-3 block">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="label truncate">{[product.brand, product.seller?.name].filter(Boolean).join(" · ") || "Unknown seller"}</p>
            <h3 className="mt-1 line-clamp-2 font-sans text-[14px] font-normal leading-snug tracking-normal group-hover:underline">{product.title}</h3>
          </div>
          <Swatches colours={product.colours} className="mt-1 shrink-0" />
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
          <span className="font-medium tabular-nums">{formatPrice(product.price, product.currency)}</span>
          {product.price_gbp != null && product.currency !== "GBP" && <span className="text-muted-foreground">{formatGBP(product.price_gbp)}</span>}
          {rec && <span className="ml-auto text-xs tabular-nums text-muted-foreground">match {Math.round(rec.design_score * 100)}</span>}
        </div>
        <div className="mt-2 flex flex-wrap gap-1">
          <QualityStatus product={product} />
          <OriginBadge product={product} />
        </div>
      </Link>
      {footer && <div className="mt-3">{footer}</div>}
    </article>
  );
}
