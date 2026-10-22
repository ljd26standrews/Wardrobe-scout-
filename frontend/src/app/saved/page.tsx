"use client";
import Link from "next/link";
import useSWR from "swr";
import { toast } from "sonner";
import { Bookmark, Trash2 } from "lucide-react";
import { api, type Product, type WardrobeResponse } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, EmptyState, ErrorState, Skeleton } from "@/components/ui/misc";
import { Select } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ProductCard } from "@/components/product/product-card";
import { formatGBP } from "@/lib/utils";

type SavedResponse = { items: Product[]; totals: Record<string, { count: number; landed_gbp: number; with_estimate: number }> };

const LISTS = [
  { key: "loved", label: "Loved" },
  { key: "liked", label: "Shortlist" },
  { key: "later", label: "Later" },
  { key: "maybe", label: "Maybe" },
  { key: "wishlist", label: "Wishlist" },
];

export default function SavedPage() {
  const { data, error, isLoading, mutate } = useSWR<SavedResponse>("/api/saved");
  const { data: wardrobe } = useSWR<WardrobeResponse>("/api/wardrobe");
  const budget = wardrobe?.budget;

  const update = async (id: number, patch: Record<string, unknown>) => {
    await api.patch(`/api/saved/${id}`, patch);
    mutate();
  };

  return (
    <div>
      <p className="label">Your decisions</p>
      <h1 className="page-title mt-2">Saved</h1>
      {error ? (
        <ErrorState error={error} retry={() => mutate()} />
      ) : isLoading || !data ? (
        <Skeleton className="mt-8 h-96" />
      ) : (
        <Tabs defaultValue="loved" className="mt-6">
          <TabsList>
            {LISTS.map((l) => (
              <TabsTrigger key={l.key} value={l.key}>
                {l.label} <span className="ml-1 text-muted-foreground">{data.totals[l.key]?.count ?? 0}</span>
              </TabsTrigger>
            ))}
          </TabsList>
          {LISTS.map((l) => {
            const items = data.items.filter((i) => i.saved?.list === l.key);
            const t = data.totals[l.key];
            return (
              <TabsContent key={l.key} value={l.key}>
                {t && t.with_estimate > 0 && (
                  <Card className="mb-6 flex flex-wrap items-baseline gap-x-8 gap-y-2 p-4 text-sm">
                    <span>
                      <span className="label mr-2">Estimated landed total</span>
                      <span className="font-serif text-2xl">{formatGBP(t.landed_gbp)}</span>
                      <span className="ml-1 text-muted-foreground">for {t.with_estimate} item{t.with_estimate === 1 ? "" : "s"} with prices</span>
                    </span>
                    {budget?.season_gbp ? (
                      <span className={t.landed_gbp > budget.season_gbp ? "text-destructive" : "text-muted-foreground"}>
                        Season budget {formatGBP(budget.season_gbp)} · {t.landed_gbp > budget.season_gbp ? "over by " + formatGBP(t.landed_gbp - budget.season_gbp) : formatGBP(budget.season_gbp - t.landed_gbp) + " left"}
                      </span>
                    ) : (
                      <Link href="/wardrobe" className="text-muted-foreground underline-offset-4 hover:underline">
                        Set a budget
                      </Link>
                    )}
                  </Card>
                )}
                {!items.length ? (
                  <EmptyState icon={<Bookmark />} title={`Nothing in ${l.label} yet`}>
                    {l.key === "wishlist" ? "Move items here from your other lists and set priorities." : "Use Discover to make quick decisions — they collect here."}
                  </EmptyState>
                ) : (
                  <div className="grid grid-cols-2 gap-x-4 gap-y-10 md:grid-cols-3 xl:grid-cols-4">
                    {items.map((p) => (
                      <ProductCard
                        key={p.id}
                        product={p}
                        footer={
                          <div className="space-y-2">
                            {p.saved?.note && <p className="text-xs text-muted-foreground">{p.saved.note}</p>}
                            <div className="flex gap-1.5">
                              <Select className="h-8 flex-1 text-xs" value={p.saved?.list} onChange={(e) => update(p.id, { list_name: e.target.value })} aria-label="Move to list">
                                {LISTS.map((x) => (
                                  <option key={x.key} value={x.key}>
                                    {x.label}
                                  </option>
                                ))}
                              </Select>
                              <Select className="h-8 w-[5.5rem] text-xs" value={p.saved?.priority} onChange={(e) => update(p.id, { priority: Number(e.target.value) })} aria-label="Priority">
                                <option value={1}>High</option>
                                <option value={2}>Medium</option>
                                <option value={3}>Low</option>
                              </Select>
                              <Button
                                size="icon"
                                variant="ghost"
                                className="size-8"
                                aria-label="Remove"
                                onClick={async () => {
                                  await api.del(`/api/saved/${p.id}`);
                                  toast("Removed from saved");
                                  mutate();
                                }}
                              >
                                <Trash2 />
                              </Button>
                            </div>
                          </div>
                        }
                      />
                    ))}
                  </div>
                )}
              </TabsContent>
            );
          })}
        </Tabs>
      )}
    </div>
  );
}
