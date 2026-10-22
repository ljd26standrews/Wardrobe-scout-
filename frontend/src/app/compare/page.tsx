"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import useSWR from "swr";
import { toast } from "sonner";
import { Check, GitCompareArrows, Search, X } from "lucide-react";
import { api, qs, type Comparison, type Product } from "@/lib/api";
import { PageHeader } from "@/components/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/misc";
import { Input } from "@/components/ui/input";
import { relationTone } from "@/components/product/alternatives";
import { Photo } from "@/components/product/photo";
import { LandedCost, Price } from "@/components/product/price";
import { QualityStatus } from "@/components/product/quality";
import { cn, formatPrice, titleCase } from "@/lib/utils";

const DIMS = [
  { key: "construction", label: "Demonstrated construction" },
  { key: "material", label: "Material evidence" },
  { key: "seller_reliability", label: "Seller reliability" },
  { key: "value", label: "Value" },
] as const;

function dimText(p: Product, k: (typeof DIMS)[number]["key"]) {
  const d = p.quality.dimensions[k];
  return d.score == null ? <span className="text-muted-foreground">{d.label}</span> : `${d.score} · ${d.label}`;
}

/** Search for one piece, to compare its offers from other sellers. */
function Picker() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const { data } = useSWR<{ items: Product[] }>(`/api/products${qs({ q, sort: "score", page_size: 12 })}`);
  return (
    <div className="max-w-2xl">
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <Input className="pl-9" placeholder="Find a piece to compare offers for" aria-label="Find a piece" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <ul className="mt-3 divide-y rounded-md border">
        {(data?.items ?? []).map((p) => (
          <li key={p.id}>
            <button type="button" className="flex min-h-14 w-full items-center gap-3 p-2.5 text-left hover:bg-secondary" onClick={() => router.push(`/compare?id=${p.id}`)}>
              <Photo src={p.images[0]?.src} alt="" className="aspect-square w-12 shrink-0 rounded-sm" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm">{p.title}</span>
                <span className="meta block truncate">
                  {p.seller?.name ?? "Unknown seller"} · {p.price == null ? "Price not listed" : formatPrice(p.price, p.currency)}
                </span>
              </span>
            </button>
          </li>
        ))}
        {data && !data.items.length && <li className="p-4 text-sm text-muted-foreground">Nothing found.</li>}
      </ul>
      <p className="meta mt-4">
        To put different pieces side by side, select them in{" "}
        <Link href="/saved" className="underline underline-offset-4 hover:text-foreground">
          Saved
        </Link>
        .
      </p>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <tr>
      <th scope="row" className="sticky left-0 z-10 w-36 border-b bg-background py-3 pr-3 text-left align-top text-[13px] font-normal text-muted-foreground">
        {label}
      </th>
      {children}
    </tr>
  );
}

function Cell({ children, className }: { children: React.ReactNode; className?: string }) {
  return <td className={cn("border-b px-3 py-3 align-top text-sm", className)}>{children}</td>;
}

function Head({ p, children }: { p: Product; children?: React.ReactNode }) {
  return (
    <th scope="col" className="w-[220px] min-w-[180px] max-w-[260px] px-3 pb-4 text-left align-top font-normal">
      <Link href={`/product/${p.id}`} className="block rounded-md">
        <Photo src={p.images[0]?.src} alt="" className="aspect-[4/5]" />
        <span className="mt-2 line-clamp-2 text-sm font-medium hover:underline">{p.title}</span>
      </Link>
      <span className="meta mt-0.5 block truncate">{p.seller?.name ?? "Unknown seller"}</span>
      {children}
    </th>
  );
}

const cheapest = (ps: Product[]) => {
  const known = ps.map((p) => p.price_gbp).filter((x): x is number => x != null);
  return known.length > 1 ? Math.min(...known) : null;
};

