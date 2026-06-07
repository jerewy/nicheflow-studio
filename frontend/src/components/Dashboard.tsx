import { useState } from "react";

import { PoolingScreen } from "@/components/PoolingScreen";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

type Sub = "pool" | "publish" | "readiness";

const SUBS: { id: Sub; label: string }[] = [
  { id: "pool", label: "Pool & Distribute" },
  { id: "publish", label: "Multi-Account Publish" },
  { id: "readiness", label: "Account Readiness" },
];

export function Dashboard() {
  const [sub, setSub] = useState<Sub>("pool");

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Publishing Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          Global checks across accounts. Pooling works across all accounts in a niche; the other
          tabs are per-account.
        </p>
      </div>

      <div className="flex flex-wrap gap-1 border-b border-border">
        {SUBS.map((s) => (
          <button
            key={s.id}
            onClick={() => setSub(s.id)}
            className={cn(
              "rounded-t-md px-3 py-1.5 text-sm font-medium transition-colors",
              sub === s.id
                ? "border-b-2 border-ring text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {s.label}
          </button>
        ))}
      </div>

      {sub === "pool" && <PoolingScreen />}
      {sub === "publish" && (
        <ComingSoon
          title="Multi-Account Publish"
          detail="Batch publish/schedule due reels across a niche's accounts. Migrating next; for now this runs in the desktop app."
        />
      )}
      {sub === "readiness" && (
        <ComingSoon
          title="Account Readiness"
          detail="Per-account session health, daily caps, cooldowns, and publishability. Migrating next; for now this runs in the desktop app."
        />
      )}
    </div>
  );
}

function ComingSoon({ title, detail }: { title: string; detail: string }) {
  return (
    <Card>
      <CardContent className="space-y-1 p-6">
        <p className="text-sm font-medium">{title}</p>
        <p className="text-sm text-muted-foreground">{detail}</p>
      </CardContent>
    </Card>
  );
}
