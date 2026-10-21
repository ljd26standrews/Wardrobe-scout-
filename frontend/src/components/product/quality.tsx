"use client";
import { Info, ShieldCheck, ShieldQuestion, ShieldAlert } from "lucide-react";
import type { Dimension, Product } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Meter, Tip } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

const DIM_LABEL: Record<string, string> = {
  design_compatibility: "Design compatibility",
  construction: "Demonstrated construction",
  material: "Material evidence",
  seller_reliability: "Seller reliability",
  value: "Value",
};

const DIM_HELP: Record<string, string> = {
  design_compatibility: "How well this matches your taste. Confidence grows as you give feedback.",
  construction: "Only independent QC photos, buyer reports, verified tests or your own notes count. Seller claims and AI photo checks never move this.",
  material: "Seller-stated composition is shown as a claim. It only becomes evidence when independently confirmed.",
  seller_reliability: "The seller's service and track record, from buyer reports and evidence you've accepted — never on popularity alone. Shown separately: it does not count towards this garment's quality.",
  value: "Price versus your catalogue's median for the category, adjusted for quality evidence and cheaper identical listings.",
};

export function statusTone(status: string) {
  return status === "Evidence-backed" ? "positive" : status === "Limited evidence" ? "caution" : "muted";
}

export function QualityStatus({ product, className }: { product: Product; className?: string }) {
  const s = product.quality.status;
  const Icon = s === "Evidence-backed" ? ShieldCheck : s === "Limited evidence" ? ShieldAlert : ShieldQuestion;
  return (
    <Badge variant={statusTone(s) as "positive" | "caution" | "muted"} className={className}>
      <Icon className="size-3.5" />
      {s}
    </Badge>
  );
}

function DimRow({ name, d }: { name: string; d: Dimension }) {
  const unverified = d.score == null;
  const tone = unverified ? "muted" : d.score! >= 65 ? "positive" : d.score! >= 45 ? "default" : "destructive";
  return (
    <div className="py-2.5">
      <div className="mb-1.5 flex items-baseline justify-between gap-3">
        <span className="flex items-center gap-1.5 text-[13px]">
          {DIM_LABEL[name]}
          <Tip content={DIM_HELP[name]}>
            <button aria-label={`About ${DIM_LABEL[name]}`} className="text-muted-foreground/70 hover:text-foreground">
              <Info className="size-3.5" />
            </button>
          </Tip>
        </span>
        <span className={cn("text-[13px] tabular-nums", unverified && "text-muted-foreground")}>
          {unverified ? d.label : `${d.score} · ${d.label}`}
        </span>
      </div>
      <Meter value={unverified ? 0 : d.score!} tone={tone} />
      <div className="mt-1 flex justify-between text-[11px] text-muted-foreground">
        <span>
          {d.evidence_counts && Object.keys(d.evidence_counts).length
            ? Object.entries(d.evidence_counts)
                .map(([k, v]) => `${v} ${k.replace("_", " ")}`)
                .join(" · ")
            : name === "value"
              ? (d.notes ?? [])[0] ?? ""
              : name === "design_compatibility"
                ? ""
                : "no evidence yet"}
        </span>
        <span>confidence {Math.round((d.confidence ?? 0) * 100)}%</span>
      </div>
    </div>
  );
}

export function QualityPanel({ product }: { product: Product }) {
  const dims = product.quality.dimensions;
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="label">Quality assessment</span>
        <QualityStatus product={product} />
      </div>
      <div className="divide-y">
        {(["design_compatibility", "construction", "material", "seller_reliability", "value"] as const).map((k) => (
          <DimRow key={k} name={k} d={dims[k]} />
        ))}
      </div>
      {product.quality.status === "Unverified" && (
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          Unverified means there isn&apos;t enough independent evidence to judge quality — not that it&apos;s poor. Add QC photos or buyer reports as evidence to change this.
        </p>
      )}
    </div>
  );
}
