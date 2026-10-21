"use client";
import { useCallback } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";

export type ActionName = "love" | "like" | "maybe" | "dislike" | "skip" | "purchased";

const MESSAGES: Record<ActionName, string> = {
  love: "Loved — strong signal recorded",
  like: "Saved to your shortlist",
  maybe: "Saved to Maybe",
  dislike: "Noted — similar items will be deprioritised",
  skip: "Skipped (nothing learned)",
  purchased: "Added to My Wardrobe",
};

export function useFeedback(onDone?: () => void) {
  return useCallback(
    async (productId: number, action: ActionName, reasons: string[] = [], note?: string, context: Record<string, unknown> = {}) => {
      try {
        const res = await api.post<{ feedback_id: number }>("/api/feedback", { product_id: productId, action, reasons, note, context });
        onDone?.();
        if (action !== "skip") {
          toast(MESSAGES[action], {
            description: reasons.includes("too_expensive") ? "Recorded as a value ceiling, not a style dislike." : undefined,
            action: {
              label: "Undo",
              onClick: async () => {
                await api.del(`/api/feedback/${res.feedback_id}`);
                toast("Undone");
                onDone?.();
              },
            },
          });
        }
        return res;
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Could not save feedback");
        throw e;
      }
    },
    [onDone],
  );
}
