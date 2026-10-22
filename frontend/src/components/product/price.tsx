"use client";
import type { Product } from "@/lib/api";
import { Tip } from "@/components/ui/misc";
import { cn, formatGBP, formatPrice } from "@/lib/utils";

/** The price as listed, then its conversion (with the rate, on hover or focus). Landed cost is separate. */
export function Price({ product, className, size = "sm" }: { product: Product; className?: string; size?: "sm" | "lg" }) {
  if (product.price == null) return <span className={cn("text-muted-foreground", className)}>Price not listed</span>;
  return (
    <span className={cn("inline-flex flex-wrap items-baseline gap-x-1.5", className)}>
      <span className={cn("font-medium tabular-nums text-foreground", size === "lg" && "text-xl font-semibold")}>{formatPrice(product.price, product.currency)}</span>
      {product.conversion && product.price_gbp != null && (
        <Tip content={`Converted at ${product.conversion.from} 1 = £${product.conversion.rate} (${product.conversion.source || "your rate in Settings"}). A conversion, not the landed cost.`}>
          <span tabIndex={0} className="cursor-help tabular-nums text-muted-foreground">
            ≈ {formatGBP(product.price_gbp)}
          </span>
        </Tip>
      )}
    </span>
  );
}

/** The UK landed-cost estimate for buying in yuan through an agent, with its parts. Nothing for other prices. */
export function LandedCost({ product }: { product: Product }) {
  const l = product.landed_cost;
  if (!l) return null;
  return (
    <Tip
      content={
        <div className="space-y-0.5">
          <div className="font-medium">Estimated UK landed cost</div>
          <div>Goods {formatGBP(l.goods_gbp)} · domestic shipping {formatGBP(l.domestic_shipping_gbp)} · agent fee {formatGBP(l.agent_fee_gbp)}</div>
          <div>
            International (~{l.estimated_weight_kg} kg) {formatGBP(l.international_shipping_gbp)} · duty {formatGBP(l.duty_gbp)} · VAT {formatGBP(l.vat_gbp)}
          </div>
          <div className="pt-1 opacity-80">{l.assumptions}</div>
        </div>
      }
    >
      <span tabIndex={0} className="cursor-help text-sm text-muted-foreground underline decoration-dotted underline-offset-4">
        Landed in the UK ≈ {formatGBP(l.total_gbp)}
      </span>
    </Tip>
  );
}
