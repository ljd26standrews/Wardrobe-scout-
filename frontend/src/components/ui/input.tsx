"use client";
import * as React from "react";
import { cn } from "@/lib/utils";

// Inside a Field, its label names the control and its hint describes it, however deeply the control is nested.
const FieldIds = React.createContext<{ label: string; hint?: string } | null>(null);

function useFieldNames(props: { "aria-label"?: string; "aria-labelledby"?: string; "aria-describedby"?: string }) {
  const ids = React.useContext(FieldIds);
  if (!ids) return {};
  return {
    ...(props["aria-label"] || props["aria-labelledby"] ? {} : { "aria-labelledby": ids.label }),
    ...(ids.hint && !props["aria-describedby"] ? { "aria-describedby": ids.hint } : {}),
  };
}

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(({ className, ...props }, ref) => {
  const names = useFieldNames(props);
  return (
    <input
      ref={ref}
      className={cn(
        "flex h-10 w-full rounded-md border border-input bg-card px-3 py-2 text-[15px] md:text-sm placeholder:text-muted-foreground disabled:opacity-50 file:border-0 file:bg-transparent file:text-sm file:font-medium [@media(pointer:coarse)]:h-11",
        className,
      )}
      {...names}
      {...props}
    />
  );
});
Input.displayName = "Input";

export const Textarea = React.forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(({ className, ...props }, ref) => {
  const names = useFieldNames(props);
  return (
    <textarea
      ref={ref}
      className={cn(
        "flex min-h-[80px] w-full rounded-md border border-input bg-card px-3 py-2 text-[15px] md:text-sm placeholder:text-muted-foreground",
        className,
      )}
      {...names}
      {...props}
    />
  );
});
Textarea.displayName = "Textarea";

export const Select = React.forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(({ className, children, ...props }, ref) => {
  const names = useFieldNames(props);
  return (
    <select
      ref={ref}
      className={cn(
        "flex h-10 w-full appearance-none rounded-md border border-input bg-card bg-[length:12px] bg-[right_0.75rem_center] bg-no-repeat px-3 pr-8 text-[15px] md:text-sm [@media(pointer:coarse)]:h-11",
        "bg-[url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 4l4 4 4-4' fill='none' stroke='%23888' stroke-width='1.5'/%3E%3C/svg%3E\")]",
        className,
      )}
      {...names}
      {...props}
    >
      {children}
    </select>
  );
});
Select.displayName = "Select";

export function Label({ className, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={cn("label mb-1.5 block", className)} {...props} />;
}

export function Field({ label, hint, children, className }: { label: string; hint?: string; children: React.ReactNode; className?: string }) {
  const id = React.useId();
  const ids = React.useMemo(() => ({ label: `${id}-label`, hint: hint ? `${id}-hint` : undefined }), [id, hint]);
  return (
    <div className={className}>
      <Label id={ids.label}>{label}</Label>
      <FieldIds.Provider value={ids}>{children}</FieldIds.Provider>
      {hint && (
        <p id={ids.hint} className="mt-1 text-xs text-muted-foreground">
          {hint}
        </p>
      )}
    </div>
  );
}
