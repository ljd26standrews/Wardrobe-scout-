"use client";
import Link from "next/link";
import { Bookmark, Hourglass, X } from "lucide-react";
import type { Product } from "@/lib/api";
import { cn } from "@/lib/utils";
import { approx } from "./confidence";
import { Photo } from "./photo";
import { Price } from "./price";

export type TileAction = "like" | "like_later" | "dislike";

/** One piece in a grid: the whole garment first, then a concise title, seller and price, and at most one quiet line
 *  of status. Quick actions appear on hover or focus (always visible on touch screens). */
export function ProductTile({
  product,
  onOpen,
  onAction,
  status,
  footer,
  href,
}: {
  product: Product;
  onOpen?: (p: Product) => void;
  onAction?: (p: Product, a: TileAction) => void;
  /** Overrides the default status line. */
  status?: React.ReactNode;
  footer?: React.ReactNode;
  href?: string;
}) {
  const link = href ?? `/product/${product.id}`;
  const line =
    status ??
    (product.recommendation?.explore || product.confidence?.learning ? (
      <span>Learning pick</span>
    ) : product.confidence?.estimate != null ? (
      <span data-testid="approval-estimate">Matches your taste {approx(product.confidence.estimate)}</span>
    ) : null);
  return (
    <article className="group relative" data-testid="product-card" data-product-id={product.id}>
      <Link
        href={link}
        onClick={(e) => {
          if (onOpen && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
            e.preventDefault();
            onOpen(product);
          }
        }}
        className="block rounded-md"
        aria-label={`${product.title}, ${product.seller?.name ?? "unknown seller"}`}
      >
        <Photo src={product.images[0]?.src} alt="" className="aspect-[4/5]" />
        <div className="mt-2 space-y-0.5">
          <h3 className="line-clamp-2 text-sm font-medium leading-5 text-foreground" data-testid="product-title">
            {product.title}
          </h3>
          <p className="meta truncate">{product.seller?.name ?? "Unknown seller"}</p>
          <p className="text-[13px] leading-[18px]">
            <Price product={product} />
          </p>
          {line && <p className="meta">{line}</p>}
        </div>
      </Link>
      {onAction && (
        <div
          className={cn(
            "mt-1.5 flex gap-1 transition-opacity [@media(pointer:fine)]:absolute [@media(pointer:fine)]:right-1.5 [@media(pointer:fine)]:top-1.5 [@media(pointer:fine)]:mt-0 [@media(pointer:fine)]:flex-col",
            "[@media(pointer:fine)]:opacity-0 [@media(pointer:fine)]:group-hover:opacity-100 [@media(pointer:fine)]:group-focus-within:opacity-100",
          )}
        >
          <QuickAction label="Like (save)" onClick={() => onAction(product, "like")} testid="tile-like">
            <Bookmark />
          </QuickAction>
          <QuickAction label="Like the look, not now" onClick={() => onAction(product, "like_later")} testid="tile-later">
            <Hourglass />
          </QuickAction>
          <QuickAction label="Dislike" onClick={() => onAction(product, "dislike")} testid="tile-dislike">
            <X />
          </QuickAction>
        </div>
      )}
      {footer && <div className="mt-2">{footer}</div>}
    </article>
  );
}

function QuickAction({ label, onClick, children, testid }: { label: string; onClick: () => void; children: React.ReactNode; testid: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      data-testid={testid}
      className="inline-flex size-9 items-center justify-center rounded-md border border-border bg-background/95 text-foreground hover:bg-secondary [@media(pointer:coarse)]:size-11 [@media(pointer:coarse)]:flex-1 [&_svg]:size-4"
    >
      {children}
    </button>
  );
}
