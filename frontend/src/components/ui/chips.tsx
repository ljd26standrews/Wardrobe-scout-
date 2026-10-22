import * as React from "react";
import { cn } from "@/lib/utils";

/** A filter chip: pressed or not, with an optional count. 36 px tall with a mouse, 44 px on touch screens. */
export function Chip({ on, count, className, children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { on: boolean; count?: number }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      className={cn(
        "inline-flex min-h-9 items-center gap-1.5 rounded-full border px-3.5 text-[13px] transition-colors [@media(pointer:coarse)]:min-h-11",
        on ? "border-foreground bg-foreground text-background" : "border-border text-foreground hover:bg-secondary",
        className,
      )}
      {...props}
    >
      {children}
      {count != null && <span className={cn("tabular-nums", on ? "opacity-75" : "text-muted-foreground")}>{count}</span>}
    </button>
  );
}
