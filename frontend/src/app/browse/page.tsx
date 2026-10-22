"use client";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { Search, Store } from "lucide-react";
import { qs, SAVED_LIST_LABEL, type Product } from "@/lib/api";
import { PageHeader } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/misc";
import { Input, Select } from "@/components/ui/input";
import { FilterSheet, type Filters } from "@/components/filters";
import { ProductTile, type TileAction } from "@/components/product/tile";
import { PiecePanel, usePiece } from "@/components/product/side-panel";
import { useFeedback } from "@/components/product/use-feedback";

type ListResponse = { items: Product[]; total: number; page: number; page_size: number };

const PAGE_SIZE = 36;

/** Filters, sort and page live in the address: a reload, a shared link or Back returns the same results. */
function readFilters(sp: URLSearchParams): Filters {
  const num = (k: string) => {
    const v = sp.get(k);
    return v != null && v !== "" && Number.isFinite(Number(v)) ? Number(v) : undefined;
  };
  const currency = sp.get("price_currency");
  const quality = sp.get("quality");
  return {
    q: sp.get("q") || undefined,
    category: sp.getAll("category").filter(Boolean),
    brand: sp.getAll("brand").filter(Boolean),
    seller_id: sp.getAll("seller_id").map(Number).filter(Number.isFinite),
    material: sp.getAll("material").filter(Boolean),
    min_price: num("min_price"),
    max_price: num("max_price"),
    price_currency: currency === "GBP" || currency === "CNY" ? currency : undefined,
    season: sp.get("season") || undefined,
    size: sp.get("size") || undefined,
    quality: quality === "evidence" || quality === "unverified" ? quality : undefined,
  };
}

export default function BrowsePage() {
  return (
    <Suspense fallback={<Skeleton className="h-96" />}>
      <Browse />
    </Suspense>
  );
}