/** Other listings of the same garment: who sells it, for how much, and what is known about each. */
function OffersView({ id }: { id: string }) {
  const { data, error, isLoading, mutate } = useSWR<Comparison>(`/api/products/${id}/compare`);
  if (error) return <ErrorState error={error} retry={() => mutate()} />;
  if (isLoading || !data) return <Skeleton className="h-96" />;
  const base = data.product;
  const cols: { p: Product; relation?: string; label?: string; confirmed?: boolean | null }[] = [
    { p: base },
    ...data.alternatives.map((a) => ({ p: a.product, relation: a.relation, label: a.relation_label, confirmed: a.user_confirmed })),
  ];
  const low = cheapest(cols.map((c) => c.p));
  const decide = async (other: number, same: boolean) => {
    try {
      await api.post(`/api/products/${base.id}/match`, { other_id: other, same });
      toast.success(same ? "Marked as the same piece" : "Marked as different; it won't be offered as a match again");
      mutate();
    } catch (e) {
      toast.error(`Not saved: ${e instanceof Error ? e.message : "the server did not answer"}`);
    }
  };
  return (
    <div>
      <section className="mb-8 max-w-2xl" aria-labelledby="verdict">
        <h2 id="verdict" className="section-title mb-2">
          In short
        </h2>
        <ul className="space-y-1.5 text-sm leading-relaxed">
          {data.verdict.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      </section>
      {data.alternatives.length === 0 ? (
        <EmptyState icon={<GitCompareArrows />} title="No other offers found">
          Nothing collected so far looks like the same piece. Scanning more sellers makes a match more likely.
        </EmptyState>
      ) : (
        <div className="-mx-4 overflow-x-auto px-4">
          <table className="min-w-[640px] border-separate border-spacing-0">
            <thead>
              <tr>
                <th className="sticky left-0 z-10 w-36 bg-background" />
                {cols.map((c) => (
                  <Head key={c.p.id} p={c.p}>
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
                      <span className="mt-2 flex gap-1">
                        <Button size="sm" variant="outline" onClick={() => decide(c.p.id, true)}>
                          <Check /> Same
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => decide(c.p.id, false)}>
                          <X /> Different
                        </Button>
                      </span>
                    )}
                  </Head>
                ))}
              </tr>
            </thead>
            <tbody>
              <Row label="Price">
                {cols.map((c) => (
                  <Cell key={c.p.id} className={cn(low != null && c.p.price_gbp === low && "font-medium")}>
                    <Price product={c.p} />
                    {low != null && c.p.price_gbp === low && <span className="meta block">Lowest here</span>}
                    <span className="block">
                      <LandedCost product={c.p} />
                    </span>
                  </Cell>
                ))}
              </Row>
              <Row label="Seller's material claim">
                {cols.map((c) => (
                  <Cell key={c.p.id}>{c.p.material_text ?? <span className="text-muted-foreground">Not stated</span>}</Cell>
                ))}
              </Row>
              <Row label="Quality evidence">
                {cols.map((c) => (
                  <Cell key={c.p.id}>
                    <QualityStatus product={c.p} />
                  </Cell>
                ))}
              </Row>
              {DIMS.map((d) => (
                <Row key={d.key} label={d.label}>
                  {cols.map((c) => (
                    <Cell key={c.p.id}>{dimText(c.p, d.key)}</Cell>
                  ))}
                </Row>
              ))}
              <Row label="Sizes listed">
                {cols.map((c) => (
                  <Cell key={c.p.id}>{c.p.sizes.join(" · ") || <span className="text-muted-foreground">Not listed</span>}</Cell>
                ))}
              </Row>
            </tbody>
          </table>
          <p className="meta mt-4 max-w-3xl leading-relaxed">
            “Confirmed identical” needs your confirmation or a shared purchase link. “Several identical photographs” means at least two photos match: a strong hint of the same source photos, but sellers often re-use factory images, so it is not proof of the same batch. “One similar photograph” and “visually similar” are only hints and never affect value.
          </p>
        </div>
      )}
    </div>
  );
}

