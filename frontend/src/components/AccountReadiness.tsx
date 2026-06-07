import { useCallback, useEffect, useState } from "react";

import { DashboardTable } from "@/components/DashboardTable";
import { Button } from "@/components/ui/button";
import { formatDate } from "@/lib/format";
import { bridge } from "@/lib/bridge";
import type { AccountReadiness as Readiness, JobSnapshot } from "@/types";

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

  const selectedRow = data?.rows.find((row) => row.account_id === selected);
  const liveCheck = async () => {
    try {
      const { job_id } = await bridge.dashboardStartLiveHealthCheck();
      setMessage("Checking sessions live...");
      while (true) {
        const job: JobSnapshot = await bridge.getJob(job_id);
        setMessage(job.message || "Checking sessions live...");
        if (job.status === "succeeded") {
          const result = job.result as { results?: { account_name: string; state: string; label: string; detail: string }[] } | null;
          if (result?.results) {
            setData((current) => current && ({
              ...current,
              rows: current.rows.map((row) => {
                const live = result.results?.find((item) => item.account_name === row.account_name);
                return live ? { ...row, session_state: live.state, session_label: live.label, detail: live.detail } : row;
              }),
            }));
          }
          break;
        }
        if (job.status === "failed") throw new Error(job.error ?? "Live health check failed.");
        await new Promise((resolve) => window.setTimeout(resolve, 500));
      }
      setMessage("Live health check complete.");
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };

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
        <p className="text-sm text-muted-foreground">Per-account session health and publish readiness. Local refresh is safe; live checks contact Instagram sequentially.</p>
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
        <Button size="sm" variant="outline" onClick={liveCheck}>Check All (live)</Button>
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
