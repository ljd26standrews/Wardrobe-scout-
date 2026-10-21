"use client";
import { useRef, useState } from "react";
import useSWR from "swr";
import { toast } from "sonner";
import { ImagePlus, Layers, Plus, Shirt, ThumbsDown, Trash2 } from "lucide-react";
import { api, type WardrobeResponse } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, EmptyState, ErrorState, Skeleton } from "@/components/ui/misc";
import { Dialog, DialogContent, DialogTrigger } from "@/components/ui/dialog";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Swatches } from "@/components/product/meta";
import { cn, formatGBP, titleCase } from "@/lib/utils";

const CATEGORIES = ["t-shirt", "polo", "shirt", "sweatshirt", "hoodie", "knitwear", "cardigan", "zip jacket", "jacket", "blazer", "overcoat", "down jacket", "gilet", "trousers", "jeans", "shorts", "footwear", "accessories", "bag"];

function AddItemDialog({ onAdded }: { onAdded: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const formRef = useRef<HTMLFormElement>(null);
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button data-testid="add-wardrobe">
          <Plus /> Add a piece you own
        </Button>
      </DialogTrigger>
      <DialogContent title="Add to wardrobe" description="A photo helps the visual model learn what you actually wear.">
        <form
          ref={formRef}
          className="grid gap-4"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            try {
              await api.form("/api/wardrobe", new FormData(e.currentTarget));
              toast.success("Added to wardrobe");
              setOpen(false);
              onAdded();
            } catch (err) {
              toast.error(err instanceof Error ? err.message : "Could not add");
            } finally {
              setBusy(false);
            }
          }}
        >
          <Field label="Name">
            <Input name="name" required placeholder="e.g. Vince white tee" />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Category">
              <Select name="category" defaultValue="">
                <option value="">Detect from name</option>
                {CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {titleCase(c)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Brand">
              <Input name="brand" />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Colours" hint="comma-separated">
              <Input name="colours" placeholder="white" />
            </Field>
            <Field label="Seasons" hint="comma-separated">
              <Input name="seasons" placeholder="autumn, winter" />
            </Field>
          </div>
          <Field label="Materials">
            <Input name="materials" placeholder="100% cotton" />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="How much do you love it?">
              <Select name="rating" defaultValue="4">
                {[5, 4, 3, 2, 1].map((r) => (
                  <option key={r} value={r}>
                    {"★".repeat(r)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Link (optional)">
              <Input name="source_url" placeholder="https://" />
            </Field>
          </div>
          <Field label="Photo">
            <Input name="image" type="file" accept="image/*" />
          </Field>
          <Field label="Notes">
            <Textarea name="notes" rows={2} />
          </Field>
          <Button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Add"}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}

type Inspiration = { id: number; image: string; note: string | null; polarity: number };

function InspirationPanel() {
  const { data, mutate } = useSWR<Inspiration[]>("/api/inspiration");
  const [polarity, setPolarity] = useState(1);
  const upload = async (files: FileList | null) => {
    if (!files?.length) return;
    for (const f of Array.from(files)) {
      const fd = new FormData();
      fd.append("image", f);
      fd.append("polarity", String(polarity));
      try {
        await api.form("/api/inspiration", fd);
      } catch (e) {
        toast.error(`${f.name}: ${e instanceof Error ? e.message : "failed"}`);
      }
    }
    toast.success("Training images added");
    mutate();
  };
  return (
    <div>
      <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground">
        Upload photos or screenshots of clothing you admire (or deliberately dislike). They become visual training examples alongside your swipes.
      </p>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Select className="h-10 w-auto" value={polarity} onChange={(e) => setPolarity(Number(e.target.value))} aria-label="Example type">
          <option value={1}>Inspiration (more like this)</option>
          <option value={-1}>Anti-inspiration (less like this)</option>
        </Select>
        <label className="inline-flex h-10 cursor-pointer items-center gap-2 rounded-full bg-primary px-5 text-sm text-primary-foreground">
          <ImagePlus className="size-4" /> Upload images
          <input type="file" accept="image/*" multiple className="hidden" onChange={(e) => upload(e.target.files)} />
        </label>
      </div>
      <div className="mt-6 grid grid-cols-3 gap-3 md:grid-cols-6">
        {(data ?? []).map((i) => (
          <div key={i.id} className="group relative aspect-[4/5] overflow-hidden rounded-xl bg-muted">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={i.image} alt={i.note ?? "inspiration"} className="h-full w-full object-cover" />
            {i.polarity < 0 && (
              <span className="absolute left-2 top-2 rounded-full bg-background/90 p-1">
                <ThumbsDown className="size-3" />
              </span>
            )}
            <button
              aria-label="Remove"
              className="absolute right-2 top-2 rounded-full bg-background/90 p-1 opacity-0 transition group-hover:opacity-100"
              onClick={async () => {
                await api.del(`/api/inspiration/${i.id}`);
                mutate();
              }}
            >
              <Trash2 className="size-3.5" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function BudgetCard({ budget, onSaved }: { budget: WardrobeResponse["budget"]; onSaved: () => void }) {
  const [m, setM] = useState(budget.monthly_gbp?.toString() ?? "");
  const [s, setS] = useState(budget.season_gbp?.toString() ?? "");
  return (
    <Card className="p-5">
      <p className="label mb-3">Spending budget (optional)</p>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Monthly £">
          <Input inputMode="decimal" value={m} onChange={(e) => setM(e.target.value)} />
        </Field>
        <Field label="This season £">
          <Input inputMode="decimal" value={s} onChange={(e) => setS(e.target.value)} />
        </Field>
      </div>
      <Button
        size="sm"
        variant="outline"
        className="mt-3"
        onClick={async () => {
          await api.put("/api/budget", { monthly_gbp: m, season_gbp: s });
          toast.success("Budget saved");
          onSaved();
        }}
      >
        Save budget
      </Button>
    </Card>
  );
}

export default function WardrobePage() {
  const [season, setSeason] = useState("autumn/winter");
  const { data, error, isLoading, mutate } = useSWR<WardrobeResponse>(`/api/wardrobe?season=${encodeURIComponent(season)}`);

  return (
    <div>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="label">What you own</p>
          <h1 className="page-title mt-2">My Wardrobe</h1>
          <p className="mt-2 max-w-xl text-sm text-muted-foreground">The aim is a cohesive, high-quality wardrobe — fewer, better pieces, not more purchases.</p>
        </div>
        <AddItemDialog onAdded={() => mutate()} />
      </div>
      {error ? (
        <ErrorState error={error} retry={() => mutate()} />
      ) : isLoading || !data ? (
        <Skeleton className="mt-8 h-96" />
      ) : (
        <Tabs defaultValue="pieces" className="mt-8">
          <TabsList>
            <TabsTrigger value="pieces">Pieces · {data.items.length}</TabsTrigger>
            <TabsTrigger value="plan">Gaps & redundancy</TabsTrigger>
            <TabsTrigger value="outfits">Outfits</TabsTrigger>
            <TabsTrigger value="inspiration">Inspiration</TabsTrigger>
          </TabsList>
          <TabsContent value="pieces">
            {!data.items.length ? (
              <EmptyState icon={<Shirt />} title="Your wardrobe is empty">
                Add pieces you already own — with photos if possible. Items you mark as Purchased in Discover are added automatically.
              </EmptyState>
            ) : (
              <div className="grid grid-cols-2 gap-x-4 gap-y-8 md:grid-cols-4 xl:grid-cols-5">
                {data.items.map((w) => (
                  <div key={w.id} className="group">
                    <div className="relative aspect-[4/5] overflow-hidden rounded-xl bg-muted">
                      {w.image ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={w.image} alt={w.name} className="h-full w-full object-cover" />
                      ) : (
                        <div className="flex h-full items-center justify-center text-muted-foreground">
                          <Shirt className="size-8" strokeWidth={1} />
                        </div>
                      )}
                      <button
                        aria-label="Remove from wardrobe"
                        className="absolute right-2 top-2 rounded-full bg-background/90 p-1.5 opacity-0 transition group-hover:opacity-100"
                        onClick={async () => {
                          if (!confirm(`Remove ${w.name}?`)) return;
                          await api.del(`/api/wardrobe/${w.id}`);
                          mutate();
                        }}
                      >
                        <Trash2 className="size-3.5" />
                      </button>
                    </div>
                    <p className="label mt-2">{titleCase(w.category ?? "uncategorised")}</p>
                    <p className="mt-0.5 text-sm">{w.name}</p>
                    <div className="mt-1 flex items-center gap-2">
                      <Swatches colours={w.colours} />
                      {w.rating && <span className="text-xs text-muted-foreground">{"★".repeat(w.rating)}</span>}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </TabsContent>
          <TabsContent value="plan">
            <div className="mb-6 flex gap-2">
              {["autumn/winter", "spring/summer"].map((s) => (
                <Button key={s} size="sm" variant={season === s ? "default" : "outline"} onClick={() => setSeason(s)}>
                  {titleCase(s)}
                </Button>
              ))}
            </div>
            <div className="grid gap-6 lg:grid-cols-3">
              <Card className="p-5 lg:col-span-2">
                <p className="label mb-4">Missing essentials</p>
                {data.analysis.gaps.length ? (
                  <ul className="divide-y">
                    {data.analysis.gaps.map((g, i) => (
                      <li key={i} className="flex items-center justify-between gap-3 py-3 text-sm">
                        <span>
                          {titleCase(g.category)}
                          {g.colours.length > 0 && <span className="text-muted-foreground"> in {g.colours.join(" / ")}</span>}
                        </span>
                        <span className="flex items-center gap-3">
                          <span className="tabular-nums text-muted-foreground">
                            {g.have} of {g.want}
                          </span>
                          <a className="text-xs underline-offset-4 hover:underline" href={`/browse`}>
                            find
                          </a>
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-sm text-muted-foreground">No gaps against your essentials list for this season.</p>
                )}
                <p className="label mb-3 mt-8">Possible redundancy</p>
                {data.analysis.redundancy.length ? (
                  <ul className="space-y-2 text-sm">
                    {data.analysis.redundancy.map((r, i) => (
                      <li key={i} className="rounded-xl bg-caution/10 p-3">
                        {r.advice}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-sm text-muted-foreground">No categories with too many similar pieces.</p>
                )}
              </Card>
              <div className="space-y-6">
                <Card className="p-5">
                  <p className="label mb-3">By category</p>
                  <ul className="space-y-1.5 text-sm">
                    {Object.entries(data.analysis.by_category).map(([k, v]) => (
                      <li key={k} className="flex justify-between">
                        <span>{titleCase(k)}</span>
                        <span className="tabular-nums text-muted-foreground">{v}</span>
                      </li>
                    ))}
                    {!data.items.length && <li className="text-muted-foreground">Nothing yet</li>}
                  </ul>
                </Card>
                <BudgetCard budget={data.budget} onSaved={() => mutate()} />
              </div>
            </div>
          </TabsContent>
          <TabsContent value="outfits">
            <div className="mb-6 flex gap-2">
              {["autumn/winter", "spring/summer"].map((s) => (
                <Button key={s} size="sm" variant={season === s ? "default" : "outline"} onClick={() => setSeason(s)}>
                  {titleCase(s)}
                </Button>
              ))}
            </div>
            {!data.analysis.outfits.length ? (
              <EmptyState icon={<Layers />} title="No outfits yet">
                Add at least one top and one pair of trousers (owned or saved) to see combinations.
              </EmptyState>
            ) : (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {data.analysis.outfits.map((o, i) => (
                  <Card key={i} className="p-4">
                    <div className="flex gap-2">
                      {o.pieces.map((p) => (
                        <div key={`${p.kind}-${p.id}`} className="w-1/4 min-w-0 flex-1">
                          <div className={cn("aspect-[4/5] overflow-hidden rounded-lg bg-muted", p.kind === "candidate" && "ring-1 ring-caution")}>
                            {p.image && (
                              // eslint-disable-next-line @next/next/no-img-element
                              <img src={p.image} alt="" className="h-full w-full object-cover" />
                            )}
                          </div>
                          <p className="mt-1 truncate text-[11px]">{p.name}</p>
                        </div>
                      ))}
                    </div>
                    <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
                      <span>harmony {Math.round(o.score * 100)}</span>
                      {o.uses_candidates > 0 && <Badge variant="caution">uses {o.uses_candidates} saved piece{o.uses_candidates > 1 ? "s" : ""}</Badge>}
                    </div>
                  </Card>
                ))}
              </div>
            )}
          </TabsContent>
          <TabsContent value="inspiration">
            <InspirationPanel />
          </TabsContent>
        </Tabs>
      )}
      {data?.budget?.monthly_gbp ? <p className="mt-10 text-xs text-muted-foreground">Monthly budget {formatGBP(data.budget.monthly_gbp)}</p> : null}
    </div>
  );
}
