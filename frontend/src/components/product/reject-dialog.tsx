"use client";
import { useEffect, useState } from "react";
import { Check } from "lucide-react";
import { REASONS } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export function RejectDialog({ open, onOpenChange, onSubmit, title }: { open: boolean; onOpenChange: (o: boolean) => void; onSubmit: (reasons: string[], note?: string) => void; title?: string }) {
  const [selected, setSelected] = useState<string[]>([]);
  const [note, setNote] = useState("");
  useEffect(() => {
    if (open) {
      setSelected([]);
      setNote("");
    }
  }, [open]);
  const toggle = (v: string) => setSelected((s) => (s.includes(v) ? s.filter((x) => x !== v) : [...s, v]));
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title="Why not this one?" description={title ? `Optional — helps the system learn the right lesson from “${title}”.` : "Optional — reasons help the system learn the right lesson."}>
        <div className="grid gap-2">
          {REASONS.map((r, i) => {
            const on = selected.includes(r.value);
            return (
              <button
                key={r.value}
                onClick={() => toggle(r.value)}
                className={cn("flex items-center gap-3 rounded-xl border px-4 py-3 text-left transition-colors", on ? "border-foreground bg-secondary" : "hover:bg-secondary/60")}
                data-testid={`reason-${r.value}`}
              >
                <span className={cn("flex size-5 shrink-0 items-center justify-center rounded-full border", on && "border-foreground bg-foreground text-background")}>{on && <Check className="size-3" />}</span>
                <span className="flex-1">
                  <span className="block text-sm">{r.label}</span>
                  <span className="block text-xs text-muted-foreground">{r.hint}</span>
                </span>
                <kbd className="hidden text-[10px] text-muted-foreground md:inline">{i + 1}</kbd>
              </button>
            );
          })}
        </div>
        <Textarea className="mt-3" placeholder="Anything else? (optional)" value={note} onChange={(e) => setNote(e.target.value)} rows={2} />
        <div className="mt-4 flex gap-2">
          <Button variant="outline" className="flex-1" onClick={() => onSubmit([], note || undefined)}>
            Just dislike
          </Button>
          <Button className="flex-1" onClick={() => onSubmit(selected, note || undefined)} disabled={!selected.length} data-testid="reject-submit">
            Dislike with {selected.length || ""} reason{selected.length === 1 ? "" : "s"}
          </Button>
        </div>
        <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
          “Too expensive” and “already own” never teach the system that you dislike the style itself.
        </p>
      </DialogContent>
    </Dialog>
  );
}
