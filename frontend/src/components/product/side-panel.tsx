"use client";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect } from "react";
import useSWR from "swr";
import * as D from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { Product } from "@/lib/api";
import { ErrorState, Skeleton } from "@/components/ui/misc";
import { ProductDetail } from "./detail";

let openedHere = false; // opened from this page (Back closes it) rather than arrived at from a shared link

/** A piece opened beside a grid: ``?piece=<id>`` in the address, so it can be shared and Back closes it. On narrow
 *  screens the full product page opens instead, so a phone keeps one column. */
export function usePiece() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const raw = params.get("piece");
  const pieceId = raw && /^\d+$/.test(raw) ? Number(raw) : null;
  useEffect(() => {
    if (pieceId == null) openedHere = false; // closed by Back: a later shared link must not navigate back
  }, [pieceId]);
  const open = useCallback(
    (p: Product | number) => {
      const id = typeof p === "number" ? p : p.id;
      if (!window.matchMedia("(min-width: 1024px)").matches) {
        router.push(`/product/${id}`);
        return;
      }
      const sp = new URLSearchParams(params.toString());
      sp.set("piece", String(id));
      openedHere = true;
      router.push(`${pathname}?${sp}`, { scroll: false });
    },
    [params, pathname, router],
  );
  const close = useCallback(() => {
    if (openedHere) {
      openedHere = false;
      router.back();
      return;
    }
    const sp = new URLSearchParams(params.toString());
    sp.delete("piece");
    router.replace(sp.size ? `${pathname}?${sp}` : pathname, { scroll: false });
  }, [params, pathname, router]);
  return { pieceId, open, close };
}

export function PiecePanel({ onChanged }: { onChanged?: () => void }) {
  const { pieceId, close } = usePiece();
  const { data, error, mutate } = useSWR<Product>(pieceId != null ? `/api/products/${pieceId}` : null);
  return (
    <D.Root open={pieceId != null} onOpenChange={(o) => !o && close()}>
      <D.Portal>
        <D.Overlay className="fixed inset-0 z-50 bg-black/30" />
        <D.Content
          className="overlay-shadow fixed inset-y-0 right-0 z-50 flex w-full max-w-[640px] flex-col border-l bg-background focus:outline-none"
          aria-describedby={undefined}
          data-testid="piece-panel"
        >
          <div className="flex h-14 shrink-0 items-center justify-between border-b px-5">
            <D.Title className="truncate text-sm font-medium">{data?.title ?? "Piece"}</D.Title>
            <D.Close className="inline-flex size-10 items-center justify-center rounded-md hover:bg-secondary" aria-label="Close">
              <X className="size-5" />
            </D.Close>
          </div>
          <div className="flex-1 overflow-y-auto px-5 py-5">
            {error ? (
              <ErrorState error={error} retry={() => mutate()} />
            ) : !data ? (
              <div className="space-y-4">
                <Skeleton className="aspect-[4/5]" />
                <Skeleton className="h-24" />
              </div>
            ) : (
              <ProductDetail
                product={data}
                variant="panel"
                mutate={() => {
                  mutate();
                  onChanged?.();
                }}
              />
            )}
          </div>
        </D.Content>
      </D.Portal>
    </D.Root>
  );
}
