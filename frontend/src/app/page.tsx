"use client";
import Link from "next/link";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { toast } from "sonner";
import { Bookmark, FlaskConical, Heart, Hourglass, LayoutGrid, Maximize2, Rows3, ShieldCheck, SkipForward, Store, Undo2, X } from "lucide-react";
import type { FeedResponse, Gate, Product, SelectiveMode } from "@/lib/api";
import { api, hasEstimates, MODE_LABEL, MODES, qs } from "@/lib/api";
import { PageHeader } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/misc";
import { FilterSheet, type Filters } from "@/components/filters";
import { approx } from "@/components/product/confidence";
import { Gallery } from "@/components/product/gallery";
import { LandedCost, Price } from "@/components/product/price";
import { PiecePanel, usePiece } from "@/components/product/side-panel";
import { ProductTile, type TileAction } from "@/components/product/tile";
import { unmarkDecided, useDecided, useFeedback, type ActionName } from "@/components/product/use-feedback";
import { useStoredState } from "@/lib/use-stored-state";
import { cn, titleCase } from "@/lib/utils";

type Seen = { feedId: string | null; position: number };
type View = "card" | "grid"; // "card" is the focused one-at-a-time view

export default function DiscoverPage() {
  return (
    <Suspense fallback={<FocusSkeleton />}>
      <Discover />
    </Suspense>
  );
}

