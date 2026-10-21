"use client";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import useSWR from "swr";
import { motion } from "framer-motion";
import { Bookmark, Clock3, GitCompareArrows, Heart, LayoutGrid, Rows3, ShoppingBag, SkipForward, Store, X } from "lucide-react";
import type { Product } from "@/lib/api";
import { qs } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, Skeleton, Tip } from "@/components/ui/misc";
import { Input } from "@/components/ui/input";
import { FilterSheet, type Filters } from "@/components/filters";
import { Gallery } from "@/components/product/gallery";
import { ProductCard } from "@/components/product/product-card";
import { ProductInfo } from "@/components/product/product-detail-body";
import { RejectDialog } from "@/components/product/reject-dialog";
import { useFeedback, type ActionName } from "@/components/product/use-feedback";
import { cn } from "@/lib/utils";

type FeedResponse = { items: Product[]; total_candidates: number; learning_strength: number; decisions: number };

function useStoredState<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(initial);
  useEffect(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw) setV(JSON.parse(raw));
    } catch {}
  }, [key]);
  const set = (nv: T) => {
    setV(nv);
    try {
      localStorage.setItem(key, JSON.stringify(nv));
    } catch {}
  };
  return [v, set];
}

const ENTER_FROM: Record<string, number> = { love: -40, like: -40, purchased: -40, dislike: 40, skip: 24 };