/** Pieces you picked in Saved, side by side. */
function PiecesView({ ids }: { ids: number[] }) {
  const router = useRouter();
  const { data, error, isLoading, mutate } = useSWR<Product[]>(["compare-pieces", ...ids], () => Promise.all(ids.map((id) => api.get<Product>(`/api/products/${id}`))));
  if (error) return <ErrorState error={error} retry={() => mutate()} />;
  if (isLoading || !data) return <Skeleton className="h-96" />;
  const low = cheapest(data);
  const drop = (id: number) => {
    const rest = ids.filter((x) => x !== id);
    router.replace(rest.length ? `/compare?ids=${rest.join(",")}` : "/saved", { scroll: false });
  };
  return (
    <div className="-mx-4 overflow-x-auto px-4" data-testid="pieces-compare">
      <table className="min-w-[640px] border-separate border-spacing-0">
        <thead>
          <tr>
            <th className="sticky left-0 z-10 w-36 bg-background" />
            {data.map((p) => (
              <Head key={p.id} p={p}>
                <Button size="sm" variant="ghost" className="-ml-3 mt-1" onClick={() => drop(p.id)} aria-label={`Remove ${p.title} from the comparison`}>
                  <X /> Remove
                </Button>
              </Head>
            ))}
          </tr>
        </thead>
        <tbody>
          <Row label="Price">
            {data.map((p) => (
              <Cell key={p.id}>
                <Price product={p} />
                {low != null && p.price_gbp === low && <span className="meta block">Lowest here</span>}
                <span className="block">
                  <LandedCost product={p} />
                </span>
              </Cell>
            ))}
          </Row>
          <Row label="Category">
            {data.map((p) => (
              <Cell key={p.id}>{p.category ? titleCase(p.category) : <span className="text-muted-foreground">Unknown</span>}</Cell>
            ))}
          </Row>
          <Row label="Seller's material claim">
            {data.map((p) => (
              <Cell key={p.id}>{p.material_text ?? <span className="text-muted-foreground">Not stated</span>}</Cell>
            ))}
          </Row>
          <Row label="Quality evidence">
            {data.map((p) => (
              <Cell key={p.id}>
                <QualityStatus product={p} />
              </Cell>
            ))}
          </Row>
          {DIMS.map((d) => (
            <Row key={d.key} label={d.label}>
              {data.map((p) => (
                <Cell key={p.id}>{dimText(p, d.key)}</Cell>
              ))}
            </Row>
          ))}
          <Row label="Sizes listed">
            {data.map((p) => (
              <Cell key={p.id}>{p.sizes.join(" · ") || <span className="text-muted-foreground">Not listed</span>}</Cell>
            ))}
          </Row>
          <Row label="Your notes">
            {data.map((p) => (
              <Cell key={p.id}>
                {p.saved?.target_size || p.saved?.note ? (
                  <>
                    {p.saved?.target_size && <span className="block">Size to check: {p.saved.target_size}</span>}
                    {p.saved?.note && <span className="block whitespace-pre-line text-muted-foreground">{p.saved.note}</span>}
                  </>
                ) : (
                  <span className="text-muted-foreground">None</span>
                )}
              </Cell>
            ))}
          </Row>
        </tbody>
      </table>
    </div>
  );
}

function ComparePageInner() {
  const sp = useSearchParams();
  const id = sp.get("id");
  const ids = (sp.get("ids") ?? "")
    .split(",")
    .filter((x) => /^\d+$/.test(x))
    .map(Number)
    .slice(0, 4);
  return (
    <div>
      <PageHeader
        title="Compare"
        description={ids.length ? "The pieces you picked, side by side." : id ? "The same piece from other sellers: price, claims and evidence." : "Compare offers for one piece across sellers, or pieces you picked in Saved."}
        actions={
          ids.length > 0 && (
            <Button asChild variant="outline" size="sm">
              <Link href="/saved">Back to Saved</Link>
            </Button>
          )
        }
      />
      {ids.length ? <PiecesView ids={ids} /> : id ? <OffersView id={id} /> : <Picker />}
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
