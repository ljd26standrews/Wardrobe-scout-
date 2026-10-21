"use client";
import Link from "next/link";
import { AlertCircle, CheckCircle2, Sparkles } from "lucide-react";
import type { Product } from "@/lib/api";
import { AlternativesPreview } from "./alternatives";
import { ConfidencePanel } from "./confidence";
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
/** ``inFeed``: Discover's status line already explains a missing calibration, so only an actual estimate is shown. */
export function ProductInfo({ product, showAlternatives = true, inFeed = false }: { product: Product; showAlternatives?: boolean; inFeed?: boolean }) {
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
      {!!product.family?.offers.length && (
        <p className="mt-1 text-sm text-muted-foreground" data-testid="family-offers">
          Also listed by {product.family.offers.length} other {product.family.offers.length === 1 ? "seller" : "sellers"} ·{" "}
          <Link href={`/compare?id=${product.id}`} className="underline underline-offset-2">
            compare offers
          </Link>
        </p>
      )}
      <div className="mt-4">
        <PriceBlock product={product} large />
      </div>

      {product.confidence && product.confidence.state !== "off" && (!inFeed || product.confidence.estimate != null) && (
        <Section title="Approval estimate">
          <ConfidencePanel c={product.confidence} />
        </Section>
      )}

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
        {product.analysis_state && product.analysis_state !== "analysed" && product.analysis_state !== "deep" && (
          <p className="mt-3 text-xs text-muted-foreground" data-testid="analysis-state">
            {product.analysis_state === "discovered"
              ? "Found in the catalogue listing; the product page and photos are still being analysed."
              : "Partly analysed; the rest is queued."}
          </p>
        )}
      </Section>

      {!!product.sizing_images?.length && (
        <Section title="Size charts">
          <p className="mb-3 text-xs text-muted-foreground">Found in the seller&apos;s gallery. Kept for sizing; never used to judge style.</p>
          <div className="flex gap-2 overflow-x-auto" data-testid="sizing-images">
            {product.sizing_images.map((img) =>
              img.src ? (
                <a key={img.id} href={img.src} target="_blank" rel="noreferrer" className="shrink-0">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={img.src} alt="Size chart" className="h-40 w-auto rounded-lg border bg-white object-contain" loading="lazy" />
                </a>
              ) : null,
            )}
          </div>
        </Section>
      )}

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
