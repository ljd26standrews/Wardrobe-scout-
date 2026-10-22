"use client";
import * as React from "react";
import * as Tt from "@radix-ui/react-tooltip";
import { AlertTriangle, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "./button";

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("rounded-md bg-muted motion-safe:animate-pulse", className)} />;
}

export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-lg border bg-card text-card-foreground", className)} {...props} />;
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("size-4 animate-spin", className)} />;
}

export function Tip({ content, children }: { content: React.ReactNode; children: React.ReactNode }) {
  return (
    <Tt.Provider delayDuration={200}>
      <Tt.Root>
        <Tt.Trigger asChild>{children}</Tt.Trigger>
        <Tt.Portal>
          <Tt.Content sideOffset={6} className="overlay-shadow z-50 max-w-xs rounded-md bg-foreground px-3 py-2 text-xs leading-relaxed text-background">
            {content}
          </Tt.Content>
        </Tt.Portal>
      </Tt.Root>
    </Tt.Provider>
  );
}

export function EmptyState({ title, children, icon, action }: { title: string; children?: React.ReactNode; icon?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="mx-auto flex max-w-md flex-col items-center px-6 py-14 text-center">
      {icon && <div className="mb-4 text-muted-foreground [&_svg]:size-7">{icon}</div>}
      <h3 className="section-title">{title}</h3>
      {children && <div className="mt-3 text-sm leading-relaxed text-muted-foreground">{children}</div>}
      {action && <div className="mt-6 flex flex-wrap justify-center gap-2">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div role="alert" className="mx-auto my-10 flex max-w-lg items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-5 text-sm">
      <AlertTriangle className="mt-0.5 size-5 shrink-0 text-destructive" />
      <div className="flex-1">
        <p className="font-medium">Something went wrong</p>
        <p className="mt-1 text-muted-foreground">{msg}</p>
        {retry && (
          <Button variant="outline" size="sm" className="mt-3" onClick={retry}>
            Try again
          </Button>
        )}
      </div>
    </div>
  );
}

export function Meter({ value, className, tone = "default" }: { value: number; className?: string; tone?: "default" | "positive" | "caution" | "destructive" | "muted" }) {
  const colour = { default: "bg-foreground/80", positive: "bg-positive", caution: "bg-caution", destructive: "bg-destructive", muted: "bg-muted-foreground/40" }[tone];
  return (
    <div className={cn("h-1.5 w-full overflow-hidden rounded-full bg-secondary", className)}>
      <div className={cn("h-full rounded-full transition-all duration-500", colour)} style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
    </div>
  );
}
