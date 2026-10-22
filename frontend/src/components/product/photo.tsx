"use client";
import { useState } from "react";
import { ImageOff } from "lucide-react";
import { cn } from "@/lib/utils";

/** A product photo: the whole garment (object-contain) on the neutral image well, never cropped. A photo that
 *  fails to load, or that the scan could not download (``unavailable``), shows a plain well saying so, never a
 *  stand-in image. */
export function Photo({
  src,
  alt,
  className,
  eager,
  unavailable,
  children,
}: {
  src?: string | null;
  alt: string;
  className?: string;
  eager?: boolean;
  unavailable?: boolean;
  children?: React.ReactNode;
}) {
  const [failed, setFailed] = useState(false);
  return (
    <div className={cn("image-well", className)}>
      {src && !failed ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={src} alt={alt} loading={eager ? "eager" : "lazy"} decoding="async" draggable={false} onError={() => setFailed(true)} className="select-none" />
      ) : (
        <div className="flex h-full w-full flex-col items-center justify-center gap-2 p-4 text-center text-xs text-muted-foreground">
          <ImageOff className="size-5" strokeWidth={1.5} aria-hidden />
          {src || unavailable ? "Photo unavailable" : "No photo"}
        </div>
      )}
      {children}
    </div>
  );
}
