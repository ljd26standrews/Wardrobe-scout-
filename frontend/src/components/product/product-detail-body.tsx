"use client";
import { AlertCircle, CheckCircle2, Sparkles } from "lucide-react";
import type { Product } from "@/lib/api";
import { AlternativesPreview } from "./alternatives";
import { LinkButtons, OriginBadge, PriceBlock, SpecList } from "./meta";
import { QualityPanel } from "./quality";

export function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border-t py-5">
      <h4 className="label mb-3">{title}</h4>
      {children}
    </section>
  );
}

/** Information column shared by Discover and the product page. */
export function ProductInfo({ product, showAlternatives = true }: { product: Product; showAlternatives?: boolean }) {
  const rec = product.recommendation;
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <p className="label">{[product.brand, product.category].filter(Boolean).join(" · ") || "Uncategorised"}</p>
        <OriginBadge product={product} />
      </div>
      <h1 className="mt-2 text-[28px] leading-[1.1] md:text-4xl" data-testid="product-title">
        {product.title}
      </h1>
      <p className="mt-2 text-sm text-muted-foreground">
        {product.seller ? product.seller.name : "Unknown seller"}
        {product.seller && product.seller.reputation !== "trusted" && <span> · seller {product.seller.reputation}</span>}
      </p>
      <div className="mt-4">
        <PriceBlock product={product} large />
      </div>

      {rec && (
        <Section title={rec.explore ? "Why you're seeing this" : "Why it matches you"}>
          <ul className="space-y-2 text-sm leading-relaxed">
            {rec.reasons.map((r) => (
              <li key={r} className="flex gap-2">
                <Sparkles className="mt-0.5 size-4 shrink-0 text-muted-foreground" strokeWidth={1.5} />
                {r}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title="Strengths & unknowns">
        <div className="grid gap-4 sm:grid-cols-2">
          <ul className="space-y-2 text-sm">
            {(product.pros.length ? product.pros : ["No demonstrated strengths yet"]).map((p) => (
              <li key={p} className="flex gap-2">
                <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-positive" strokeWidth={1.5} />
                <span>{p}</span>
              </li>
            ))}
          </ul>
          <ul className="space-y-2 text-sm">
            {product.cons.map((c) => (
              <li key={c} className="flex gap-2">
                <AlertCircle className="mt-0.5 size-4 shrink-0 text-caution" strokeWidth={1.5} />
                <span>{c}</span>
              </li>
            ))}
          </ul>
        </div>
      </Section>

      <Section title="Quality">
        <QualityPanel product={product} />
      </Section>

      <Section title="Details">
        <SpecList product={product} />
      </Section>

      <Section title="Buy / view">
        <LinkButtons product={product} />
      </Section>

      {showAlternatives && (
        <Section title="Comparable listings">
          <AlternativesPreview productId={product.id} />
        </Section>
      )}
    </div>
  );
}
