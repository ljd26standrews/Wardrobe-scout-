"use client";
import * as React from "react";
import * as D from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

export const Dialog = D.Root;
export const DialogTrigger = D.Trigger;
export const DialogClose = D.Close;

export function DialogContent({ className, children, title, description, side, ...props }: React.ComponentProps<typeof D.Content> & { title: string; description?: string; side?: "center" | "bottom" | "right" }) {
  const placement =
    side === "right"
      ? "inset-y-0 right-0 h-full w-full max-w-md rounded-none border-l data-[state=open]:animate-in"
      : "left-1/2 top-1/2 w-[calc(100%-1.5rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-2xl max-h-[88dvh] max-sm:top-auto max-sm:bottom-0 max-sm:translate-y-0 max-sm:w-full max-sm:max-w-none max-sm:rounded-b-none max-sm:rounded-t-3xl";
  return (
    <D.Portal>
      <D.Overlay className="fixed inset-0 z-50 bg-black/40 backdrop-blur-[2px] animate-fade-in" />
      <D.Content
        className={cn("fixed z-50 flex flex-col overflow-hidden border bg-popover text-popover-foreground shadow-2xl focus:outline-none animate-fade-in", placement, className)}
        {...props}
      >
        <div className="flex items-start justify-between gap-4 border-b px-5 py-4">
          <div>
            <D.Title className="font-serif text-2xl leading-tight">{title}</D.Title>
            {description ? <D.Description className="mt-1 text-sm text-muted-foreground">{description}</D.Description> : <D.Description className="sr-only">{title}</D.Description>}
          </div>
          <D.Close className="rounded-full p-1.5 text-muted-foreground hover:bg-secondary" aria-label="Close">
            <X className="size-5" />
          </D.Close>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4 safe-bottom">{children}</div>
      </D.Content>
    </D.Portal>
  );
}
