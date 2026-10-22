"use client";
import { useCallback, useEffect, useState } from "react";
import * as D from "@radix-ui/react-dialog";
import { ChevronLeft, ChevronRight, ImageOff, X, ZoomIn } from "lucide-react";
import type { Product } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Photo } from "./photo";

/** The product's garment photos: one large view (whole garment, never cropped), thumbnails to switch views, arrow
 *  keys, and click to zoom. Size charts are not here: they belong to Sizing. */
export function Gallery({ product, className }: { product: Product; className?: string }) {
  const images = product.images.filter((i) => i.src);
  const [index, setIndex] = useState(0);
  const [zoom, setZoom] = useState(false);
  useEffect(() => setIndex(0), [product.id]);
  const go = useCallback((i: number) => setIndex((i + images.length) % Math.max(1, images.length)), [images.length]);

  if (!images.length) {
    return (
      <div className={cn("image-well flex aspect-[4/5] flex-col items-center justify-center gap-3 p-6 text-center text-muted-foreground", className)}>
        <ImageOff className="size-6" strokeWidth={1.5} aria-hidden />
        <p className="max-w-[18rem] text-[13px] leading-relaxed">
          {product.data_origin === "seed"
            ? "No photos yet: this record comes from your notes and hasn't been checked against the live listing."
            : product.images.length
              ? "The seller's photos couldn't be downloaded."
              : "No photos were collected for this listing."}
        </p>
      </div>
    );
  }
  const current = images[Math.min(index, images.length - 1)];
  return (
    <div className={className}>
      <div
        className="group relative"
        tabIndex={0}
        role="group"
        aria-roledescription="gallery"
        aria-label={`${product.title}: photo ${index + 1} of ${images.length}. Left and right arrows switch photos; Enter zooms.`}
        onKeyDown={(e) => {
          if (e.key === "ArrowRight") go(index + 1);
          else if (e.key === "ArrowLeft") go(index - 1);
          else if (e.key === "Enter") setZoom(true);
          else return;
          e.preventDefault();
        }}
      >
        <button type="button" className="block w-full cursor-zoom-in" onClick={() => setZoom(true)} aria-label="Zoom photo" tabIndex={-1}>
          <Photo src={current.src} alt={`${product.title}, photo ${index + 1}`} className="aspect-[4/5]" eager />
        </button>
        <span className="pointer-events-none absolute right-2 top-2 inline-flex items-center gap-1 rounded-md bg-background/90 px-2 py-1 text-xs text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
          <ZoomIn className="size-3.5" aria-hidden /> Zoom
        </span>
        {images.length > 1 && (
          <>
            <StepButton side="left" onClick={() => go(index - 1)} />
            <StepButton side="right" onClick={() => go(index + 1)} />
          </>
        )}
      </div>
      {images.length > 1 && (
        <div className="no-scrollbar mt-2 flex gap-2 overflow-x-auto" aria-label="Photos">
          {images.map((img, i) => (
            <button
              key={img.id}
              type="button"
              onClick={() => setIndex(i)}
              aria-label={`Photo ${i + 1} of ${images.length}`}
              aria-current={i === index ? "true" : undefined}
              className={cn("w-14 shrink-0 rounded-md border-2 p-0.5 [@media(pointer:coarse)]:w-16", i === index ? "border-primary" : "border-transparent hover:border-border")}
            >
              <Photo src={img.src} alt="" className="aspect-[4/5] rounded-sm" />
            </button>
          ))}
        </div>
      )}
      <Zoom images={images.map((i) => i.src!)} title={product.title} open={zoom} index={index} onIndex={setIndex} onOpenChange={setZoom} />
    </div>
  );
}

function StepButton({ side, onClick }: { side: "left" | "right"; onClick: () => void }) {
  const Icon = side === "left" ? ChevronLeft : ChevronRight;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={side === "left" ? "Previous photo" : "Next photo"}
      tabIndex={-1}
      className={cn(
        "absolute top-1/2 inline-flex size-10 -translate-y-1/2 items-center justify-center rounded-md bg-background/90 text-foreground opacity-0 transition-opacity group-hover:opacity-100 [@media(pointer:coarse)]:opacity-100",
        side === "left" ? "left-2" : "right-2",
      )}
    >
      <Icon className="size-5" />
    </button>
  );
}

/** Full-screen zoom: the whole photo first; click or tap to magnify at that point, move to pan, arrows to switch. */
function Zoom({ images, title, open, index, onIndex, onOpenChange }: { images: string[]; title: string; open: boolean; index: number; onIndex: (i: number) => void; onOpenChange: (o: boolean) => void }) {
  const [magnified, setMagnified] = useState(false);
  const [origin, setOrigin] = useState("50% 50%");
  useEffect(() => setMagnified(false), [index, open]);
  const step = (d: number) => onIndex((index + d + images.length) % images.length);
  return (
    <D.Root open={open} onOpenChange={onOpenChange}>
      <D.Portal>
        <D.Overlay className="fixed inset-0 z-50 bg-background" />
        <D.Content
          className="fixed inset-0 z-50 flex flex-col focus:outline-none"
          onKeyDown={(e) => {
            if (e.key === "ArrowRight") step(1);
            else if (e.key === "ArrowLeft") step(-1);
          }}
        >
          <div className="flex h-14 items-center justify-between border-b px-4">
            <D.Title className="truncate text-sm font-medium">
              {title} · {index + 1} / {images.length}
            </D.Title>
            <D.Description className="sr-only">Click the photo to magnify, move to pan, arrow keys switch photos, Escape closes.</D.Description>
            <D.Close className="inline-flex size-10 items-center justify-center rounded-md hover:bg-secondary [@media(pointer:coarse)]:size-11" aria-label="Close zoom">
              <X className="size-5" />
            </D.Close>
          </div>
          <div
            className={cn("relative flex-1 overflow-hidden bg-imagewell", magnified ? "cursor-zoom-out" : "cursor-zoom-in")}
            onClick={(e) => {
              const r = e.currentTarget.getBoundingClientRect();
              setOrigin(`${((e.clientX - r.left) / r.width) * 100}% ${((e.clientY - r.top) / r.height) * 100}%`);
              setMagnified((m) => !m);
            }}
            onPointerMove={(e) => {
              if (!magnified) return;
              const r = e.currentTarget.getBoundingClientRect();
              setOrigin(`${((e.clientX - r.left) / r.width) * 100}% ${((e.clientY - r.top) / r.height) * 100}%`);
            }}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={images[index]}
              alt={`${title}, photo ${index + 1}`}
              draggable={false}
              className="h-full w-full select-none object-contain"
              style={{ transform: magnified ? "scale(2.5)" : undefined, transformOrigin: origin }}
            />
          </div>
          {images.length > 1 && (
            <div className="flex items-center justify-center gap-2 border-t py-2">
              <button type="button" onClick={() => step(-1)} className="inline-flex size-11 items-center justify-center rounded-md hover:bg-secondary" aria-label="Previous photo">
                <ChevronLeft className="size-5" />
              </button>
              <button type="button" onClick={() => step(1)} className="inline-flex size-11 items-center justify-center rounded-md hover:bg-secondary" aria-label="Next photo">
                <ChevronRight className="size-5" />
              </button>
            </div>
          )}
        </D.Content>
      </D.Portal>
    </D.Root>
  );
}
