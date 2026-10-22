"use client";
import { useCallback } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";

/** "like_later" is Like with "not now": you like the look but won't buy it now (saved to Later). */
export type ActionName = "love" | "like" | "like_later" | "maybe" | "dislike" | "skip" | "purchased";

const MESSAGES: Record<ActionName, string> = {
  love: "Loved — strong signal recorded",
  like: "Saved to your shortlist",
  like_later: "Saved to Later — you like the look, not buying now",
  maybe: "Saved to Maybe",
  dislike: "Noted — similar items will be deprioritised",
  skip: "Skipped (nothing learned)",
  purchased: "Added to My Wardrobe",
};

/** Records a decision. ``onUndo`` runs after the toast's Undo removed it (e.g. to put the card back). */
export function useFeedback(onDone?: () => void, onUndo?: (productId: number) => void) {
  return useCallback(
    async (productId: number, action: ActionName, reasons: string[] = [], note?: string, context: Record<string, unknown> = {}) => {
      try {
        const [sent, sentReasons] = action === "like_later" ? (["like", [...reasons, "not_now"]] as const) : ([action, reasons] as const);
        const res = await api.post<{ feedback_id: number }>("/api/feedback", { product_id: productId, action: sent, reasons: sentReasons, note, context });
        onDone?.();
        if (action !== "skip") {
          toast(MESSAGES[action], {
            description: reasons.some((r) => ["too_expensive", "already_own", "wrong_size", "construction", "quality_unclear"].includes(r))
              ? "Recorded about buying or quality, not as a dislike of the look."
              : undefined,
            action: {
              label: "Undo",
              onClick: async () => {
                await api.del(`/api/feedback/${res.feedback_id}`);
                toast("Undone");
                onUndo?.(productId);
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
    [onDone, onUndo],
  );
}
