"use client";
import { useEffect, useRef } from "react";
import { toast } from "sonner";
import { api, type Job } from "@/lib/api";

/** Polls a background job and reports progress via toasts. */
export function watchJob(jobId: number, label: string, onDone?: (job: Job) => void) {
  const id = toast.loading(`${label}…`);
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    try {
      const job = await api.get<Job>(`/api/jobs/${jobId}`);
      if (job.status === "queued" || job.status === "running") {
        toast.loading(`${label} — ${job.message ?? job.status}`, { id });
        setTimeout(tick, 1500);
        return;
      }
      const msg = (job.result?.message as string) || (job.result?.error as string) || job.message || job.status;
      if (job.status === "succeeded") toast.success(`${label} finished`, { id, description: msg });
      else if (job.status === "partial") toast.warning(`${label} partly succeeded`, { id, description: msg });
      else toast.error(`${label} failed`, { id, description: msg, duration: 12000 });
      onDone?.(job);
    } catch (e) {
      toast.error(`${label}: ${e instanceof Error ? e.message : "error"}`, { id });
    }
  };
  setTimeout(tick, 800);
  return () => {
    stopped = true;
  };
}

export function useJobWatcher() {
  const cleanups = useRef<(() => void)[]>([]);
  useEffect(() => () => cleanups.current.forEach((c) => c()), []);
  return (jobId: number, label: string, onDone?: (job: Job) => void) => {
    cleanups.current.push(watchJob(jobId, label, onDone));
  };
}
