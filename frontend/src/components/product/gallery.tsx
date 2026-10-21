"use client";
import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, ImageOff } from "lucide-react";
import type { Product } from "@/lib/api";
import { cn } from "@/lib/utils";

/** Swipeable gallery using native scroll-snap (smooth on iPhone), with arrows + dots on desktop. */
export function Gallery({ product, className, aspect = "aspect-[4/5]", rounded = "rounded-2xl" }: { product: Product; className?: string; aspect?: string; rounded?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [index, setIndex] = useState(0);
  const [failed, setFailed] = useState<Record<number, boolean>>({});
  const images = product.images.filter((i) => i.src);

  useEffect(() => {
    setIndex(0);
    ref.current?.scrollTo({ left: 0 });
  }, [product.id]);

  const go = (i: number) => {
    const el = ref.current;
    if (!el) return;
    const next = Math.max(0, Math.min(images.length - 1, i));
    el.scrollTo({ left: next * el.clientWidth, behavior: "smooth" });
  };

  if (images.length === 0) {
    return (
      <div className={cn("flex flex-col items-center justify-center gap-3 bg-muted text-center text-muted-foreground", aspect, rounded, className)}>
        <ImageOff className="size-7" strokeWidth={1.25} />
        <div className="max-w-[16rem] px-4 text-xs leading-relaxed">
          {product.data_origin === "seed"
            ? "No photographs yet — this seed record hasn't been verified against the live listing."
            : product.images.length
              ? "Photographs couldn't be downloaded from the seller."
              : "No photographs were collected for this listing."}
        </div>
      </div>
    );
  }

  return (
    <div className={cn("group relative overflow-hidden bg-muted", aspect, rounded, className)}>
      <div
        ref={ref}
        className="no-scrollbar flex h-full w-full snap-x snap-mandatory overflow-x-auto overscroll-x-contain"
        onScroll={(e) => {
          const el = e.currentTarget;
          setIndex(Math.round(el.scrollLeft / Math.max(1, el.clientWidth)));
        }}
      >
        {images.map((img, i) => (
          <div key={img.id} className="relative h-full w-full shrink-0 snap-center">
            {failed[img.id] ? (
              <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
                <ImageOff className="mr-2 size-4" /> Image unavailable
              </div>
            ) : (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={img.src!}
                alt={`${product.title} — photo ${i + 1}`}
                loading={i === 0 ? "eager" : "lazy"}
                draggable={false}
                onError={() => setFailed((f) => ({ ...f, [img.id]: true }))}
                className="h-full w-full select-none object-cover"
              />
            )}
          </div>
        ))}
      </div>
      {images.length > 1 && (
        <>
          <button
            aria-label="Previous photo"
            onClick={() => go(index - 1)}
            className={cn("absolute left-3 top-1/2 hidden -translate-y-1/2 rounded-full bg-background/80 p-2 opacity-0 shadow backdrop-blur transition group-hover:opacity-100 md:block", index === 0 && "invisible")}
          >
            <ChevronLeft className="size-4" />
          </button>
          <button
            aria-label="Next photo"
            onClick={() => go(index + 1)}
            className={cn("absolute right-3 top-1/2 hidden -translate-y-1/2 rounded-full bg-background/80 p-2 opacity-0 shadow backdrop-blur transition group-hover:opacity-100 md:block", index === images.length - 1 && "invisible")}
          >
            <ChevronRight className="size-4" />
          </button>
          <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center gap-1.5">
            {images.slice(0, 12).map((img, i) => (
              <span key={img.id} className={cn("h-1.5 rounded-full bg-white/90 shadow transition-all", i === index ? "w-5" : "w-1.5 opacity-60")} />
            ))}
          </div>
          <span className="absolute right-3 top-3 rounded-full bg-black/45 px-2 py-0.5 text-[11px] text-white backdrop-blur">
            {index + 1} / {images.length}
          </span>
        </>
      )}
    </div>
  );
}
