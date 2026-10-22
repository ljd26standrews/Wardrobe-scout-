"use client";
import { useEffect, useState } from "react";
import { Check } from "lucide-react";
import { toast } from "sonner";
import { api, REASON_GROUP_LABEL, REASONS, type ReasonGroup } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { requestReasons, useReasonsRequest } from "./use-feedback";

/** Why not this one? Optional, and asked after the dislike is already recorded. Reasons are grouped by what they
 *  are about; only reasons about the look teach the app what you don't like. */
export function ReasonsSheet({ open, onOpenChange, onSubmit, title }: { open: boolean; onOpenChange: (o: boolean) => void; onSubmit: (reasons: string[], note?: string) => void; title?: string }) {
  const [selected, setSelected] = useState<string[]>([]);
  const [note, setNote] = useState("");
  useEffect(() => {
    if (open) {
      setSelected([]);
      setNote("");
    }
  }, [open]);
  const toggle = (v: string) => setSelected((s) => (s.includes(v) ? s.filter((x) => x !== v) : [...s, v]));
  const groups = (Object.keys(REASON_GROUP_LABEL) as ReasonGroup[]).map((g) => ({ g, items: REASONS.filter((r) => r.group === g) }));
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="Why not this one?" description={title ? `Optional. Helps the app learn the right thing from “${title}”.` : "Optional. Helps the app learn the right thing."}>
        <div className="space-y-4">
          {groups.map(({ g, items }) => (
            <fieldset key={g}>
              <legend className="label mb-2">{REASON_GROUP_LABEL[g]}</legend>
              <div className="flex flex-wrap gap-2">
                {items.map((r) => {
                  const on = selected.includes(r.value);
                  return (
                    <button
                      key={r.value}
                      type="button"
                      onClick={() => toggle(r.value)}
                      aria-pressed={on}
                      title={r.hint}
                      data-testid={`reason-${r.value}`}
                      className={cn(
                        "inline-flex min-h-10 items-center gap-1.5 rounded-full border px-3 text-sm transition-colors [@media(pointer:coarse)]:min-h-11",
                        on ? "border-primary bg-primary/10 text-foreground" : "border-input hover:bg-secondary",
                      )}
                    >
                      {on && <Check className="size-3.5 text-primary" aria-hidden />}
                      {r.label}
                    </button>
                  );
                })}
              </div>
            </fieldset>
          ))}
          <Textarea placeholder="Anything else? (optional)" value={note} onChange={(e) => setNote(e.target.value)} rows={2} />
          <div className="flex gap-2">
            <Button variant="outline" className="flex-1" onClick={() => onOpenChange(false)}>
              Skip reasons
            </Button>
            <Button className="flex-1" onClick={() => onSubmit(selected, note || undefined)} disabled={!selected.length && !note} data-testid="reject-submit">
              Save {selected.length ? `${selected.length} reason${selected.length === 1 ? "" : "s"}` : "note"}
            </Button>
          </div>
          <p className="text-xs leading-relaxed text-muted-foreground">Only reasons about the look teach the app what you don&apos;t like. Buying, quality and listing reasons are kept apart.</p>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Mounted once (AppShell): asks for reasons after a dislike was recorded, and adds them to that decision. */
export function ReasonsHost() {
  const req = useReasonsRequest();
  return (
    <ReasonsSheet
      open={!!req}
      title={req?.title}
      onOpenChange={(o) => !o && requestReasons(null)}
      onSubmit={async (reasons, note) => {
        if (!req) return;
        requestReasons(null);
        try {
          await api.post(`/api/feedback/${req.feedbackId}/reasons`, { reasons, note });
          toast(reasons.length ? "Reasons added" : "Note added", {
            description: reasons.some((r) => REASONS.find((x) => x.value === r)?.group !== "look") ? "Reasons about buying or quality don't count as disliking the look." : undefined,
          });
          req.onDone?.();
        } catch (e) {
          toast.error(e instanceof Error ? `Reasons not saved: ${e.message}` : "Reasons not saved");
        }
      }}
    />
  );
}
