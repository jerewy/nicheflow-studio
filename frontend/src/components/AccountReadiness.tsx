import { useCallback, useEffect, useState } from "react";

import { DashboardTable } from "@/components/DashboardTable";
import { Button } from "@/components/ui/button";
import { useAutoRefresh } from "@/hooks/useAutoRefresh";
import { formatDate } from "@/lib/format";
import { bridge } from "@/lib/bridge";
import type { AccountReadiness as Readiness } from "@/types";

export function AccountReadiness() {
  const [data, setData] = useState<Readiness | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await bridge.dashboardAccountReadiness());
      setMessage(null);
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    }
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(load, 0);
    return () => window.clearTimeout(timer);
  }, [load]);
  // Session health/queue counts change from background work (publishes,
  // re-logins in the desktop app); refresh is local-only, so poll cheaply.
  useAutoRefresh(load, 30000);

  const selectedRow = data?.rows.find((row) => row.account_id === selected);

  const relogin = async () => {
    if (selected === null) return;
    try {
      await bridge.dashboardRelogin(selected);
      setMessage("Opened Instagram login. Log in, then refresh.");
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <section className="space-y-4 rounded-xl border bg-card p-5">
      <div>
        <h2 className="text-lg font-semibold">Account Readiness</h2>
        <p className="text-sm text-muted-foreground">Per-account session health and publish readiness. Refresh is local-only and never contacts Instagram (live checks were removed to protect your accounts from automation flags).</p>
      </div>
      {data && <p className="text-sm text-muted-foreground">{data.totals.account_count} account(s) · {data.totals.total_due_now} due now · {data.totals.total_scheduled} scheduled · next post {formatDate(data.totals.next_post_at)}{data.totals.blocked_accounts ? ` · ${data.totals.blocked_accounts} blocked` : ""}</p>}
      <DashboardTable headers={["Account", "Profile", "Session", "Due now", "Scheduled", "Next post", "Detail"]}>
        {data?.rows.map((row) => (
          <tr key={row.account_id} onClick={() => setSelected(row.account_id)} className={`cursor-pointer border-t border-border hover:bg-accent ${selected === row.account_id ? "bg-accent" : ""}`}>
            <td className="whitespace-nowrap px-3 py-2">{row.account_name}</td>
            <td className="px-3 py-2">{row.profile ?? "—"}</td>
            <td className={`px-3 py-2 ${stateColor(row.session_state)}`}>{row.session_label}</td>
            <td className={`px-3 py-2 text-center ${row.publishable ? "text-emerald-500" : row.due_now ? "text-destructive" : ""}`}>{row.due_now}</td>
            <td className="px-3 py-2 text-center">{row.scheduled}</td>
            <td className="whitespace-nowrap px-3 py-2">{formatDate(row.next_post_at)}</td>
            <td className="px-3 py-2">{row.detail}</td>
          </tr>
        ))}
      </DashboardTable>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="secondary" onClick={load}>Refresh</Button>
        <Button size="sm" variant="outline" disabled={!selectedRow?.profile} onClick={relogin}>Re-login</Button>
        <Button size="sm" variant="outline" disabled={!selectedRow?.login_identifier} onClick={() => selectedRow?.login_identifier && navigator.clipboard.writeText(selectedRow.login_identifier)}>Copy Email</Button>
      </div>
      {message && <p className="text-sm text-muted-foreground">{message}</p>}
    </section>
  );
}

function stateColor(state: string) {
  if (state === "ok") return "text-emerald-500";
  if (state === "warn" || state === "unknown") return "text-amber-500";
  if (state === "cooldown") return "text-sky-500";
  return "text-destructive";
}
