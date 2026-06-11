import { useCallback, useEffect, useState } from "react";

import { DashboardTable } from "@/components/DashboardTable";
import { PoolingScreen } from "@/components/PoolingScreen";
import { MultiAccountPublish } from "@/components/MultiAccountPublish";
import { AccountReadiness } from "@/components/AccountReadiness";
import { Button } from "@/components/ui/button";
import { bridge } from "@/lib/bridge";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { DashboardAccountStats } from "@/types";

type Sub = "pool" | "publish" | "readiness";

const SUBS: { id: Sub; label: string }[] = [
  { id: "pool", label: "Pool & Distribute" },
  { id: "publish", label: "Multi-Account Publish" },
  { id: "readiness", label: "Account Readiness" },
];

export function Dashboard({ activeAccountId }: { activeAccountId: number }) {
  const [sub, setSub] = useState<Sub>("pool");
  const [stats, setStats] = useState<DashboardAccountStats | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);

  const loadStats = useCallback(async () => {
    try {
      setStats(await bridge.dashboardAccountStats(activeAccountId));
      setStatsError(null);
    } catch (err: unknown) {
      setStatsError(err instanceof Error ? err.message : String(err));
    }
  }, [activeAccountId]);

  useEffect(() => {
    const timer = window.setTimeout(loadStats, 0);
    return () => window.clearTimeout(timer);
  }, [loadStats]);

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Publishing Dashboard</h1>
        <p className="text-sm text-muted-foreground">
          Global checks across accounts. Pooling works across all accounts in a niche; the other
          tabs are per-account.
        </p>
      </div>

      <section className="space-y-3 rounded-xl border bg-card p-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-lg font-semibold">Account stats</h2>
            <p className="text-sm text-muted-foreground">
              {stats?.niche ? `${stats.niche[0].toUpperCase()}${stats.niche.slice(1)} niche` : "Active niche"} posting pace and backlog runway.
            </p>
          </div>
          <Button size="sm" variant="secondary" onClick={loadStats}>Refresh</Button>
        </div>
        {statsError && <p className="text-sm text-destructive">{statsError}</p>}
        <DashboardTable headers={["Account", "Today", "This week", "All-time", "In queue", "Runway", "Next post"]}>
          {stats?.accounts.map((row) => (
            <tr key={row.account_id} className="border-t border-border">
              <td className="whitespace-nowrap px-3 py-2 font-medium">{row.account_name}</td>
              <td className="px-3 py-2 text-center">{row.today} / {row.daily_target}</td>
              <td className="px-3 py-2 text-center">{row.week}</td>
              <td className="px-3 py-2 text-center">{row.all_time}</td>
              <td className="px-3 py-2 text-center">
                {row.in_queue}
                <span className="ml-1 text-xs text-muted-foreground">({row.scheduled} scheduled)</span>
              </td>
              <td className={`px-3 py-2 text-center font-medium ${runwayColor(row.runway_status)}`}>
                {row.runway_days.toFixed(2)}d
              </td>
              <td className="whitespace-nowrap px-3 py-2">{formatDate(row.next_post_at)}</td>
            </tr>
          ))}
        </DashboardTable>
      </section>

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
      {sub === "publish" && <MultiAccountPublish />}
      {sub === "readiness" && <AccountReadiness />}
    </div>
  );
}

function runwayColor(status: "green" | "amber" | "red") {
  if (status === "green") return "text-emerald-500";
  if (status === "amber") return "text-amber-500";
  return "text-destructive";
}
