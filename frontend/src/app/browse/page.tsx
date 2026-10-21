"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import useSWR from "swr";
import { Search, Store } from "lucide-react";
import { qs, type Product } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/misc";
import { Input, Select } from "@/components/ui/input";
import { FilterSheet, type Filters } from "@/components/filters";
import { ProductCard } from "@/components/product/product-card";

type ListResponse = { items: Product[]; total: number; page: number; page_size: number };

export default function BrowsePage() {
  const [filters, setFilters] = useState<Filters>({});
  const [sort, setSort] = useState("score");
  const [status, setStatus] = useState("active");
  const [origin, setOrigin] = useState("");
  const [newOnly, setNewOnly] = useState(false);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setFilters((f) => ({ ...f, q: search || undefined })), 300);
    return () => clearTimeout(t);
  }, [search]);
  useEffect(() => setPage(1), [filters, sort, status, origin, newOnly]);
  const { data, error, isLoading, mutate } = useSWR<ListResponse>(`/api/products${qs({ ...filters, sort, status, origin, new_only: newOnly || undefined, page, page_size: 36 })}`, { keepPreviousData: true });
  const pages = data ? Math.ceil(data.total / data.page_size) : 1;

  return (
    <div>
      <p className="label">Everything collected</p>
      <h1 className="page-title mt-2">Browse</h1>
      <div className="sticky top-14 z-20 -mx-4 mt-6 flex flex-wrap items-center gap-2 border-b bg-background/90 px-4 py-3 backdrop-blur md:top-16">
        <div className="relative min-w-0 flex-1 basis-56">
          <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input className="h-9 rounded-full pl-9" placeholder="Search titles, brands, descriptions" value={search} onChange={(e) => setSearch(e.target.value)} data-testid="browse-search" />
        </div>
        <FilterSheet filters={filters} onChange={setFilters} />
        <Select className="h-9 w-auto rounded-full text-xs" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort">
          <option value="score">Best match</option>
          <option value="newest">Newest</option>
          <option value="updated">Recently updated</option>
          <option value="price_asc">Price ↑</option>
          <option value="price_desc">Price ↓</option>
        </Select>
        <Select className="h-9 w-auto rounded-full text-xs" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
          <option value="active">Available</option>
          <option value="unavailable">Unavailable</option>
          <option value="all">All</option>
        </Select>
        <Select className="h-9 w-auto rounded-full text-xs" value={origin} onChange={(e) => setOrigin(e.target.value)} aria-label="Origin">
          <option value="">Any source</option>
          <option value="scraped">Scanned</option>
          <option value="manual">Manual</option>
          <option value="bookmarklet">Browser import</option>
          <option value="csv">CSV</option>
          <option value="json">JSON</option>
          <option value="seed">Seed records</option>
        </Select>
        <Button size="sm" variant={newOnly ? "default" : "outline"} onClick={() => setNewOnly(!newOnly)}>
          New arrivals
        </Button>
      </div>

      {error ? (
        <ErrorState error={error} retry={() => mutate()} />
      ) : isLoading && !data ? (
        <div className="mt-8 grid grid-cols-2 gap-4 md:grid-cols-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="aspect-[4/5]" />
          ))}
        </div>
      ) : !data?.items.length ? (
        <EmptyState icon={<Store />} title="Nothing here yet" action={<Button asChild><Link href="/sellers">Add sellers or import products</Link></Button>}>
          No collected listings match. Only genuinely collected or imported products are shown — nothing is invented.
        </EmptyState>
      ) : (
        <>
          <p className="mt-6 text-sm text-muted-foreground">{data.total} products</p>
          <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-10 md:grid-cols-3 xl:grid-cols-4">
            {data.items.map((p) => (
              <ProductCard key={p.id} product={p} />
            ))}
          </div>
          {pages > 1 && (
            <div className="mt-10 flex items-center justify-center gap-3">
              <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                Previous
              </Button>
              <span className="text-sm text-muted-foreground">
                Page {page} of {pages}
              </span>
              <Button variant="outline" size="sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>
                Next
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
