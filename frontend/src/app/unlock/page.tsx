"use client";
import { useState } from "react";
import { Lock } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function UnlockPage() {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="mx-auto mt-16 max-w-sm text-center">
      <Lock className="mx-auto size-7 text-muted-foreground" strokeWidth={1.25} />
      <h1 className="page-title mt-4">Locked</h1>
      <p className="mt-3 text-sm text-muted-foreground">This Wardrobe Scout is shared on your network. Enter the access code set in WS_ACCESS_CODE.</p>
      <form
        className="mt-6 flex gap-2"
        onSubmit={async (e) => {
          e.preventDefault();
          const r = await fetch("/api/unlock", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) });
          if (r.ok) window.location.href = "/";
          else setError("Incorrect access code");
        }}
      >
        <Input type="password" autoFocus aria-label="Access code" value={code} onChange={(e) => setCode(e.target.value)} placeholder="Access code" />
        <Button type="submit">Unlock</Button>
      </form>
      {error && (
        <p className="mt-3 text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