function Browse() {
  const sp = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const filters = useMemo(() => readFilters(sp), [sp]);
  const sort = sp.get("sort") ?? "score";
  const status = sp.get("status") ?? "active";
  const origin = sp.get("origin") ?? "";
  const newOnly = sp.get("new_only") === "true";
  const page = Math.max(1, Math.floor(Number(sp.get("page"))) || 1);
  const { open } = usePiece();

  const state = { ...filters, sort, status, origin, new_only: newOnly || undefined, page };
  const update = (patch: Partial<typeof state>) => {
    const next: Record<string, string | number | boolean | string[] | number[] | null | undefined> = { ...state, page: undefined, ...patch };
    if (next.sort === "score") delete next.sort;
    if (next.status === "active") delete next.status;
    if (next.page === 1) delete next.page;
    router.replace(`${pathname}${qs(next)}`, { scroll: false });
  };

  // The search box writes to the address after a pause; Back (or a link) writes the box.
  const [search, setSearch] = useState(filters.q ?? "");
  const wrote = useRef(filters.q ?? "");
  useEffect(() => {
    if ((filters.q ?? "") !== wrote.current) {
      wrote.current = filters.q ?? "";
      setSearch(wrote.current);
    }
  }, [filters.q]);
  useEffect(() => {
    if (search === wrote.current) return;
    const t = setTimeout(() => {
      wrote.current = search;
      update({ q: search || undefined });
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  const key = `/api/products${qs({ ...filters, sort, status, origin, new_only: newOnly || undefined, page, page_size: PAGE_SIZE })}`;
  const { data, error, isLoading, mutate } = useSWR<ListResponse>(key, { keepPreviousData: true });
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  const feedback = useFeedback({ onDone: useCallback(() => void mutate(), [mutate]) });
  const act = (p: Product, a: TileAction) => feedback(p.id, a, [], undefined, { surface: "browse" }, p.title).catch(() => {});
  const goTo = (n: number) => {
    update({ page: n });
    window.scrollTo({ top: 0 });
  };

  return (
    <div>
      <PageHeader title="Browse all" description="Everything collected from your sellers and imports. Nothing here is invented." />
      <div className="sticky top-14 z-20 -mx-4 flex flex-wrap items-center gap-2 border-b bg-background px-4 py-3 md:-mx-6 md:px-6 lg:top-0 lg:-mx-10 lg:px-10">
        <div className="relative min-w-0 flex-1 basis-56">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
          <Input className="pl-9" placeholder="Search titles, brands, descriptions" aria-label="Search" value={search} onChange={(e) => setSearch(e.target.value)} data-testid="browse-search" />
        </div>
        <FilterSheet filters={filters} onChange={(f) => update({ ...f, q: filters.q })} />
        <Select className="w-auto" value={sort} onChange={(e) => update({ sort: e.target.value })} aria-label="Sort">
          <option value="score">Best match</option>
          <option value="newest">Newest</option>
          <option value="updated">Recently updated</option>
          <option value="price_asc">Price, low to high</option>
          <option value="price_desc">Price, high to low</option>
        </Select>
        <Select className="w-auto" value={status} onChange={(e) => update({ status: e.target.value })} aria-label="Availability">
          <option value="active">Available</option>
          <option value="unavailable">Unavailable</option>
          <option value="notice">Shop notices</option>
          <option value="all">All</option>
        </Select>
        <Select className="w-auto" value={origin} onChange={(e) => update({ origin: e.target.value })} aria-label="Source">
          <option value="">Any source</option>
          <option value="scraped">Scanned</option>
          <option value="manual">Added by hand</option>
          <option value="bookmarklet">Browser import</option>
          <option value="csv">CSV</option>
          <option value="json">JSON</option>
          <option value="seed">Seed records</option>
        </Select>
        <Button variant={newOnly ? "default" : "outline"} aria-pressed={newOnly} onClick={() => update({ new_only: newOnly ? undefined : true })}>
          New arrivals
        </Button>
      </div>

      {error ? (
        <ErrorState error={error} retry={() => mutate()} />
      ) : isLoading && !data ? (
        <div className="mt-6 grid grid-cols-2 gap-x-4 gap-y-8 md:grid-cols-3 xl:grid-cols-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="aspect-[4/5]" />
          ))}
        </div>
      ) : !data?.items.length ? (
        <EmptyState
          icon={<Store />}
          title={page > 1 ? "No more pieces on this page" : "Nothing matches"}
          action={
            page > 1 ? (
              <Button variant="outline" onClick={() => goTo(1)}>
                Back to page 1
              </Button>
            ) : (
              <Button asChild variant="outline">
                <Link href="/sellers">Add sellers or import pieces</Link>
              </Button>
            )
          }
        >
          Only pieces collected from sellers or imported by you are shown. Try fewer filters, or add a seller.
        </EmptyState>
      ) : (
        <>
          <p className="meta mt-5" aria-live="polite">
            {data.total} piece{data.total === 1 ? "" : "s"}
            {pages > 1 && ` · page ${page} of ${pages}`}
          </p>
          <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-8 md:grid-cols-3 xl:grid-cols-4" data-testid="browse-grid">
            {data.items.map((p) => (
              <ProductTile
                key={p.id}
                product={p}
                onOpen={open}
                onAction={act}
                status={p.saved ? <span>Saved · {SAVED_LIST_LABEL[p.saved.list] ?? p.saved.list}</span> : undefined}
              />
            ))}
          </div>
          {pages > 1 && (
            <nav className="mt-10 flex items-center justify-center gap-3" aria-label="Pages">
              <Button variant="outline" disabled={page <= 1} onClick={() => goTo(page - 1)}>
                Previous
              </Button>
              <span className="text-sm text-muted-foreground">
                Page {page} of {pages}
              </span>
              <Button variant="outline" disabled={page >= pages} onClick={() => goTo(page + 1)}>
                Next
              </Button>
            </nav>
          )}
        </>
      )}
      <PiecePanel onChanged={() => mutate()} />
    </div>
  );
}
