"use client";
import { ExternalLink } from "lucide-react";
import type { Product } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/misc";
import { COLOUR_SWATCH, cn, titleCase } from "@/lib/utils";

export function OriginBadge({ product }: { product: Product }) {
  if (product.data_origin === "seed")
    return (
      <Tip content="Created from your notes (historical price/link). Not yet verified against the live listing; no photos were invented.">
        <Badge variant="caution">Seed · unverified</Badge>
      </Tip>
    );
  if (product.data_origin !== "scraped")
    return (
      <Tip content={`Imported via ${product.data_origin}. Details are as entered or captured, not from an automated scan.`}>
        <Badge variant="muted">{product.data_origin === "bookmarklet" ? "Browser import" : `${titleCase(product.data_origin)} import`}</Badge>
      </Tip>
    );
  if (product.completeness === "partial") return <Badge variant="muted">Partial listing</Badge>;
  return null;
}

export function Swatches({ colours, className }: { colours: string[]; className?: string }) {
  if (!colours.length) return null;
  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      {colours.slice(0, 5).map((c) => (
        <Tip key={c} content={titleCase(c)}>
          <span className="inline-block size-3.5 rounded-full border border-black/10 shadow-inner" style={{ background: COLOUR_SWATCH[c] ?? "#bbb" }} />
        </Tip>
      ))}
    </span>
  );
}

const LINK_LABEL: Record<string, string> = { yupoo: "Yupoo", weidian: "Weidian", taobao: "Taobao", seller: "Seller site" };

export function LinkButtons({ product }: { product: Product }) {
  const entries = Object.entries(product.links).filter(([, u]) => u && u.startsWith("http"));
  if (!entries.length && !product.source_url) return <span className="text-xs text-muted-foreground">No links available</span>;
  const all = entries.length ? entries : [["seller", product.source_url!] as [string, string]];
  return (
    <div className="flex flex-wrap gap-2">
      {all.map(([k, u]) => (
        <a key={k} href={u} target="_blank" rel="noreferrer noopener" className="inline-flex min-h-9 items-center gap-1.5 rounded-md border px-3 text-[13px] hover:bg-secondary [@media(pointer:coarse)]:min-h-11">
          {LINK_LABEL[k] ?? titleCase(k)}
          <ExternalLink className="size-3.5" aria-hidden />
          <span className="sr-only">(opens in a new tab)</span>
        </a>
      ))}
    </div>
  );
}

export function SpecList({ product }: { product: Product }) {
  const rows: [string, React.ReactNode][] = [
    ["Category", product.category ? titleCase(product.category) : <span className="text-muted-foreground">Unknown</span>],
    [
      "Material",
      product.material_text ? (
        <span>
          {product.material_text} <span className="text-muted-foreground">(seller claim)</span>
        </span>
      ) : (
        <span className="text-muted-foreground">Not stated</span>
      ),
    ],
    ["Weight", product.gsm ? `${product.gsm} g/m² (seller claim)` : <span className="text-muted-foreground">Not stated</span>],
    ["Colour", product.colours.length ? <span className="inline-flex items-center gap-2"><Swatches colours={product.colours} />{product.colours.map(titleCase).join(", ")}</span> : <span className="text-muted-foreground">Not stated</span>],
    ["Sizes", product.sizes.length ? product.sizes.join(" · ") : <span className="text-muted-foreground">Not listed</span>],
    ["Fit", product.fit_tags.length ? product.fit_tags.map(titleCase).join(", ") : <span className="text-muted-foreground">Not stated</span>],
    ["Branding", product.logo_level === "unknown" ? <span className="text-muted-foreground">Unknown</span> : titleCase(product.logo_level === "none" ? "no visible logo" : product.logo_level)],
    ["Season", product.seasons.length ? product.seasons.map(titleCase).join(", ") : <span className="text-muted-foreground">—</span>],
  ];
  if (product.seller_item_code) rows.push(["Seller item ID", product.seller_item_code]);
  return (
    <dl className="grid grid-cols-[7.5rem_1fr] gap-x-4 gap-y-2.5 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}
