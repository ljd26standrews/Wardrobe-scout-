"use client";
import { useRouter } from "next/navigation";
import { use } from "react";
import useSWR from "swr";
import { ArrowLeft } from "lucide-react";
import type { Product } from "@/lib/api";
import { ErrorState, Skeleton } from "@/components/ui/misc";
import { ProductDetail } from "@/components/product/detail";

/** The shareable page for one piece (the side panel shows the same details beside a grid). */
export default function ProductPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const { data: p, error, isLoading, mutate } = useSWR<Product>(`/api/products/${id}`);

  return (
    <div>
      <button
        type="button"
        onClick={() => (window.history.length > 1 ? router.back() : router.push("/browse"))}
        className="mb-4 inline-flex min-h-10 items-center gap-1.5 rounded-md text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-4" /> Back
      </button>
      {error ? (
        <ErrorState error={error} retry={() => mutate()} />
      ) : isLoading || !p ? (
        <div className="grid gap-8 md:grid-cols-2">
          <Skeleton className="aspect-[4/5]" />
          <Skeleton className="h-96" />
        </div>
      ) : (
        <ProductDetail product={p} mutate={() => mutate()} />
      )}
    </div>
  );
}
