"use client";
import { useState } from "react";
import { ExternalLink, Minus, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api, type Evidence } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTrigger } from "@/components/ui/dialog";
import { Field, Input, Select, Textarea } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { timeAgo, titleCase } from "@/lib/utils";

/** Host of an http(s) link, or null: anything else (a malformed or non-web link) is never made clickable. */
function webHost(url: string): string | null {
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.hostname : null;
  } catch {
    return null;
  }
}

const KIND_TONE: Record<string, "positive" | "secondary" | "caution" | "muted" | "outline"> = {
  verified: "positive",
  buyer_report: "secondary",
  user_note: "secondary",
  seller_claim: "muted",
  ai_visual: "caution",
};

export const ASPECTS = ["construction", "stitching", "hardware", "material", "gsm", "fit", "sizing", "accuracy", "defect", "reputation", "service", "consistency", "batch", "value"];

export function EvidenceList({ items, onDelete }: { items: Evidence[]; onDelete?: (id: number) => void }) {
  if (!items.length) return <p className="text-sm text-muted-foreground">No evidence recorded yet.</p>;
  const order = ["verified", "buyer_report", "user_note", "seller_claim", "ai_visual"];
  const sorted = [...items].sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind));
  return (
    <ul className="space-y-3">
      {sorted.map((e) => (
        <li key={`${e.scope ?? "p"}-${e.id}`} className="rounded-xl border p-3 text-sm">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge variant={KIND_TONE[e.kind] ?? "outline"}>{e.kind_label}</Badge>
            <Badge variant="outline">{titleCase(e.aspect)}</Badge>
            {e.polarity > 0 && <Badge variant="positive"><Plus className="size-3" /> supports</Badge>}
            {e.polarity < 0 && <Badge variant="destructive"><Minus className="size-3" /> concern</Badge>}
            {e.is_affiliate && <Badge variant="caution">possible affiliate / promo</Badge>}
            {e.scope === "seller" && <Badge variant="muted">about the seller</Badge>}
            {onDelete && e.created_by !== "system" && e.scope !== "seller" && (
              <button aria-label="Delete evidence" className="ml-auto text-muted-foreground hover:text-destructive" onClick={() => onDelete(e.id)}>
                <Trash2 className="size-4" />
              </button>
            )}
          </div>
          <p className="mt-2 leading-relaxed">{e.claim}</p>
          <p className="mt-1.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
            {e.source_url && webHost(e.source_url) ? (
              <a className="inline-flex items-center gap-1 underline-offset-2 hover:underline" href={e.source_url} target="_blank" rel="noreferrer">
                {e.source_title || webHost(e.source_url)} <ExternalLink className="size-3" />
              </a>
            ) : e.source_url ? (
              <span title={e.source_url}>{e.source_title ?? "source link not recognised"}</span>
            ) : (
              <span>{e.source_title ?? "no source link"}</span>
            )}
            <span>· captured {timeAgo(e.captured_at)}</span>
          </p>
        </li>
      ))}
    </ul>
  );
}

export function AddEvidenceDialog({ endpoint, onAdded, trigger }: { endpoint: string; onAdded: () => void; trigger?: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ kind: "buyer_report", aspect: "construction", claim: "", polarity: 1, strength: 0.7, source_url: "", source_title: "", is_affiliate: false });
  const submit = async () => {
    try {
      await api.post(endpoint, { ...form, source_url: form.source_url || null, source_title: form.source_title || null });
      toast.success("Evidence added");
      setOpen(false);
      setForm((f) => ({ ...f, claim: "", source_url: "", source_title: "" }));
      onAdded();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not add evidence");
    }
  };
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{trigger ?? <Button variant="outline" size="sm"><Plus /> Add evidence</Button>}</DialogTrigger>
      <DialogContent title="Add evidence" description="Record a QC photo set, buyer review, test result or your own observation, with its source.">
        <div className="grid gap-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Type">
              <Select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
                <option value="buyer_report">Independent buyer report</option>
                <option value="verified">Verified evidence</option>
                <option value="user_note">My own observation</option>
                <option value="seller_claim">Seller claim</option>
              </Select>
            </Field>
            <Field label="About">
              <Select value={form.aspect} onChange={(e) => setForm({ ...form, aspect: e.target.value })}>
                {ASPECTS.map((a) => (
                  <option key={a} value={a}>
                    {titleCase(a)}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <Field label="What does it show?">
            <Textarea value={form.claim} onChange={(e) => setForm({ ...form, claim: e.target.value })} placeholder="e.g. QC photos show clean double-needle hems and a Riri zip; label reads 70% wool 30% cashmere" />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Direction">
              <Select value={form.polarity} onChange={(e) => setForm({ ...form, polarity: Number(e.target.value) })}>
                <option value={1}>Supports quality</option>
                <option value={0}>Neutral / informational</option>
                <option value={-1}>Raises a concern</option>
              </Select>
            </Field>
            <Field label="Strength">
              <Select value={form.strength} onChange={(e) => setForm({ ...form, strength: Number(e.target.value) })}>
                <option value={0.3}>Weak / anecdotal</option>
                <option value={0.7}>Clear</option>
                <option value={1}>Conclusive</option>
              </Select>
            </Field>
          </div>
          <Field label="Source URL">
            <Input value={form.source_url} onChange={(e) => setForm({ ...form, source_url: e.target.value })} placeholder="https://…" />
          </Field>
          <Field label="Source title">
            <Input value={form.source_title} onChange={(e) => setForm({ ...form, source_title: e.target.value })} placeholder="e.g. r/QualityReps QC thread" />
          </Field>
          <label className="flex items-center justify-between gap-3 text-sm">
            Possibly affiliate / promotional
            <Switch checked={form.is_affiliate} onCheckedChange={(v) => setForm({ ...form, is_affiliate: v })} />
          </label>
          <Button onClick={submit} disabled={form.claim.trim().length < 3}>
            Save evidence
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
