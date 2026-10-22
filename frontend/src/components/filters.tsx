"use client";
import useSWR from "swr";
import { SlidersHorizontal, X } from "lucide-react";
import type { Facets } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTrigger } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/input";
import { cn, titleCase } from "@/lib/utils";

export type Filters = {
  q?: string;
  category?: string[];
  brand?: string[];
  seller_id?: number[];
  material?: string[];
  min_price?: number;
  max_price?: number;
  /** The currency min_price/max_price are in; prices in other currencies are converted at your rates. */
  price_currency?: "CNY" | "GBP";
  season?: string;
  size?: string;
  quality?: "" | "evidence" | "unverified";
};

export function activeFilterCount(f: Filters) {
  return Object.entries(f).filter(([k, v]) => k !== "q" && k !== "price_currency" && v != null && v !== "" && !(Array.isArray(v) && !v.length)).length;
}

function Chips<T extends string | number>({ options, value, onChange }: { options: { value: T; label: string; count: number }[]; value: T[]; onChange: (v: T[]) => void }) {
  if (!options.length) return <p className="text-xs text-muted-foreground">Nothing collected yet</p>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map((o) => {
        const on = value.includes(o.value);
        return (
          <button
            key={String(o.value)}
            onClick={() => onChange(on ? value.filter((x) => x !== o.value) : [...value, o.value])}
            className={cn("rounded-full border px-3 py-1 text-xs transition-colors", on ? "border-foreground bg-foreground text-background" : "hover:bg-secondary")}
          >
            {o.label} <span className="opacity-60">{o.count}</span>
          </button>
        );
      })}
    </div>
  );
}

export function FilterSheet({ filters, onChange }: { filters: Filters; onChange: (f: Filters) => void }) {
  const { data } = useSWR<Facets>("/api/facets");
  const n = activeFilterCount(filters);
  const set = (patch: Partial<Filters>) => onChange({ ...filters, ...patch });
  const currency = filters.price_currency ?? "CNY";
  const symbol = currency === "GBP" ? "£" : "¥";
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" data-testid="filters-button">
          <SlidersHorizontal /> Filters{n ? ` · ${n}` : ""}
        </Button>
      </DialogTrigger>
      <DialogContent title="Filter" side="right" description="Applies to Discover and Browse.">
        <div className="space-y-6">
          <Field label="Category">
            <Chips options={(data?.categories ?? []).map((c) => ({ ...c, label: titleCase(c.value) }))} value={filters.category ?? []} onChange={(v) => set({ category: v })} />
          </Field>
          <Field label="Brand / style">
            <Chips options={(data?.brands ?? []).map((c) => ({ ...c, label: c.value }))} value={filters.brand ?? []} onChange={(v) => set({ brand: v })} />
          </Field>
          <Field label="Seller">
            <Chips options={data?.sellers ?? []} value={filters.seller_id ?? []} onChange={(v) => set({ seller_id: v })} />
          </Field>
          <Field label="Material (claimed)">
            <Chips options={(data?.materials ?? []).map((c) => ({ ...c, label: titleCase(c.value) }))} value={filters.material ?? []} onChange={(v) => set({ material: v })} />
          </Field>
          <div className="grid grid-cols-3 gap-3">
            <Field label="Price in">
              <Select value={currency} onChange={(e) => set({ price_currency: e.target.value as "CNY" | "GBP" })} data-testid="price-currency">
                <option value="CNY">Yuan (¥)</option>
                <option value="GBP">Pounds (£)</option>
              </Select>
            </Field>
            <Field label={`Min ${symbol}`}>
              <Input type="number" inputMode="numeric" value={filters.min_price ?? ""} onChange={(e) => set({ min_price: e.target.value ? Number(e.target.value) : undefined, price_currency: currency })} />
            </Field>
            <Field label={`Max ${symbol}`}>
              <Input type="number" inputMode="numeric" value={filters.max_price ?? ""} onChange={(e) => set({ max_price: e.target.value ? Number(e.target.value) : undefined, price_currency: currency })} />
            </Field>
          </div>
          <p className="-mt-3 text-xs text-muted-foreground">Other prices are converted at your rates in Settings. A price whose currency is not stated is left out of a price filter.</p>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Season">
              <Select value={filters.season ?? ""} onChange={(e) => set({ season: e.target.value || undefined })}>
                <option value="">Any</option>
                {["autumn", "winter", "spring", "summer"].map((s) => (
                  <option key={s} value={s}>
                    {titleCase(s)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Size">
              <Select value={filters.size ?? ""} onChange={(e) => set({ size: e.target.value || undefined })}>
                <option value="">Any</option>
                {(data?.sizes ?? []).map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.value}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <Field label="Quality evidence">
            <Select value={filters.quality ?? ""} onChange={(e) => set({ quality: e.target.value as Filters["quality"] })}>
              <option value="">Any</option>
              <option value="evidence">Has independent evidence</option>
              <option value="unverified">Unverified only</option>
            </Select>
          </Field>
          {n > 0 && (
            <Button variant="ghost" className="w-full" onClick={() => onChange({ q: filters.q })}>
              <X /> Clear filters
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
