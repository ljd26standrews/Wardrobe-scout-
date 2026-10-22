"use client";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import useSWR from "swr";
import { toast } from "sonner";
import { Bookmark, GitCompareArrows, Pencil, Search, Trash2, X } from "lucide-react";
import { api, SAVED_LIST_LABEL, SAVED_LISTS, type Product, type WardrobeResponse } from "@/lib/api";
import { PageHeader } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chips";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/misc";
import { Input, Select, Textarea } from "@/components/ui/input";
import { ProductTile } from "@/components/product/tile";
import { PiecePanel, usePiece } from "@/components/product/side-panel";
import { formatGBP, titleCase } from "@/lib/utils";

type SavedResponse = { items: Product[]; totals: Record<string, { count: number; landed_gbp: number; with_estimate: number }> };
type Saved = NonNullable<Product["saved"]>;

const LISTS = SAVED_LISTS;
const LIST_LABEL = SAVED_LIST_LABEL;
const MAX_COMPARE = 4;

function matches(p: Product, q: string) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  const text = [p.title, p.seller?.name, p.brand, p.category, p.saved?.note].filter(Boolean).join(" ").toLowerCase();
  return words.every((w) => text.includes(w));
}

const short = (t: string) => (t.length > 40 ? `${t.slice(0, 38)}…` : t);

export default function SavedPage() {
  return (
    <Suspense fallback={<Skeleton className="h-96" />}>
      <SavedLists />
    </Suspense>
  );
}

