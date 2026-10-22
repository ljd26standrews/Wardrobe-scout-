"use client";
import { useCallback, useSyncExternalStore } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";

/** "like_later" is Like with "not now": you like the look but won't buy it now (saved to Later). */
export type ActionName = "love" | "like" | "like_later" | "maybe" | "dislike" | "skip" | "purchased";

const MESSAGES: Record<ActionName, string> = {
  love: "Loved",
  like: "Saved to your shortlist",
  like_later: "Saved to Later: you like the look, not buying now",
  maybe: "Saved to Maybe",
  dislike: "Disliked",
  skip: "Skipped",
  purchased: "Added to your wardrobe",
};

// ----------------------------------------------------------------- decided this session
// A piece you decided on is never brought back by an older response: a feed fetched before the decision can arrive
// after it. Undo (or a failed save) removes it again.
let decided = new Set<number>();
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
export function markDecided(id: number) {
  if (decided.has(id)) return;
  decided = new Set(decided).add(id);
  emit();
}
export function unmarkDecided(id: number) {
  if (!decided.has(id)) return;
  decided = new Set(decided);
  decided.delete(id);
  emit();
}
export function useDecided(): ReadonlySet<number> {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => decided,
    () => decided,
  );
}

// ----------------------------------------------------------------- reasons, asked after the decision
export type ReasonsRequest = { feedbackId: number; productId: number; title?: string; onDone?: () => void };
let pending: ReasonsRequest | null = null;
const reasonListeners = new Set<() => void>();
export function requestReasons(r: ReasonsRequest | null) {
  pending = r;
  reasonListeners.forEach((l) => l());
}
export function useReasonsRequest(): ReasonsRequest | null {
  return useSyncExternalStore(
    (l) => {
      reasonListeners.add(l);
      return () => reasonListeners.delete(l);
    },
    () => pending,
    () => null,
  );
}

type Options = {
  /** After the decision is saved (refresh lists, re-rank). */
  onDone?: () => void;
  /** After Undo removed the decision (e.g. bring the piece back). */
  onUndo?: (productId: number) => void;
  /** Hide the piece from feeds at once (Discover); Browse and the product page keep showing it. */
  hide?: boolean;
};

/** Records a decision at once and offers Undo; a dislike also offers "Add a reason" (reasons are never required).
 *  A save that fails rolls back: the piece reappears and an error says so. */
export function useFeedback({ onDone, onUndo, hide = false }: Options = {}) {
  return useCallback(
    async (productId: number, action: ActionName, reasons: string[] = [], note?: string, context: Record<string, unknown> = {}, title?: string) => {
      if (hide && action !== "purchased") markDecided(productId);
      const [sent, sentReasons] = action === "like_later" ? (["like", [...reasons, "not_now"]] as const) : ([action, reasons] as const);
      try {
        const res = await api.post<{ feedback_id: number }>("/api/feedback", { product_id: productId, action: sent, reasons: sentReasons, note, context });
        if (hide) markDecided(productId);
        onDone?.();
        if (action !== "skip") {
          const undo = async () => {
            await api.del(`/api/feedback/${res.feedback_id}`);
            unmarkDecided(productId);
            toast("Undone");
            onUndo?.(productId);
            onDone?.();
          };
          toast(MESSAGES[action], {
            id: `decision-${productId}`,
            description: reasons.some((r) => ["too_expensive", "already_own", "wrong_size", "construction", "quality_unclear"].includes(r))
              ? "Recorded about buying or quality, not as a dislike of the look."
              : undefined,
            action: { label: "Undo", onClick: undo },
            cancel:
              action === "dislike" && !reasons.length
                ? { label: "Add a reason", onClick: () => requestReasons({ feedbackId: res.feedback_id, productId, title, onDone }) }
                : undefined,
            duration: 6000,
          });
        }
        return res;
      } catch (e) {
        unmarkDecided(productId);
        onUndo?.(productId);
        toast.error(e instanceof Error ? `Not saved: ${e.message}` : "Not saved: the decision could not be recorded");
        throw e;
      }
    },
    [onDone, onUndo, hide],
  );
}
