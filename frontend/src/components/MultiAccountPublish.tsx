import { useCallback, useEffect, useState } from "react";

import { DashboardTable } from "@/components/DashboardTable";
import { Button } from "@/components/ui/button";
import { formatDate } from "@/lib/format";
import { bridge } from "@/lib/bridge";
import type { DashboardPublishQueue } from "@/types";

export function MultiAccountPublish() {
  const [queue, setQueue] = useState<DashboardPublishQueue | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [schedule, setSchedule] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setQueue(await bridge.dashboardPublishJobs());
      setMessage(null);
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    }
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(load, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const run = async (action: () => Promise<unknown>, success: string) => {
    try {
      await action();
      setMessage(success);
      await load();
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };
  const toggle = (id: number) => setSelected((ids) => ids.includes(id) ? ids.filter((value) => value !== id) : [...ids, id]);

  return (
    <section className="space-y-4 rounded-xl border bg-card p-5">
      <div>
        <h2 className="text-lg font-semibold">Multi-Account Publish</h2>
        <p className="text-sm text-muted-foreground">Review and prepare recent exported reels across all accounts.</p>
      </div>
      {queue && <p className="text-sm text-muted-foreground">{queue.jobs.length} recent exported/due item(s). {queue.draft} exported, {queue.ready} ready, {queue.scheduled} scheduled, {queue.due_count} due now.</p>}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="secondary" onClick={load}>Refresh</Button>
        <Button size="sm" disabled={!selected.length} onClick={() => run(() => bridge.dashboardMarkReady(selected), "Selected reels marked ready.")}>Mark Selected Ready</Button>
        <input type="datetime-local" className="h-8 rounded-md border border-input px-2 text-xs" value={schedule} onChange={(e) => setSchedule(e.target.value)} />
        <Button size="sm" variant="outline" disabled={!selected.length || !schedule} onClick={() => run(() => Promise.all(selected.map((id) => bridge.rescheduleJob(id, new Date(schedule).toISOString()))), "Schedule updated.")}>Set Schedule</Button>
        <Button size="sm" variant="outline" disabled={!selected.length} onClick={() => run(() => Promise.all(selected.map((id) => bridge.unscheduleJob(id))), "Schedule cleared.")}>Clear Schedule</Button>
        <Button size="sm" variant="outline" disabled={selected.length !== 1} onClick={() => run(() => bridge.dashboardOpenOutput(selected[0]), "Opened reel output.")}>Open Video</Button>
      </div>
      {message && <p className="text-sm text-muted-foreground">{message}</p>}
      <DashboardTable headers={["", "Account", "Video", "Title", "Status", "Scheduled", "Profile", "Output"]}>
        {queue?.jobs.map((job) => (
          <tr key={job.id} className="border-t border-border">
            <td className="px-3 py-2"><input type="checkbox" checked={selected.includes(job.id)} onChange={() => toggle(job.id)} /></td>
            <td className="whitespace-nowrap px-3 py-2">{job.account_name}</td>
            <td className="max-w-56 truncate px-3 py-2">{job.video}</td>
            <td className="max-w-md truncate px-3 py-2">{job.title ?? "—"}</td>
            <td className="px-3 py-2">{job.is_due ? "Due now" : job.status === "draft" ? "Exported" : job.status}</td>
            <td className="whitespace-nowrap px-3 py-2">{formatDate(job.scheduled_at)}</td>
            <td className="px-3 py-2">{job.profile ?? "—"}</td>
            <td className="px-3 py-2">{job.output_name}</td>
          </tr>
        ))}
      </DashboardTable>
      <p className="text-xs text-muted-foreground">Live “Publish All Due” remains in PyQt until its sequential Playwright runner is moved out of the Qt window.</p>
    </section>
  );
}
