"use client";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { motion } from "framer-motion";
import { toast } from "sonner";
import { Bookmark, Clock3, FlaskConical, GitCompareArrows, Heart, LayoutGrid, Rows3, ShieldCheck, ShoppingBag, SkipForward, Store, X } from "lucide-react";
import type { FeedResponse, Gate, Product, SelectiveMode } from "@/lib/api";
import { api, MODE_LABEL, MODES, qs } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, Skeleton, Tip } from "@/components/ui/misc";
import { Input } from "@/components/ui/input";
import { FilterSheet, type Filters } from "@/components/filters";
import { GateStatus } from "@/components/product/confidence";
import { Gallery } from "@/components/product/gallery";
import { ProductCard } from "@/components/product/product-card";
import { ProductInfo } from "@/components/product/product-detail-body";
import { RejectDialog } from "@/components/product/reject-dialog";
import { useFeedback, type ActionName } from "@/components/product/use-feedback";
import { cn } from "@/lib/utils";

type Seen = { product: Product; feedId: string | null; position: number };

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
  // The card on screen stays put while the ranking behind it is refreshed after each decision.
  const [pinned, setPinned] = useState<number | null>(null);
  const seen = useRef(new Map<number, Seen>());
  const logged = useRef(new Set<string>());
  const [rejecting, setRejecting] = useState<Product | null>(null);
  const [lastAction, setLastAction] = useState<string>("skip");
  // Selective recommendation: the mode is a saved setting, so the server and every page agree on it.
  const { data: settings, mutate: mutateSettings } = useSWR<Record<string, any>>("/api/settings"); // eslint-disable-line @typescript-eslint/no-explicit-any
  const selective = settings?.selective as { enabled: boolean; mode: SelectiveMode } | undefined;
  const [tab, setTab] = useStoredState<"foryou" | "learning">("ws-discover-tab", "foryou");
  const learning = !!selective?.enabled && tab === "learning";
  // The mode is part of the request (and so of the cache key): one mode's pieces can never be served from another's cache.
  const url = settings ? `/api/feed${learning ? "/learning" : ""}${qs({ limit: 24, mode: selective?.enabled ? selective.mode : undefined, ...filters })}` : null;
  const { data, error, isLoading, mutate } = useSWR<FeedResponse>(url);
  const queue = useMemo(() => {
    data?.items.forEach((p, i) => seen.current.set(p.id, { product: p, feedId: data.feed_id, position: i }));
    const items = (data?.items ?? []).filter((p) => !hidden.has(p.id));
    // The card on screen stays put across re-ranking only while the latest response still includes it: a piece
    // the gate no longer passes is never kept on screen from an older response.
    const pin = pinned != null ? items.find((p) => p.id === pinned) : undefined;
    return pin ? [pin, ...items.filter((p) => p.id !== pin.id)] : items;
  }, [data, hidden, pinned]);
  const current = queue[0];

  useEffect(() => {
    setHidden(new Set());
    setPinned(null);
  }, [url]);
  const chooseMode = useCallback(
    async (m: SelectiveMode) => {
      if (!settings || !selective || m === selective.mode) return;
      const next = { ...settings.selective, mode: m };
      mutateSettings({ ...settings, selective: next }, false);
      try {
        await api.put("/api/settings/selective", next);
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Could not change the mode");
      }
      mutateSettings();
    },
    [settings, selective, mutateSettings],
  );
  // An undone piece is waiting to come back from the server: keep its pin until the fresh response arrives.
  const restoring = useRef<number | null>(null);
  useEffect(() => {
    if (restoring.current == null && current && current.id !== pinned) setPinned(current.id);
  }, [current, pinned]);

  // Impression log: what was actually displayed, from which feed and position (for honest evaluation later).
  const logViews = useCallback((feedId: string | null, surface: string, items: { product_id: number; position: number }[]) => {
    const fresh = items.filter((it) => !logged.current.has(`${feedId}:${surface}:${it.product_id}`));
    if (!feedId || !fresh.length) return;
    fresh.forEach((it) => logged.current.add(`${feedId}:${surface}:${it.product_id}`));
    api.post(`/api/feed/${feedId}/viewed`, { surface, items: fresh }).catch(() => {});
  }, []);
  useEffect(() => {
    if (mode !== "card" || !current) return;
    const s = seen.current.get(current.id);
    if (s) logViews(s.feedId, "card", [{ product_id: current.id, position: s.position }]);
  }, [mode, current, logViews]);
  useEffect(() => {
    if (mode !== "grid" || !data?.feed_id) return;
    const shown = data.items.map((p, position) => ({ product_id: p.id, position })).filter((it) => !hidden.has(it.product_id));
    logViews(data.feed_id, "grid", shown);
  }, [mode, data, hidden, logViews]);

  const restore = useCallback(
    async (productId: number) => {
      setHidden((h) => {
        const n = new Set(h);
        n.delete(productId);
        return n;
      });
      setPinned(productId);
      restoring.current = productId;
      let fresh: FeedResponse | undefined;
      try {
        fresh = await mutate();
      } finally {
        restoring.current = null;
      }
      if (fresh?.items.some((p) => p.id === productId)) setPinned(productId);
      else if (fresh) {
        toast("It's undecided again, but not in this feed", {
          description: learning ? "It isn't among today's learning picks." : `It doesn't currently pass ${MODE_LABEL[fresh.gate.mode]}.`,
        });
      }
    },
    [mutate, learning],
  );
  const feedback = useFeedback(undefined, restore);
  const act = useCallback(
    async (p: Product, action: ActionName, reasons: string[] = [], note?: string) => {
      setLastAction(action);
      setHidden((h) => new Set(h).add(p.id));
      const s = seen.current.get(p.id);
      try {
        await feedback(p.id, action, reasons, note, {
          explore: !!p.recommendation?.explore,
          surface: mode,
          feed_id: s?.feedId ?? null,
          position: s?.position ?? null,
        });
        mutate(); // re-rank the rest of the queue with this decision included
      } catch {
        setHidden((h) => {
          const n = new Set(h);
          n.delete(p.id);
          return n;
        });
      }
    },
    [feedback, mode, mutate],
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
          {data?.gate?.trial === "ranked" && (
            <p className="mt-2 max-w-2xl text-sm text-muted-foreground" data-testid="gate-status">
              {data.gate.message}
            </p>
          )}
          {data && data.gate?.state !== "off" && data.gate?.trial !== "ranked" && <GateStatus gate={data.gate} total={data.total_candidates} />}
          {data && data.gate?.state === "off" && (
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

      {selective?.enabled && (
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3 border-b">
          <div className="-mb-px flex gap-1" role="tablist" aria-label="Feed">
            {(
              [
                ["foryou", "For you", ShieldCheck],
                ["learning", "Learning picks", FlaskConical],
              ] as const
            ).map(([key, label, Icon]) => (
              <button
                key={key}
                role="tab"
                aria-selected={tab === key}
                data-testid={`tab-${key}`}
                onClick={() => setTab(key)}
                className={cn(
                  "inline-flex items-center gap-1.5 whitespace-nowrap border-b-2 border-transparent px-3 pb-2.5 pt-1 text-sm text-muted-foreground transition-colors hover:text-foreground",
                  tab === key && "border-foreground text-foreground",
                )}
              >
                <Icon className="size-4" strokeWidth={1.6} /> {label}
              </button>
            ))}
          </div>
          <div className="mb-2 flex rounded-full border p-0.5 text-xs" role="radiogroup" aria-label="How selective">
            {MODES.map((m) => (
              <button
                key={m}
                role="radio"
                aria-checked={selective.mode === m}
                data-testid={`mode-${m}`}
                onClick={() => chooseMode(m)}
                className={cn("rounded-full px-3 py-1.5 transition-colors", selective.mode === m ? "bg-foreground text-background" : "text-muted-foreground hover:text-foreground")}
              >
                {MODE_LABEL[m]}
              </button>
            ))}
          </div>
        </div>
      )}
      {learning && !!data?.items.length && (
        <p className="mb-6 max-w-2xl text-sm leading-relaxed text-muted-foreground" data-testid="learning-note">
          Pieces the model is unsure about. They&apos;re not recommendations and never appear in For you; how you rate them teaches the model the most.
        </p>
      )}

      {error ? (
        <ErrorState error={error} retry={() => mutate()} />
      ) : isLoading || !data ? (
        <div className="grid grid-cols-[minmax(0,1fr)] gap-8 md:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
          <Skeleton className="aspect-[4/5] w-full rounded-2xl" />
          <div className="space-y-4">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-10 w-3/4" />
            <Skeleton className="h-6 w-40" />
            <Skeleton className="h-40 w-full" />
          </div>
        </div>
      ) : !current && data.gate && data.gate.state !== "off" && data.total_candidates > 0 ? (
        <GateEmpty gate={data.gate} learning={learning} onMode={chooseMode} onLearning={() => setTab("learning")} />
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
                <p className="mt-3 text-xs text-muted-foreground">
                  {current.confidence?.learning
                    ? "Learning pick — the model is unsure about pieces like this; your reaction teaches it the most."
                    : "Exploration pick — about 1 in 6 suggestions deliberately test something new."}
                </p>
              )}
            </div>
            <div className="pb-24 md:pb-6">
              <ProductInfo product={current} inFeed />
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

/** Why a selective feed is empty, and what you can do: never a quietly relaxed threshold. */
function GateEmpty({ gate, learning, onMode, onLearning }: { gate: Gate; learning: boolean; onMode: (m: SelectiveMode) => void; onLearning: () => void }) {
  const others = MODES.filter((m) => m !== gate.mode && (gate.qualified?.[m] ?? 0) > 0);
  const title = learning
    ? "No learning picks right now"
    : gate.state === "calibrated"
      ? "No sufficiently strong matches right now"
      : gate.mode === "balanced"
        ? "Nothing to show yet"
        : `Nothing can be shown as ${gate.mode_label.toLowerCase()} yet`;
  return (
    <EmptyState icon={learning ? <FlaskConical /> : <ShieldCheck />} title={title}
      action={
        <>
          {others.map((m) => (
            <Button key={m} variant="outline" onClick={() => onMode(m)} data-testid={`switch-${m}`}>
              {MODE_LABEL[m]} ({gate.qualified?.[m]})
            </Button>
          ))}
          {!learning && gate.state !== "unavailable" && (
            <Button variant={others.length ? "ghost" : "outline"} onClick={onLearning}>
              <FlaskConical /> Learning picks
            </Button>
          )}
        </>
      }
    >
      <span data-testid="gate-message">{gate.message}</span>
      {!learning && gate.state === "calibrated" && <span className="mt-2 block">The bar is never lowered to fill this feed; it fills as the model becomes surer of your taste.</span>}
    </EmptyState>
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
