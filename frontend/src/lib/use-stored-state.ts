"use client";
import { useCallback, useMemo, useSyncExternalStore } from "react";

// Kept in memory as well, so a choice still works for this visit when the browser refuses storage (private windows,
// blocked site data).
const memory = new Map<string, string>();
const listeners = new Map<string, Set<() => void>>();

function read(key: string): string | null {
  try {
    return localStorage.getItem(key) ?? memory.get(key) ?? null;
  } catch {
    return memory.get(key) ?? null;
  }
}

/** State remembered in this browser (a view, a tab, filters). It is read before the first render on client-side
 *  navigation, so going Back shows the same view (and the browser can restore the scroll position); on the very first
 *  page load it starts from the initial value and switches straight after hydration. */
export function useStoredState<T>(key: string, initial: T): [T, (v: T) => void] {
  const raw = useSyncExternalStore(
    (cb) => {
      const set = listeners.get(key) ?? new Set();
      set.add(cb);
      listeners.set(key, set);
      return () => set.delete(cb);
    },
    () => read(key),
    () => null,
  );
  const value = useMemo(() => {
    if (raw == null) return initial;
    try {
      return JSON.parse(raw) as T;
    } catch {
      return initial;
    }
    // `initial` is a fallback only; a new literal on each render must not produce a new value.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [raw]);
  const set = useCallback(
    (v: T) => {
      const json = JSON.stringify(v);
      memory.set(key, json);
      try {
        localStorage.setItem(key, json);
      } catch {}
      listeners.get(key)?.forEach((l) => l());
    },
    [key],
  );
  return [value, set];
}
