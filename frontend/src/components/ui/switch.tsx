"use client";
import * as React from "react";
import * as S from "@radix-ui/react-switch";
import * as Sl from "@radix-ui/react-slider";
import { cn } from "@/lib/utils";

export function Switch({ className, ...props }: React.ComponentProps<typeof S.Root>) {
  return (
    <S.Root
      className={cn("peer inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent bg-input transition-colors data-[state=checked]:bg-primary", className)}
      {...props}
    >
      <S.Thumb className="pointer-events-none block h-5 w-5 rounded-full bg-background shadow transition-transform data-[state=checked]:translate-x-5" />
    </S.Root>
  );
}

export function Slider({ className, "aria-label": ariaLabel, ...props }: React.ComponentProps<typeof Sl.Root>) {
  return (
    <Sl.Root className={cn("relative flex w-full touch-none select-none items-center py-2", className)} {...props}>
      <Sl.Track className="relative h-1 w-full grow overflow-hidden rounded-full bg-secondary">
        <Sl.Range className="absolute h-full bg-primary" />
      </Sl.Track>
      <Sl.Thumb aria-label={ariaLabel} className="block h-5 w-5 rounded-full border border-input bg-card shadow" />
    </Sl.Root>
  );
}