function Discover() {
  const [filters, setFilters] = useStoredState<Filters>("ws-filters", {});
  const [view, setView] = useStoredState<View>("ws-discover-mode", "card");
  const [tab, setTab] = useStoredState<"foryou" | "learning">("ws-discover-tab", "foryou");
  // The selectivity mode is a saved setting, so the server and every page agree on it.
  const { data: settings, mutate: mutateSettings } = useSWR<Record<string, any>>("/api/settings"); // eslint-disable-line @typescript-eslint/no-explicit-any
  const selective = settings?.selective as { enabled: boolean; mode: SelectiveMode } | undefined;
  const learning = !!selective?.enabled && tab === "learning";
  // The mode is part of the request (and of the cache key): one mode's pieces are never served from another's cache.
  const url = settings ? `/api/feed${learning ? "/learning" : ""}${qs({ limit: 24, mode: selective?.enabled ? selective.mode : undefined, ...filters })}` : null;
  const { data, error, isLoading, mutate } = useSWR<FeedResponse>(url, { keepPreviousData: true });
  const decided = useDecided();
  const { open: openPiece } = usePiece();
  // The server's gate is the truth: if selective recommendation was switched on or off elsewhere (another tab,
  // Settings), re-read the setting so the controls shown match, and a mode click cannot switch it back.
  const serverOff = data ? data.gate.state === "off" : undefined;
  useEffect(() => {
    if (serverOff !== undefined && selective && serverOff === selective.enabled) mutateSettings();
  }, [serverOff, selective, mutateSettings]);
  const seen = useRef(new Map<number, Seen>());
  const logged = useRef(new Set<string>());

  // Pieces you decided on this session never come back from a response fetched before the decision.
  const items = useMemo(() => {
    data?.items.forEach((p, i) => seen.current.set(p.id, { feedId: data.feed_id, position: i }));
    return (data?.items ?? []).filter((p) => !decided.has(p.id));
  }, [data, decided]);

  // The focused piece stays put while the ranking behind it refreshes, as long as the latest response still has it.
  const [pinned, setPinned] = useState<number | null>(null);
  const queue = useMemo(() => {
    const pin = pinned != null ? items.find((p) => p.id === pinned) : undefined;
    return pin ? [pin, ...items.filter((p) => p.id !== pin.id)] : items;
  }, [items, pinned]);
  const current = queue[0];
  useEffect(() => {
    if (current && current.id !== pinned) setPinned(current.id);
  }, [current, pinned]);

  // Impressions: what was actually displayed, from which feed and position (for a fair evaluation later).
  const logViews = useCallback((feedId: string | null, surface: string, shown: { product_id: number; position: number }[]) => {
    const fresh = shown.filter((it) => !logged.current.has(`${feedId}:${surface}:${it.product_id}`));
    if (!feedId || !fresh.length) return;
    fresh.forEach((it) => logged.current.add(`${feedId}:${surface}:${it.product_id}`));
    api.post(`/api/feed/${feedId}/viewed`, { surface, items: fresh }).catch(() => {});
  }, []);
  useEffect(() => {
    if (view !== "card" || !current) return;
    const s = seen.current.get(current.id);
    if (s) logViews(s.feedId, "card", [{ product_id: current.id, position: s.position }]);
  }, [view, current, logViews]);
  useEffect(() => {
    if (view !== "grid" || !data?.feed_id) return;
    logViews(data.feed_id, "grid", items.map((p) => ({ product_id: p.id, position: seen.current.get(p.id)?.position ?? 0 })));
  }, [view, data?.feed_id, items, logViews]);

  const last = useRef<{ feedbackId: number; productId: number } | null>(null);
  const [canUndo, setCanUndo] = useState(false);
  const restore = useCallback(
    async (productId: number) => {
      setPinned(productId);
      const fresh = await mutate();
      if (fresh && !fresh.items.some((p) => p.id === productId)) {
        toast("It's undecided again, but not in this feed", { description: learning ? "It isn't among today's learning picks." : `It doesn't currently pass ${MODE_LABEL[fresh.gate.mode]}.` });
      }
    },
    [mutate, learning],
  );
  const feedback = useFeedback({ onDone: () => mutate(), onUndo: restore, hide: true });
  const act = useCallback(
    async (p: Product, action: ActionName) => {
      const s = seen.current.get(p.id);
      try {
        const res = await feedback(p.id, action, [], undefined, { explore: !!p.recommendation?.explore, surface: view, feed_id: s?.feedId ?? null, position: s?.position ?? null }, p.title);
        if (action !== "skip") {
          last.current = { feedbackId: res.feedback_id, productId: p.id };
          setCanUndo(true);
        }
      } catch {
        /* rolled back by useFeedback: the piece is shown again and an error says so */
      }
    },
    [feedback, view],
  );
  const undoLast = useCallback(async () => {
    const l = last.current;
    if (!l) return;
    last.current = null;
    setCanUndo(false);
    try {
      await api.del(`/api/feedback/${l.feedbackId}`);
      unmarkDecided(l.productId);
      toast("Undone");
      restore(l.productId);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not undo");
    }
  }, [restore]);

  const chooseMode = useCallback(
    async (m: SelectiveMode) => {
      if (!settings || !selective || m === selective.mode) return;
      const next = { ...settings.selective, mode: m };
      mutateSettings({ ...settings, selective: next }, false);
      try {
        await api.put("/api/settings/selective", next);
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Could not change how selective Discover is");
      }
      mutateSettings();
    },
    [settings, selective, mutateSettings],
  );

  // Keyboard (focused view): L love · S like · W not now · D dislike · → or N skip · Enter details · Z undo
  useEffect(() => {
    if (view !== "card") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if ((e.target as HTMLElement)?.closest("input,textarea,select,[role=dialog],[role=group]")) return;
      const k = e.key.toLowerCase();
      if (k === "z") {
        undoLast();
        e.preventDefault();
        return;
      }
      if (!current) return;
      const map: Record<string, ActionName> = { l: "love", s: "like", w: "like_later", d: "dislike", x: "dislike", n: "skip", arrowright: "skip" };
      if (map[k]) act(current, map[k]);
      else if (k === "enter") openPiece(current);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [view, current, act, undoLast, openPiece]);

  const tileAction = (p: Product, a: TileAction) => act(p, a);

  return (
    <div>
      <PageHeader
        title="Discover"
        description={data ? <FeedStatus data={data} learning={learning} filtered={Object.keys(filters).length > 0} /> : "Pieces chosen for your taste."}
        actions={
          <>
            <FilterSheet filters={filters} onChange={setFilters} />
            <div className="flex rounded-md border border-input p-0.5" role="radiogroup" aria-label="View">
              <ViewButton on={view === "card"} label="One at a time" onClick={() => setView("card")}>
                <Rows3 />
              </ViewButton>
              <ViewButton on={view === "grid"} label="Grid" onClick={() => setView("grid")}>
                <LayoutGrid />
              </ViewButton>
            </div>
          </>
        }
      />

      {selective?.enabled && (
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3 border-b">
          <div className="-mb-px flex" role="tablist" aria-label="Feed">
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
                  "-mb-px inline-flex min-h-11 items-center gap-1.5 border-b-2 border-transparent px-3 text-sm font-medium text-muted-foreground hover:text-foreground",
                  tab === key && "border-primary text-foreground",
                )}
              >
                <Icon className="size-4" strokeWidth={1.75} /> {label}
              </button>
            ))}
            <Link href="/browse" className="inline-flex min-h-11 items-center px-3 text-sm text-muted-foreground hover:text-foreground">
              Browse all
            </Link>
          </div>
          {!learning && (
            <div className="mb-2 flex rounded-md border border-input p-0.5 text-[13px]" role="radiogroup" aria-label="How selective">
              {MODES.map((m) => (
                <button
                  key={m}
                  role="radio"
                  aria-checked={selective.mode === m}
                  data-testid={`mode-${m}`}
                  onClick={() => chooseMode(m)}
                  className={cn("min-h-9 rounded-[5px] px-2.5 font-medium [@media(pointer:coarse)]:min-h-11", selective.mode === m ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground")}
                >
                  {MODE_LABEL[m]}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {error ? (
        <ErrorState error={error} retry={() => mutate()} />
      ) : isLoading || !data ? (
        view === "grid" ? <GridSkeleton /> : <FocusSkeleton />
      ) : !items.length ? (
        <FeedEmpty data={data} learning={learning} filtered={Object.keys(filters).length > 0} onClear={() => setFilters({})} onMode={chooseMode} onLearning={() => setTab("learning")} />
      ) : view === "grid" ? (
        <div className="grid grid-cols-2 gap-x-4 gap-y-8 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-5" data-testid="discover-grid">
          {items.map((p) => (
            <ProductTile key={p.id} product={p} onOpen={openPiece} onAction={tileAction} />
          ))}
        </div>
      ) : (
        current && <Focus product={current} onAct={act} onOpen={() => openPiece(current)} onUndo={canUndo ? undoLast : undefined} />
      )}
      <PiecePanel onChanged={() => mutate()} />
    </div>
  );
}

function ViewButton({ on, label, onClick, children }: { on: boolean; label: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      role="radio"
      aria-checked={on}
      aria-label={label}
      title={label}
      onClick={onClick}
      className={cn("inline-flex size-9 items-center justify-center rounded-[5px] [@media(pointer:coarse)]:size-11 [&_svg]:size-4", on ? "bg-secondary text-foreground" : "text-muted-foreground hover:text-foreground")}
    >
      {children}
    </button>
  );
}

/** One piece at a time: its photos large, its essentials, and the decisions. */
function Focus({ product: p, onAct, onOpen, onUndo }: { product: Product; onAct: (p: Product, a: ActionName) => void; onOpen: () => void; onUndo?: () => void }) {
  const reasons = p.recommendation?.reasons.slice(0, 3) ?? [];
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px] lg:gap-10" data-testid="discover-card" data-product-id={p.id}>
      <Gallery product={p} className="mx-auto w-full max-w-[640px] lg:max-w-none" />
      <div className="min-w-0 pb-28 lg:pb-0">
        <p className="meta">{[p.seller?.name ?? "Unknown seller", p.category && titleCase(p.category)].filter(Boolean).join(" · ")}</p>
        <h2 className="mt-1 text-xl font-semibold leading-7 tracking-tight" data-testid="product-title">
          {p.title}
        </h2>
        <div className="mt-2 space-y-1">
          <Price product={p} size="lg" />
          <div>
            <LandedCost product={p} />
          </div>
        </div>
        {(p.confidence?.estimate != null || p.confidence?.learning || reasons.length > 0) && (
          <div className="mt-5 border-t pt-4">
            <p className="text-sm font-medium">{p.confidence?.learning || p.recommendation?.explore ? "A learning pick" : "Why it's suggested"}</p>
            {p.confidence?.estimate != null && (
              <p className="meta mt-1" data-testid="approval-estimate">
                Estimated chance you&apos;ll like the look: {approx(p.confidence.estimate)}
              </p>
            )}
            {p.confidence?.learning && <p className="meta mt-1">The app is unsure about pieces like this; how you react teaches it the most. Not a recommendation.</p>}
            <ul className="mt-2 space-y-1 text-sm leading-relaxed text-muted-foreground">
              {reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          </div>
        )}
        <Button variant="link" className="mt-3 h-auto" onClick={onOpen} data-testid="open-details">
          <Maximize2 /> Sizing, evidence and offers
        </Button>
        <ActionBar product={p} onAct={onAct} onUndo={onUndo} />
      </div>
    </div>
  );
}

/** The decisions for the focused piece: beside the details on desktop, fixed above the tabs on phones. */
function ActionBar({ product, onAct, onUndo }: { product: Product; onAct: (p: Product, a: ActionName) => void; onUndo?: () => void }) {
  const b = (a: ActionName, label: string, key: string, icon: React.ReactNode, testid: string, primary?: boolean) => (
    <button
      type="button"
      onClick={() => onAct(product, a)}
      data-testid={testid}
      aria-keyshortcuts={key === "→" ? "ArrowRight" : key}
      className={cn(
        "inline-flex min-h-12 flex-1 flex-col items-center justify-center gap-0.5 rounded-md px-2 text-xs font-medium [&_svg]:size-5 lg:min-h-11 lg:flex-row lg:justify-start lg:gap-3 lg:px-3 lg:text-sm",
        primary ? "bg-primary text-primary-foreground hover:bg-primary/90" : "border border-input bg-background hover:bg-secondary",
      )}
    >
      {icon}
      <span>{label}</span>
      <kbd className="ml-auto hidden text-xs font-normal opacity-70 lg:inline">{key}</kbd>
    </button>
  );
  return (
    <div className="fixed inset-x-0 bottom-[calc(env(safe-area-inset-bottom)+52px)] z-30 border-t bg-background px-3 py-2 lg:static lg:mt-5 lg:border-0 lg:p-0">
      <div className="mx-auto flex max-w-xl gap-1.5 lg:max-w-none lg:flex-col lg:gap-2">
        {onUndo && (
          <button
            type="button"
            onClick={onUndo}
            className="inline-flex min-h-12 flex-1 flex-col items-center justify-center gap-0.5 rounded-md px-2 text-xs font-medium text-muted-foreground hover:bg-secondary lg:hidden [&_svg]:size-5"
            data-testid="undo-last-mobile"
          >
            <Undo2 />
            <span>Undo</span>
          </button>
        )}
        {b("dislike", "Dislike", "D", <X />, "action-dislike")}
        {b("skip", "Skip", "→", <SkipForward />, "action-skip")}
        {b("like_later", "Not now", "W", <Hourglass />, "action-later")}
        {b("like", "Like", "S", <Bookmark />, "action-like")}
        {b("love", "Love", "L", <Heart />, "action-love", true)}
      </div>
      {onUndo && (
        <button type="button" onClick={onUndo} className="mt-2 hidden min-h-10 items-center gap-2 rounded-md px-3 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground lg:inline-flex" data-testid="undo-last">
          <Undo2 className="size-4" /> Undo last <kbd className="text-xs opacity-70">Z</kbd>
        </button>
      )}
    </div>
  );
}

/** What this feed is, in plain words. */
function FeedStatus({ data, learning, filtered }: { data: FeedResponse; learning: boolean; filtered: boolean }) {
  const g = data.gate;
  const count = data.items.length;
  let text: string;
  if (g.trial === "ranked") text = g.message ?? "Trial day: the earlier ranking is shown today.";
  else if (g.state === "off") text = `${data.total_candidates} pieces you haven't decided on, ranked for your taste.`;
  else if (learning) text = "Pieces the app is unsure about. Not recommendations: how you react teaches it the most.";
  else if (hasEstimates(g.state))
    text = `${count} piece${count === 1 ? "" : "s"} likely enough to suit your taste for ${g.mode_label}${g.state === "stale" ? " (being updated with your newest decisions)" : ""}.`;
  else if (g.state === "pending") text = "Updating match estimates from your decisions. Meanwhile, this is your taste ranking.";
  else if (g.state === "failed") text = "Match estimates couldn't be updated and will be retried shortly. Meanwhile, this is your taste ranking.";
  else if (g.state === "unavailable") text = "Match estimates are unavailable right now. This is your taste ranking.";
  else {
    const p = g.progress;
    const more = p ? Math.max(0, p.needed - p.labels) : 0;
    text = `Your taste ranking.${more ? ` After ${more} more likes or dislikes, Discover can show how likely each piece is to suit you.` : ""}`;
  }
  return (
    <span data-testid="gate-status">
      {text}
      {data.hidden && (
        <span className="mt-0.5 block" data-testid="hidden-categories-note">
          Hidden by you: {data.hidden.categories.map(titleCase).join(", ")} ({data.hidden.pieces} pieces) ·{" "}
          <Link href="/settings" className="text-primary underline-offset-4 hover:underline">
            change
          </Link>
        </span>
      )}
      {data.price_filter?.unknown_currency_left_out ? (
        <span className="mt-0.5 block">{data.price_filter.unknown_currency_left_out} pieces priced in an unknown currency are left out of your price filter.</span>
      ) : null}
      {filtered && <span className="mt-0.5 block">Filters are on.</span>}
    </span>
  );
}

/** Why there is nothing to show, and what helps. Never filler. */
function FeedEmpty({ data, learning, filtered, onClear, onMode, onLearning }: { data: FeedResponse; learning: boolean; filtered: boolean; onClear: () => void; onMode: (m: SelectiveMode) => void; onLearning: () => void }) {
  const g: Gate = data.gate;
  if (data.total_candidates === 0 && !filtered) {
    return (
      <EmptyState
        icon={<Store />}
        title="Nothing to discover yet"
        action={
          <Button asChild>
            <Link href="/sellers">Add a seller or import a listing</Link>
          </Button>
        }
      >
        Discover shows pieces from the sellers you scan. Nothing has been collected yet, or you have decided on everything.
      </EmptyState>
    );
  }
  const others = MODES.filter((m) => m !== g.mode && (g.qualified?.[m] ?? 0) > 0);
  const strict = !learning && g.state !== "off" && g.mode !== "balanced";
  const title = learning
    ? "No learning picks right now"
    : filtered
      ? "Nothing here matches your filters"
      : strict
        ? hasEstimates(g.state)
          ? `No pieces pass ${g.mode_label} right now`
          : `${g.mode_label} needs match estimates first`
        : "You've seen everything here";
  return (
    <EmptyState
      icon={learning ? <FlaskConical /> : <ShieldCheck />}
      title={title}
      action={
        <>
          {filtered && (
            <Button variant="outline" onClick={onClear}>
              Clear filters
            </Button>
          )}
          {others.map((m) => (
            <Button key={m} variant="outline" onClick={() => onMode(m)} data-testid={`switch-${m}`}>
              {MODE_LABEL[m]} ({g.qualified?.[m]})
            </Button>
          ))}
          {!learning && g.state !== "off" && g.state !== "unavailable" && (
            <Button variant="outline" onClick={onLearning}>
              <FlaskConical /> Learning picks
            </Button>
          )}
          <Button variant="ghost" asChild>
            <Link href="/browse">Browse all</Link>
          </Button>
        </>
      }
    >
      <span data-testid="gate-message">
        {strict && !hasEstimates(g.state)
          ? "It shows only pieces the app is confident you'll like, and it needs more of your likes and dislikes to judge that. Balanced shows your taste ranking meanwhile."
          : strict
            ? "The bar is never lowered to fill this feed. It fills as the app learns your taste."
            : learning
              ? "There are no uncertain pieces to learn from at the moment."
              : "Everything that fits has been decided. New pieces appear after the next scan."}
        {g.state === "stale" && " Estimates are being updated with your newest decisions."}
        {g.withdrawn && ` Earlier estimates were withdrawn: ${g.withdrawn.label}.`}
      </span>
    </EmptyState>
  );
}

function FocusSkeleton() {
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_380px] lg:gap-10">
      <Skeleton className="aspect-[4/5] w-full" />
      <div className="space-y-3">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-7 w-3/4" />
        <Skeleton className="h-6 w-32" />
        <Skeleton className="h-48 w-full" />
      </div>
    </div>
  );
}

function GridSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-8 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-5">
      {Array.from({ length: 8 }).map((_, i) => (
        <div key={i} className="space-y-2">
          <Skeleton className="aspect-[4/5] w-full" />
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-3 w-1/2" />
        </div>
      ))}
    </div>
  );
}
