"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import useSWR from "swr";
import { toast } from "sonner";
import { Check, GitCompareArrows, Search, X } from "lucide-react";
import { api, qs, type Comparison, type Product } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, EmptyState, ErrorState, Skeleton } from "@/components/ui/misc";
import { Input } from "@/components/ui/input";
import { relationTone } from "@/components/product/alternatives";
import { QualityStatus } from "@/components/product/quality";
import { cn, formatGBP, formatPrice } from "@/lib/utils";

function Picker() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const { data } = useSWR<{ items: Product[] }>(`/api/products${qs({ q, sort: "score", page_size: 12 })}`);
  return (
    <div className="mx-auto max-w-2xl">
      <div className="relative">
        <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input className="h-11 rounded-full pl-9" placeholder="Find a product to compare…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="mt-4 divide-y rounded-2xl border bg-card">
        {(data?.items ?? []).map((p) => (
          <button key={p.id} className="flex w-full items-center gap-3 p-3 text-left hover:bg-secondary/50" onClick={() => router.push(`/compare?id=${p.id}`)}>
            <div className="size-12 shrink-0 overflow-hidden rounded-lg bg-muted">
              {p.images[0]?.src && (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={p.images[0].src} alt="" className="h-full w-full object-cover" />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm">{p.title}</p>
              <p className="text-xs text-muted-foreground">
                {p.seller?.name} · {formatPrice(p.price, p.currency)}
              </p>
            </div>
          </button>
        ))}
        {data && !data.items.length && <p className="p-4 text-sm text-muted-foreground">No products found.</p>}
      </div>
    </div>
  );
}

function Cell({ children, className }: { children: React.ReactNode; className?: string }) {
  return <td className={cn("border-b px-3 py-3 align-top text-sm", className)}>{children}</td>;
}

function dimText(p: Product, k: keyof Product["quality"]["dimensions"]) {
  const d = p.quality.dimensions[k];
  return d.score == null ? <span className="text-muted-foreground">{d.label}</span> : `${d.score} · ${d.label}`;
}

function ComparisonView({ id }: { id: string }) {
  const { data, error, isLoading, mutate } = useSWR<Comparison>(`/api/products/${id}/compare`);
  if (error) return <ErrorState error={error} retry={() => mutate()} />;
  if (isLoading || !data) return <Skeleton className="h-96" />;
  const base = data.product;
  const cols: { p: Product; relation?: string; label?: string; confirmed?: boolean | null }[] = [
    { p: base },
    ...data.alternatives.map((a) => ({ p: a.product, relation: a.relation, label: a.relation_label, confirmed: a.user_confirmed })),
  ];
  const decide = async (other: number, same: boolean) => {
    await api.post(`/api/products/${base.id}/match`, { other_id: other, same });
    toast.success(same ? "Marked as the same product" : "Marked as different — won't be suggested again");
    mutate();
  };
  return (
    <div>
      <Card className="mb-6 p-5">
        <p className="label mb-2">Verdict</p>
        <ul className="space-y-1.5 text-sm leading-relaxed">
          {data.verdict.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      </Card>
      {data.alternatives.length === 0 ? (
        <EmptyState icon={<GitCompareArrows />} title="No alternatives yet">
          Nothing in your collected catalogues looks identical or similar. Scanning more sellers increases the chance of finding the same piece elsewhere.
        </EmptyState>
      ) : (
        <div className="-mx-4 overflow-x-auto px-4">
          <table className="min-w-[640px] border-separate border-spacing-0">
            <thead>
              <tr>
                <th className="w-32" />
                {cols.map((c) => (
                  <th key={c.p.id} className="w-[220px] min-w-[180px] max-w-[240px] px-3 pb-4 text-left align-top font-normal">
                    <Link href={`/product/${c.p.id}`} className="block">
                      <div className="aspect-[4/5] overflow-hidden rounded-xl bg-muted">
                        {c.p.images[0]?.src && (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={c.p.images[0].src} alt="" className="h-full w-full object-cover" />
                        )}
                      </div>
                      <p className="mt-2 line-clamp-2 text-sm hover:underline">{c.p.title}</p>
                    </Link>
                    {c.relation ? (
                      <Badge className="mt-2" variant={relationTone(c.relation) as "positive" | "caution" | "muted"}>
                        {c.label}
                      </Badge>
                    ) : (
                      <Badge className="mt-2" variant="outline">
                        This listing
                      </Badge>
                    )}
                    {c.relation && c.confirmed == null && (
                      <div className="mt-2 flex gap-1">
                        <Button size="sm" variant="outline" onClick={() => decide(c.p.id, true)}>
                          <Check /> Same
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => decide(c.p.id, false)}>
                          <X /> Different
                        </Button>
                      </div>
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr>
                <Cell className="text-muted-foreground">Seller</Cell>
                {cols.map((c) => (
                  <Cell key={c.p.id}>
                    {c.p.seller?.name ?? "—"}
                    <span className="block text-xs text-muted-foreground">reputation: {c.p.seller?.reputation ?? "unknown"}</span>
                  </Cell>
                ))}
              </tr>
              <tr>
                <Cell className="text-muted-foreground">Price</Cell>
                {cols.map((c) => (
                  <Cell key={c.p.id} className={cn(c.p.price != null && c.p.price === Math.min(...cols.map((x) => x.p.price ?? Infinity)) && "font-medium")}>
                    {formatPrice(c.p.price, c.p.currency)}
                    <span className="block text-xs text-muted-foreground">{c.p.landed_cost ? `landed ≈ ${formatGBP(c.p.landed_cost.total_gbp)}` : ""}</span>
                  </Cell>
                ))}
              </tr>
              <tr>
                <Cell className="text-muted-foreground">Material claim</Cell>
                {cols.map((c) => (
                  <Cell key={c.p.id}>{c.p.material_text ?? <span className="text-muted-foreground">not stated</span>}</Cell>
                ))}
              </tr>
              <tr>
                <Cell className="text-muted-foreground">Quality evidence</Cell>
                {cols.map((c) => (
                  <Cell key={c.p.id}>
                    <QualityStatus product={c.p} />
                  </Cell>
                ))}
              </tr>
              {(["construction", "material", "seller_reliability", "value"] as const).map((k) => (
                <tr key={k}>
                  <Cell className="capitalize text-muted-foreground">{k.replace("_", " ")}</Cell>
                  {cols.map((c) => (
                    <Cell key={c.p.id}>{dimText(c.p, k)}</Cell>
                  ))}
                </tr>
              ))}
              <tr>
                <Cell className="text-muted-foreground">Confidence</Cell>
                {cols.map((c) => (
                  <Cell key={c.p.id}>{Math.round(c.p.quality.overall_confidence * 100)}%</Cell>
                ))}
              </tr>
              <tr>
                <Cell className="text-muted-foreground">Sizes</Cell>
                {cols.map((c) => (
                  <Cell key={c.p.id}>{c.p.sizes.join(" · ") || "—"}</Cell>
                ))}
              </tr>
            </tbody>
          </table>
          <p className="mt-4 text-xs leading-relaxed text-muted-foreground">
            “Confirmed identical” requires your confirmation or a shared purchase link. “Same photographs” means the listings use near-identical photos — a strong hint of a shared source, but sellers frequently re-use factory images. “Visually similar” may be a different product.
          </p>
        </div>
      )}
    </div>
  );
}

function ComparePageInner() {
  const sp = useSearchParams();
  const id = sp.get("id");
  return (
    <div>
      <p className="label">Cross-seller research</p>
      <h1 className="page-title mt-2 mb-8">Compare</h1>
      {id ? <ComparisonView id={id} /> : <Picker />}
    </div>
  );
}

export default function ComparePage() {
  return (
    <Suspense fallback={<Skeleton className="h-96" />}>
      <ComparePageInner />
    </Suspense>
  );
}