function SavedLists() {
  const sp = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const list = sp.get("list") ?? "all";
  const category = sp.get("category") ?? "all";
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<number[]>([]);
  const { data, error, isLoading, mutate } = useSWR<SavedResponse>("/api/saved");
  const { data: wardrobe } = useSWR<WardrobeResponse>("/api/wardrobe");
  const { open } = usePiece();

  // Lists and category live in the address, so a reload or Back keeps them.
  const setParam = (k: string, v: string) => {
    const next = new URLSearchParams(sp.toString());
    if (v === "all") next.delete(k);
    else next.set(k, v);
    router.replace(next.size ? `${pathname}?${next}` : pathname, { scroll: false });
  };

  const items = useMemo(() => data?.items ?? [], [data]);
  const categories = useMemo(() => [...new Set(items.map((i) => i.category).filter((c): c is string => !!c))].sort(), [items]);
  const shown = items.filter((i) => (list === "all" || i.saved?.list === list) && (category === "all" || i.category === category) && matches(i, q));
  const filtered = list !== "all" || category !== "all" || q.trim() !== "";

  const save = async (p: Product, patch: Partial<Record<"list_name" | "priority" | "note" | "target_size", string | number | null>>) => {
    const before: Saved | null = p.saved;
    await api.patch(`/api/saved/${p.id}`, patch);
    await mutate();
    if (patch.list_name && before && patch.list_name !== before.list) {
      toast(`Moved to ${LIST_LABEL[patch.list_name as string]}`, {
        action: {
          label: "Undo",
          onClick: async () => {
            await api.patch(`/api/saved/${p.id}`, { list_name: before.list });
            mutate();
          },
        },
      });
    } else {
      toast("Notes saved");
    }
  };

  const remove = async (p: Product) => {
    const from = LIST_LABEL[p.saved?.list ?? ""] ?? "Saved";
    // Shown as gone at once; put back if the server refuses.
    await mutate((cur) => cur && { ...cur, items: cur.items.filter((i) => i.id !== p.id) }, { revalidate: false });
    setPicked((x) => x.filter((id) => id !== p.id));
    try {
      const r = await api.del<{ ok: boolean; feedback_id: number | null }>(`/api/saved/${p.id}`);
      mutate();
      toast(`Removed “${short(p.title)}” from ${from}`, {
        action: r.feedback_id
          ? {
              label: "Undo",
              onClick: async () => {
                await api.del(`/api/feedback/${r.feedback_id}`); // restores its list, priority and notes
                mutate();
              },
            }
          : undefined,
      });
    } catch (e) {
      mutate();
      toast.error(`Not removed: ${e instanceof Error ? e.message : "the server did not answer"}`);
    }
  };

  const pick = (id: number) =>
    setPicked((x) => {
      if (x.includes(id)) return x.filter((i) => i !== id);
      if (x.length >= MAX_COMPARE) {
        toast(`Compare up to ${MAX_COMPARE} pieces at a time`);
        return x;
      }
      return [...x, id];
    });

  return (
    <div>
      <PageHeader title="Saved" description="Pieces you liked, in lists. Notes here are for you when buying; they don't change suggestions." />
      {error ? (
        <ErrorState error={error} retry={() => mutate()} />
      ) : isLoading || !data ? (
        <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="aspect-[4/5]" />
          ))}
        </div>
      ) : !items.length ? (
        <EmptyState
          icon={<Bookmark />}
          title="Nothing saved yet"
          action={
            <Button asChild variant="outline">
              <Link href="/">Go to Discover</Link>
            </Button>
          }
        >
          Like or save pieces in Discover or Browse; they collect here, in lists you can sort and annotate.
        </EmptyState>
      ) : (
        <>
          <div className="mb-5 space-y-3">
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Lists">
              <Chip on={list === "all"} count={items.length} onClick={() => setParam("list", "all")} data-testid="list-all">
                All
              </Chip>
              {LISTS.map((l) => (
                <Chip key={l.key} on={list === l.key} count={items.filter((i) => i.saved?.list === l.key).length} onClick={() => setParam("list", l.key)} data-testid={`list-${l.key}`}>
                  {l.label}
                </Chip>
              ))}
            </div>
            <div className="flex flex-wrap gap-2">
              <div className="relative min-w-[12rem] flex-1 sm:max-w-xs">
                <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
                <Input className="pl-9" placeholder="Search saved" aria-label="Search saved pieces" value={q} onChange={(e) => setQ(e.target.value)} data-testid="saved-search" />
              </div>
              {categories.length > 1 && (
                <Select className="w-auto min-w-[10rem]" aria-label="Category" value={category} onChange={(e) => setParam("category", e.target.value)} data-testid="saved-category">
                  <option value="all">All categories</option>
                  {categories.map((c) => (
                    <option key={c} value={c}>
                      {titleCase(c)}
                    </option>
                  ))}
                </Select>
              )}
            </div>
          </div>
          <Totals items={shown} budget={wardrobe?.budget?.season_gbp} />
          {!shown.length ? (
            <EmptyState
              icon={<Search />}
              title={filtered ? "No saved pieces match" : "Nothing here yet"}
              action={
                <Button
                  variant="outline"
                  onClick={() => {
                    setQ("");
                    router.replace(pathname, { scroll: false });
                  }}
                >
                  Show all saved
                </Button>
              }
            >
              {list === "wishlist" ? "Move pieces here from your other lists to keep the ones you mean to buy." : "Try another list, category or search."}
            </EmptyState>
          ) : (
            <div className="grid grid-cols-1 gap-x-6 gap-y-10 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4" data-testid="saved-grid">
              {shown.map((p) => (
                <ProductTile
                  key={p.id}
                  product={p}
                  onOpen={open}
                  status={
                    <span data-testid="saved-list-label">
                      {LIST_LABEL[p.saved?.list ?? ""] ?? "Saved"}
                      {p.saved?.priority === 1 ? " · High priority" : ""}
                    </span>
                  }
                  footer={<SavedControls product={p} picked={picked.includes(p.id)} onPick={() => pick(p.id)} onSave={(patch) => save(p, patch)} onRemove={() => remove(p)} />}
                />
              ))}
            </div>
          )}
        </>
      )}
      {picked.length > 0 && (
        <div className="fixed inset-x-0 bottom-[calc(env(safe-area-inset-bottom)+52px)] z-30 border-t bg-background px-4 py-2 lg:bottom-0 lg:left-[232px]" data-testid="compare-bar">
          <div className="mx-auto flex max-w-[1440px] items-center justify-between gap-3 lg:px-6">
            <p className="text-sm">
              {picked.length} selected
              <span className="meta ml-2">{picked.length < 2 ? "Pick one more to compare" : `Up to ${MAX_COMPARE}`}</span>
            </p>
            <div className="flex gap-2">
              <Button variant="ghost" onClick={() => setPicked([])}>
                <X /> Clear
              </Button>
              <Button disabled={picked.length < 2} onClick={() => router.push(`/compare?ids=${picked.join(",")}`)} data-testid="compare-go">
                <GitCompareArrows /> Compare
              </Button>
            </div>
          </div>
        </div>
      )}
      <PiecePanel onChanged={() => mutate()} />
    </div>
  );
}

/** What the pieces shown would cost delivered, where there is an estimate, against the season budget. */
function Totals({ items, budget }: { items: Product[]; budget?: number | null }) {
  const est = items.filter((i) => i.landed_cost);
  if (!est.length) return null;
  const total = est.reduce((s, i) => s + (i.landed_cost?.total_gbp ?? 0), 0);
  return (
    <p className="meta mb-6" data-testid="saved-total">
      Estimated UK landed cost <span className="font-medium tabular-nums text-foreground">≈ {formatGBP(total)}</span> for{" "}
      {est.length === items.length ? `these ${items.length}` : `${est.length} of these ${items.length}`} piece{items.length === 1 ? "" : "s"}
      {est.length < items.length && " (the others have no landed-cost estimate)"}.{" "}
      {budget ? (
        total > budget ? (
          <span className="text-destructive">Over your season budget of {formatGBP(budget)} by {formatGBP(total - budget)}.</span>
        ) : (
          `${formatGBP(budget - total)} left of your season budget.`
        )
      ) : (
        <Link href="/wardrobe" className="underline underline-offset-4 hover:text-foreground">
          Set a season budget
        </Link>
      )}
    </p>
  );
}