export default function DiscoverPage() {
  const [filters, setFilters] = useStoredState<Filters>("ws-filters", {});
  const [mode, setMode] = useStoredState<"card" | "grid">("ws-discover-mode", "card");
  const [hidden, setHidden] = useState<Set<number>>(new Set());
  const [rejecting, setRejecting] = useState<Product | null>(null);
  const [lastAction, setLastAction] = useState<string>("skip");
  const url = `/api/feed${qs({ limit: 24, ...filters })}`;
  const { data, error, isLoading, mutate } = useSWR<FeedResponse>(url);
  const queue = useMemo(() => (data?.items ?? []).filter((p) => !hidden.has(p.id)), [data, hidden]);
  const current = queue[0];

  useEffect(() => setHidden(new Set()), [url]);
  // Refill when the local queue runs low
  useEffect(() => {
    if (data && queue.length <= 2 && data.items.length > 2) {
      mutate().then(() => setHidden(new Set()));
    }
  }, [queue.length, data, mutate]);

  const feedback = useFeedback();
  const act = useCallback(
    async (p: Product, action: ActionName, reasons: string[] = [], note?: string) => {
      setLastAction(action);
      setHidden((h) => new Set(h).add(p.id));
      try {
        await feedback(p.id, action, reasons, note, { explore: !!p.recommendation?.explore, surface: mode });
      } catch {
        setHidden((h) => {
          const n = new Set(h);
          n.delete(p.id);
          return n;
        });
      }
    },
    [feedback, mode],
  );

  // Keyboard shortcuts (card mode)
  useEffect(() => {
    if (mode !== "card") return;
    const onKey = (e: KeyboardEvent) => {
      if (!current || rejecting || (e.target as HTMLElement)?.closest("input,textarea,select,[role=dialog]")) return;
      const k = e.key.toLowerCase();
      if (k === "l") act(current, "love");
      else if (k === "s") act(current, "like");
      else if (k === "m") act(current, "maybe");
      else if (k === "d" || k === "x") setRejecting(current);
      else if (k === "arrowright" || k === "n") act(current, "skip");
      else if (k === "c") window.location.href = `/compare?id=${current.id}`;
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, current, act, rejecting]);

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4 md:mb-8">
        <div>
          <p className="label">Today&apos;s selection</p>
          <h1 className="page-title mt-2">Discover</h1>
          {data && (
            <p className="mt-2 text-sm text-muted-foreground">
              {data.total_candidates} undecided pieces ·{" "}
              {data.decisions === 0 ? "cold start (ranking on your written preferences)" : `learning from ${data.decisions} decisions (${Math.round(data.learning_strength * 100)}% learned weight)`}
            </p>
          )}
        </div>
        <div className="flex w-full items-center gap-2 sm:w-auto">
          <Input placeholder="Search…" className="h-8 flex-1 rounded-full sm:w-48" value={filters.q ?? ""} onChange={(e) => setFilters({ ...filters, q: e.target.value || undefined })} />
          <FilterSheet filters={filters} onChange={setFilters} />
          <div className="flex rounded-full border p-0.5">
            <button aria-label="Card view" onClick={() => setMode("card")} className={cn("rounded-full p-1.5", mode === "card" && "bg-secondary")}>
              <Rows3 className="size-4" />
            </button>
            <button aria-label="Grid view" onClick={() => setMode("grid")} className={cn("rounded-full p-1.5", mode === "grid" && "bg-secondary")}>
              <LayoutGrid className="size-4" />
            </button>
          </div>
        </div>
      </div>

      {error ? (
        <ErrorState error={error} retry={() => mutate()} />
      ) : isLoading ? (
        <div className="grid grid-cols-[minmax(0,1fr)] gap-8 md:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
          <Skeleton className="aspect-[4/5] w-full rounded-2xl" />
          <div className="space-y-4">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-10 w-3/4" />
            <Skeleton className="h-6 w-40" />
            <Skeleton className="h-40 w-full" />
          </div>
        </div>
      ) : !current ? (
        <EmptyState
          icon={<Store />}
          title={data?.total_candidates === 0 && !Object.keys(filters).length ? "No collected products yet" : "You're all caught up"}
          action={
            <>
              <Button asChild>
                <Link href="/sellers">Scan sellers or import</Link>
              </Button>
              {Object.keys(filters).length > 0 && (
                <Button variant="outline" onClick={() => setFilters({})}>
                  Clear filters
                </Button>
              )}
            </>
          }
        >
          {data?.total_candidates === 0
            ? "Wardrobe Scout only shows real listings it has collected. Run a seller scan, paste a product link, use the browser bookmarklet, or import a CSV to get started. Seed examples from your notes live in Saved."
            : "Nothing left matching these filters. New arrivals appear after the next catalogue refresh."}
        </EmptyState>
      ) : mode === "grid" ? (
        <div className="grid grid-cols-2 gap-x-4 gap-y-10 md:grid-cols-3 xl:grid-cols-4">
          {queue.map((p) => (
            <ProductCard
              key={p.id}
              product={p}
              footer={
                <div className="flex gap-1">
                  <Button size="sm" variant="outline" aria-label="Love" onClick={() => act(p, "love")}>
                    <Heart />
                  </Button>
                  <Button size="sm" variant="outline" aria-label="Save" onClick={() => act(p, "like")}>
                    <Bookmark />
                  </Button>
                  <Button size="sm" variant="outline" aria-label="Maybe" onClick={() => act(p, "maybe")}>
                    <Clock3 />
                  </Button>
                  <Button size="sm" variant="outline" aria-label="Dislike" onClick={() => setRejecting(p)}>
                    <X />
                  </Button>
                </div>
              }
            />
          ))}
        </div>
      ) : (
        // Each decision brings the next piece in from the side matching the gesture (no exit phase, so
        // rapid decisions never leave overlapping cards behind).
        <motion.div
            key={current.id}
            initial={{ opacity: 0, x: ENTER_FROM[lastAction] ?? 0, y: ENTER_FROM[lastAction] ? 0 : 12 }}
            animate={{ opacity: 1, x: 0, y: 0 }}
            transition={{ type: "spring", stiffness: 300, damping: 32 }}
            className="grid grid-cols-[minmax(0,1fr)] gap-6 md:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)] md:gap-12"
            data-testid="discover-card"
          >
            <div className="md:sticky md:top-24 md:self-start">
              <Gallery product={current} />
              {current.recommendation?.explore && (
                <p className="mt-3 text-xs text-muted-foreground">Exploration pick — about 1 in 6 suggestions deliberately test something new.</p>
              )}
            </div>
            <div className="pb-24 md:pb-6">
              <ProductInfo product={current} />
            </div>
          </motion.div>
      )}

      {mode === "card" && current && (
        <div className="fixed inset-x-0 bottom-[calc(env(safe-area-inset-bottom)+4.2rem)] z-30 px-3 lg:bottom-6">
          <div className="mx-auto flex max-w-xl items-center justify-between gap-1 rounded-full border bg-background/90 p-1.5 shadow-xl backdrop-blur-md">
            <ActionBtn label="Dislike (D)" testid="action-dislike" onClick={() => setRejecting(current)} className="text-destructive">
              <X />
            </ActionBtn>
            <ActionBtn label="Skip (→)" testid="action-skip" onClick={() => act(current, "skip")}>
              <SkipForward />
            </ActionBtn>
            <ActionBtn label="Maybe (M)" testid="action-maybe" onClick={() => act(current, "maybe")}>
              <Clock3 />
            </ActionBtn>
            <ActionBtn label="Save (S)" testid="action-like" onClick={() => act(current, "like")}>
              <Bookmark />
            </ActionBtn>
            <ActionBtn label="Love (L)" testid="action-love" big onClick={() => act(current, "love")} className="bg-foreground text-background hover:bg-foreground/90">
              <Heart />
            </ActionBtn>
            <ActionBtn label="Compare (C)" testid="action-compare" onClick={() => (window.location.href = `/compare?id=${current.id}`)}>
              <GitCompareArrows />
            </ActionBtn>
            <ActionBtn label="Purchased" testid="action-purchased" className="max-sm:hidden" onClick={() => act(current, "purchased")}>
              <ShoppingBag />
            </ActionBtn>
          </div>
        </div>
      )}

      <RejectDialog
        open={!!rejecting}
        title={rejecting?.title}
        onOpenChange={(o) => !o && setRejecting(null)}
        onSubmit={(reasons, note) => {
          const p = rejecting!;
          setRejecting(null);
          act(p, "dislike", reasons, note);
        }}
      />
    </div>
  );
}

function ActionBtn({ label, onClick, children, className, big, testid }: { label: string; onClick: () => void; children: React.ReactNode; className?: string; big?: boolean; testid?: string }) {
  return (
    <Tip content={label}>
      <button
        aria-label={label}
        data-testid={testid}
        onClick={onClick}
        className={cn(
          "flex items-center justify-center rounded-full transition-all hover:bg-secondary active:scale-90 [&_svg]:stroke-[1.6]",
          big ? "size-12 sm:size-14 [&_svg]:size-6" : "size-10 sm:size-11 [&_svg]:size-5",
          className,
        )}
      >
        {children}
      </button>
    </Tip>
  );
}