/** Under each saved piece: its purchase notes (editable in place), Compare, and moving or removing it. */
function SavedControls({
  product,
  picked,
  onPick,
  onSave,
  onRemove,
}: {
  product: Product;
  picked: boolean;
  onPick: () => void;
  onSave: (patch: Partial<Record<"list_name" | "priority" | "note" | "target_size", string | number | null>>) => Promise<void>;
  onRemove: () => void;
}) {
  const s = product.saved;
  const [editing, setEditing] = useState(false);
  if (!s) return null;
  if (editing) return <NotesEditor product={product} saved={s} onSave={onSave} onRemove={onRemove} onDone={() => setEditing(false)} />;
  return (
    <div className="space-y-2">
      {(s.target_size || s.note) && (
        <div className="space-y-0.5 text-[13px] leading-5 text-muted-foreground" data-testid="saved-notes">
          {s.target_size && (
            <p>
              Size to check: <span className="text-foreground">{s.target_size}</span>
            </p>
          )}
          {s.note && <p className="line-clamp-3 whitespace-pre-line">{s.note}</p>}
        </div>
      )}
      <div className="flex items-center justify-between gap-2">
        <label className="inline-flex min-h-9 cursor-pointer items-center gap-2 text-[13px] [@media(pointer:coarse)]:min-h-11">
          <input type="checkbox" className="size-4 accent-[hsl(var(--primary))]" checked={picked} onChange={onPick} data-testid="saved-pick" />
          Compare
        </label>
        <Button size="sm" variant="ghost" onClick={() => setEditing(true)} data-testid="saved-edit">
          <Pencil /> Notes and list
        </Button>
      </div>
    </div>
  );
}

function NotesEditor({
  product,
  saved,
  onSave,
  onRemove,
  onDone,
}: {
  product: Product;
  saved: Saved;
  onSave: (patch: Partial<Record<"list_name" | "priority" | "note" | "target_size", string | number | null>>) => Promise<void>;
  onRemove: () => void;
  onDone: () => void;
}) {
  const [listName, setListName] = useState(saved.list);
  const [priority, setPriority] = useState(saved.priority);
  const [size, setSize] = useState(saved.target_size ?? "");
  const [note, setNote] = useState(saved.note ?? "");
  const [busy, setBusy] = useState(false);
  const id = `notes-${product.id}`;
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await onSave({ list_name: listName, priority, target_size: size.trim() || null, note: note.trim() || null });
      onDone();
    } catch (err) {
      toast.error(`Not saved: ${err instanceof Error ? err.message : "the server did not answer"}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} className="space-y-3 rounded-md border p-3" data-testid="saved-editor">
      <div className="grid grid-cols-2 gap-2">
        <div>
          <label htmlFor={`${id}-list`} className="label mb-1 block">
            List
          </label>
          <Select id={`${id}-list`} value={listName} onChange={(e) => setListName(e.target.value)} data-testid="saved-list-select">
            {LISTS.map((l) => (
              <option key={l.key} value={l.key}>
                {l.label}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <label htmlFor={`${id}-priority`} className="label mb-1 block">
            Priority
          </label>
          <Select id={`${id}-priority`} value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
            <option value={1}>High</option>
            <option value={2}>Medium</option>
            <option value={3}>Low</option>
          </Select>
        </div>
      </div>
      <div>
        <label htmlFor={`${id}-size`} className="label mb-1 block">
          Size to check
        </label>
        <Input id={`${id}-size`} value={size} maxLength={20} onChange={(e) => setSize(e.target.value)} placeholder={product.sizes.length ? `Listed: ${product.sizes.slice(0, 5).join(", ")}` : "e.g. M, or 52"} data-testid="saved-size" />
      </div>
      <div>
        <label htmlFor={`${id}-note`} className="label mb-1 block">
          Note
        </label>
        <Textarea id={`${id}-note`} value={note} maxLength={2000} rows={3} className="min-h-[72px]" onChange={(e) => setNote(e.target.value)} placeholder="What to ask the seller, measurements to compare…" data-testid="saved-note" />
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Button type="button" variant="ghost" size="sm" className="text-destructive hover:text-destructive" onClick={onRemove} data-testid="saved-remove">
          <Trash2 /> Remove
        </Button>
        <div className="flex gap-2">
          <Button type="button" variant="ghost" size="sm" onClick={onDone}>
            Cancel
          </Button>
          <Button type="submit" size="sm" disabled={busy} data-testid="saved-save">
            Save
          </Button>
        </div>
      </div>
    </form>
  );
}
