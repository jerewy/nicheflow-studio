import { useCallback, useEffect, useState, type ReactNode } from "react";

import { DashboardTable } from "@/components/DashboardTable";
import { Button } from "@/components/ui/button";
import { bridge } from "@/lib/bridge";
import { formatDate } from "@/lib/format";
import type {
  CloudPublisherHealth,
  DashboardPublishJob,
  DashboardPublishQueue,
  PublishQueueJob,
  ScheduleCoverage,
  ScheduleCoverageAccount,
  ScheduleCoverageSlot,
} from "@/types";

// Remember the last account picked in the coverage panel so reopening the
// dashboard restores it instead of snapping back to the first account.
const SELECTED_ACCOUNT_STORAGE_KEY = "nicheflow.multiAccountPublish.selectedAccountId";

function readStoredAccountId(): number | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(SELECTED_ACCOUNT_STORAGE_KEY);
    if (raw === null) return null;
    const value = Number(raw);
    return Number.isInteger(value) ? value : null;
  } catch {
    return null;
  }
}

function writeStoredAccountId(accountId: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(SELECTED_ACCOUNT_STORAGE_KEY, String(accountId));
  } catch {
    // Storage can fail (private mode / quota); the selection still works in-session.
  }
}

// `datetime-local` inputs need a naive local `yyyy-MM-ddTHH:mm` value. The slot
// timestamps carry a timezone offset, so convert through the browser's local
// clock — the same convention the toolbar's "Set Schedule" field already uses.
function toDatetimeLocalValue(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const METRICS = [
  ["posted_views", "Views"],
  ["posted_likes", "Likes"],
  ["posted_comments", "Comments"],
  ["posted_shares", "Shares"],
] as const;

function formatBytes(bytes: number) {
  if (bytes >= 1_000_000_000) return `${(bytes / 1_000_000_000).toFixed(2)} GB`;
  if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(2)} MB`;
  return `${(bytes / 1_000).toFixed(2)} KB`;
}

function formatAge(minutes: number | null) {
  if (minutes === null) return "none";
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function CloudPublisherHealthPanel({
  health,
  error,
  loading,
  onRefresh,
}: {
  health: CloudPublisherHealth | null;
  error: string | null;
  loading: boolean;
  onRefresh: () => Promise<void>;
}) {
  const stale = health?.stale_jobs;
  const hasCritical =
    Boolean(stale && (stale.awaiting_upload || stale.processing || stale.processing_age_unknown)) ||
    Boolean(health && (health.usage_percent >= 80 || health.active_usage_percent >= 80));
  const hasWarning =
    !hasCritical &&
    Boolean(
      health &&
        (health.publish_mode !== "live" ||
          health.usage_percent >= 60 ||
          health.active_usage_percent >= 60 ||
          stale?.scheduled_past_due),
    );
  const label = hasCritical ? "Alert" : hasWarning ? "Attention" : "Healthy";
  const badgeClass = hasCritical
    ? "bg-red-500/15 text-red-500"
    : hasWarning
      ? "bg-amber-500/15 text-amber-500"
      : "bg-emerald-500/15 text-emerald-500";

  return (
    <div className="space-y-3 rounded-lg border border-border bg-background/50 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="font-semibold">Cloud Publisher Health</h3>
          <p className="text-xs text-muted-foreground">
            Read-only capacity and stuck-job safety checks.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {health && (
            <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${badgeClass}`}>
              {label}
            </span>
          )}
          <Button size="sm" variant="outline" disabled={loading} onClick={onRefresh}>
            {loading ? "Refreshing..." : "Refresh health"}
          </Button>
        </div>
      </div>
      {error && <p className="text-sm text-red-500">Cloud publisher health unavailable: {error}</p>}
      {health && stale && (
        <>
          <div className="grid gap-2 text-sm sm:grid-cols-2 xl:grid-cols-4">
            <div><span className="text-muted-foreground">Worker mode:</span> <strong>{health.publish_mode?.toUpperCase() ?? "UNKNOWN"}</strong></div>
            <div><span className="text-muted-foreground">Storage:</span> <strong>{formatBytes(health.stored_bytes)} / {formatBytes(health.max_stored_bytes)} ({health.usage_percent}%)</strong></div>
            <div><span className="text-muted-foreground">Active jobs:</span> <strong>{health.active_jobs} / {health.max_active_jobs} ({health.active_usage_percent}%)</strong></div>
            <div><span className="text-muted-foreground">Oldest active:</span> <strong>{formatAge(health.oldest_active_age_minutes)}</strong></div>
          </div>
          <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
            <span className={stale.awaiting_upload ? "font-medium text-red-500" : "text-muted-foreground"}>Stale uploads: {stale.awaiting_upload}</span>
            <span className={stale.processing || stale.processing_age_unknown ? "font-medium text-red-500" : "text-muted-foreground"}>Stalled processing: {stale.processing + stale.processing_age_unknown}</span>
            <span className={stale.scheduled_past_due ? "font-medium text-amber-500" : "text-muted-foreground"}>Past due: {stale.scheduled_past_due}</span>
          </div>
        </>
      )}
    </div>
  );
}

const SLOT_STATE_STYLE: Record<string, string> = {
  cloud: "border-indigo-500/40 bg-indigo-500/10 text-indigo-400",
  scheduled: "border-fuchsia-500/40 bg-fuchsia-500/10 text-fuchsia-400",
  posted: "border-emerald-500/40 bg-emerald-500/10 text-emerald-400",
  failed: "border-red-500/40 bg-red-500/10 text-red-400",
  missed: "border-red-500/30 bg-red-500/5 text-red-400",
  open: "border-amber-500/30 bg-amber-500/5 text-amber-400",
};

function slotStateLabel(slot: ScheduleCoverageSlot) {
  if (slot.state === "cloud") return "Cloud";
  if (slot.state === "scheduled") return "Local";
  if (slot.state === "posted") return "Posted";
  if (slot.state === "failed") return "Failed";
  if (slot.state === "missed") return "Missed";
  return "Open";
}

function slotStatusLabel(slot: ScheduleCoverageSlot) {
  const state = slotStateLabel(slot);
  return slot.timing === "late" ? `${state} · Late` : state;
}

function SlotActionsPopover({
  slot,
  fillableJobs,
  onFill,
  onChangeTime,
  onRemove,
  onOpenVideo,
  onEditInProcessing,
  onClose,
}: {
  slot: ScheduleCoverageSlot;
  fillableJobs: DashboardPublishJob[];
  onFill: (jobId: number, slotAt: string) => void;
  onChangeTime: (jobId: number, iso: string) => void;
  onRemove: (jobId: number) => void;
  onOpenVideo: (jobId: number) => void;
  onEditInProcessing?: (itemId: number | null, search: string) => void;
  onClose: () => void;
}) {
  const [fillJobId, setFillJobId] = useState("");
  const [timeValue, setTimeValue] = useState(() =>
    toDatetimeLocalValue(slot.scheduled_at ?? slot.slot_at),
  );
  const jobId = slot.job_id;

  // Deep-link to the Processing screen with this reel's title pre-searched, so a
  // scheduled clip can be re-edited and re-exported without manually hunting for it.
  const editInProcessingButton =
    onEditInProcessing && jobId !== null ? (
      <Button
        size="sm"
        variant="outline"
        className="w-full"
        onClick={() => {
          onEditInProcessing(slot.item_id, slot.job_title ?? "");
          onClose();
        }}
      >
        Edit in Processing
      </Button>
    ) : null;

  let body: ReactNode;
  if (jobId === null) {
    // Open or missed slot: assign one of this account's unscheduled reels.
    body = fillableJobs.length ? (
      <>
        <p className="text-xs font-medium text-muted-foreground">Fill {slot.slot} slot</p>
        <select
          className="h-8 w-full rounded-md border border-input bg-background px-2 text-xs text-foreground"
          value={fillJobId}
          onChange={(event) => setFillJobId(event.target.value)}
        >
          <option value="">Select a reel…</option>
          {fillableJobs.map((job) => (
            <option key={job.id} value={job.id}>
              {job.title ?? job.video}
            </option>
          ))}
        </select>
        {slot.state === "missed" && (
          <p className="text-xs text-amber-500">
            Slot time has passed; it will post as soon as it is due.
          </p>
        )}
        <Button
          size="sm"
          className="w-full"
          disabled={!fillJobId}
          onClick={() => {
            onFill(Number(fillJobId), slot.slot_at);
            onClose();
          }}
        >
          Assign to {slot.slot}
        </Button>
      </>
    ) : (
      <p className="text-xs text-muted-foreground">
        No unscheduled reels available for this account.
      </p>
    );
  } else if (slot.state === "posted") {
    body = (
      <>
        <p className="truncate text-xs font-medium" title={slot.job_title ?? undefined}>
          {slot.job_title ?? `Job ${jobId}`}
        </p>
        <p className="text-xs text-muted-foreground">Already posted — schedule is locked.</p>
        <Button
          size="sm"
          variant="ghost"
          className="w-full"
          onClick={() => {
            onOpenVideo(jobId);
            onClose();
          }}
        >
          Open video
        </Button>
        {editInProcessingButton}
      </>
    );
  } else {
    // Cloud / local / failed slot: retime, remove, or open the reel.
    body = (
      <>
        <p className="truncate text-xs font-medium" title={slot.job_title ?? undefined}>
          {slot.job_title ?? `Job ${jobId}`}
        </p>
        <label className="grid gap-1 text-xs text-muted-foreground">
          Change time
          <input
            type="datetime-local"
            className="h-8 w-full rounded-md border border-input bg-background px-2 text-xs text-foreground"
            value={timeValue}
            onChange={(event) => setTimeValue(event.target.value)}
          />
        </label>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            className="flex-1"
            disabled={!timeValue}
            onClick={() => {
              onChangeTime(jobId, new Date(timeValue).toISOString());
              onClose();
            }}
          >
            Save time
          </Button>
          <Button
            size="sm"
            variant="destructive"
            className="flex-1"
            onClick={() => {
              onRemove(jobId);
              onClose();
            }}
          >
            Remove
          </Button>
        </div>
        <Button
          size="sm"
          variant="ghost"
          className="w-full"
          onClick={() => {
            onOpenVideo(jobId);
            onClose();
          }}
        >
          Open video
        </Button>
        {editInProcessingButton}
      </>
    );
  }

  return (
    <>
      <button
        type="button"
        aria-hidden
        tabIndex={-1}
        className="fixed inset-0 z-10 cursor-default"
        onClick={onClose}
      />
      <div
        role="dialog"
        className="absolute left-0 top-full z-20 mt-1 w-64 space-y-2 rounded-md border border-border bg-background p-3 text-left text-foreground shadow-lg"
      >
        {body}
      </div>
    </>
  );
}

function ScheduleCoveragePanel({
  coverage,
  selectedAccountId,
  fillableJobs,
  onSelectAccount,
  onFillSlot,
  onChangeSlotTime,
  onRemoveSlot,
  onOpenVideo,
  onEditInProcessing,
}: {
  coverage: ScheduleCoverage | null;
  selectedAccountId: number | null;
  fillableJobs: DashboardPublishJob[];
  onSelectAccount: (accountId: number) => void;
  onFillSlot: (jobId: number, slotAt: string) => void;
  onChangeSlotTime: (jobId: number, iso: string) => void;
  onRemoveSlot: (jobId: number) => void;
  onOpenVideo: (jobId: number) => void;
  onEditInProcessing?: (accountId: number, itemId: number | null, search: string) => void;
}) {
  const [openSlot, setOpenSlot] = useState<string | null>(null);
  // Switching accounts swaps the whole grid; close any stale popover. Resetting
  // during render (vs. an effect) avoids a cascading re-render.
  const [lastAccountId, setLastAccountId] = useState(selectedAccountId);
  if (selectedAccountId !== lastAccountId) {
    setLastAccountId(selectedAccountId);
    setOpenSlot(null);
  }
  const account =
    coverage?.accounts.find((candidate) => candidate.account_id === selectedAccountId) ??
    coverage?.accounts[0] ??
    null;
  if (!coverage?.accounts.length || !account) {
    return (
      <div className="rounded-lg border border-border bg-background/50 p-4 text-sm text-muted-foreground">
        No account schedule slots configured yet.
      </div>
    );
  }
  const complete = account.total > 0 && account.filled === account.total;

  return (
    <div className="space-y-4 rounded-lg border border-border bg-background/50 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h3 className="font-semibold">Account Schedule Coverage</h3>
          <p className="text-xs text-muted-foreground">
            Configured slots for today and tomorrow. Off-slot jobs remain in the queue below.
          </p>
        </div>
        <label className="grid gap-1 text-xs text-muted-foreground">
          Account
          <select
            className="h-9 min-w-60 rounded-md border border-input bg-background px-3 text-sm text-foreground"
            value={account.account_id}
            onChange={(event) => onSelectAccount(Number(event.target.value))}
          >
            {coverage.accounts.map((candidate) => (
              <option key={candidate.account_id} value={candidate.account_id}>
                {candidate.account_name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
        <span>
          <strong>{account.filled} / {account.total}</strong>{" "}
          <span className="text-muted-foreground">slots filled</span>
        </span>
        <span className={complete ? "font-medium text-emerald-500" : "font-medium text-amber-500"}>
          {complete ? "Two-day queue complete" : `${account.total - account.filled} slot(s) need reels`}
        </span>
        <span className="text-muted-foreground">
          Target {account.daily_target}/day · {account.timezone} · Auto-schedule{" "}
          {account.auto_schedule_on_export ? "on" : "off"}
        </span>
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        {account.days.map((day) => (
          <div key={day.date} className="space-y-3 rounded-md border border-border p-3">
            <div className="flex items-center justify-between gap-2">
              <strong>{day.is_today ? "Today" : "Tomorrow"} · {day.date}</strong>
              <span className="text-xs text-muted-foreground">{day.filled}/{day.total} filled</span>
            </div>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
              {day.slots.map((slot) => (
                <div key={slot.slot_at} className="relative">
                  <button
                    type="button"
                    onClick={() =>
                      setOpenSlot((current) =>
                        current === slot.slot_at ? null : slot.slot_at,
                      )
                    }
                    className={`w-full rounded-md border p-2 text-left transition hover:ring-2 hover:ring-ring/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${SLOT_STATE_STYLE[slot.state] ?? SLOT_STATE_STYLE.open}`}
                    title={slot.job_title ?? `${slot.slot} slot is ${slotStateLabel(slot).toLowerCase()}`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <strong>{slot.slot}</strong>
                      <span className="text-xs font-medium">{slotStatusLabel(slot)}</span>
                    </div>
                    <p className="mt-1 truncate text-xs opacity-80">
                      {slot.scheduled_at
                        ? `${new Date(slot.scheduled_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} · ${slot.job_title ?? `Job ${slot.job_id}`}`
                        : "No matching job"}
                    </p>
                  </button>
                  {openSlot === slot.slot_at && (
                    <SlotActionsPopover
                      slot={slot}
                      fillableJobs={fillableJobs}
                      onFill={onFillSlot}
                      onChangeTime={onChangeSlotTime}
                      onRemove={onRemoveSlot}
                      onOpenVideo={onOpenVideo}
                      onEditInProcessing={
                        onEditInProcessing
                          ? (itemId, search) =>
                              onEditInProcessing(account.account_id, itemId, search)
                          : undefined
                      }
                      onClose={() => setOpenSlot(null)}
                    />
                  )}
                </div>
              ))}
            </div>
            {!day.slots.length && (
              <p className="text-xs text-muted-foreground">No schedule slots configured.</p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function MetricsEditor({
  job,
  onSaved,
}: {
  job: PublishQueueJob;
  onSaved: () => Promise<void>;
}) {
  const [values, setValues] = useState(() =>
    Object.fromEntries(
      METRICS.map(([field]) => [field, job[field] === null ? "" : String(job[field])]),
    ),
  );
  const [message, setMessage] = useState<string | null>(null);

  const save = async () => {
    if (Object.values(values).some((value) => value !== "" && !/^\d+$/.test(value))) {
      setMessage("Metrics must be non-negative integers.");
      return;
    }
    try {
      await bridge.updateJobMetrics(
        job.id,
        Object.fromEntries(
          Object.entries(values).map(([field, value]) => [
            field,
            value === "" ? null : Number(value),
          ]),
        ),
      );
      setMessage("Saved.");
      await onSaved();
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="flex min-w-[34rem] flex-wrap items-end gap-2">
      {METRICS.map(([field, label]) => (
        <label key={field} className="grid gap-1 text-xs text-muted-foreground">
          {label}
          <input
            type="number"
            min="0"
            step="1"
            className="h-8 w-24 rounded-md border border-input bg-background px-2 text-foreground"
            value={values[field]}
            onChange={(event) =>
              setValues((current) => ({ ...current, [field]: event.target.value }))
            }
          />
        </label>
      ))}
      <Button size="sm" variant="outline" onClick={save}>Save</Button>
      {message && <span className="text-xs text-muted-foreground">{message}</span>}
    </div>
  );
}

export function MultiAccountPublish({
  onOpenInProcessing,
}: {
  onOpenInProcessing?: (accountId: number, itemId: number | null, search: string) => void;
} = {}) {
  const [queue, setQueue] = useState<DashboardPublishQueue | null>(null);
  const [postedJobs, setPostedJobs] = useState<PublishQueueJob[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [schedule, setSchedule] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [health, setHealth] = useState<CloudPublisherHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [healthLoading, setHealthLoading] = useState(false);
  const [coverage, setCoverage] = useState<ScheduleCoverage | null>(null);
  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(() =>
    readStoredAccountId(),
  );

  const syncCloudJobs = useCallback(async () => {
    try {
      await bridge.syncCloudPublishJobs();
    } catch {
      // The dashboard can still render local state when the cloud worker is unreachable.
    }
  }, []);

  const loadHealth = useCallback(async () => {
    setHealthLoading(true);
    try {
      setHealth(await bridge.cloudPublisherHealth());
      setHealthError(null);
    } catch (err: unknown) {
      setHealthError(err instanceof Error ? err.message : String(err));
    } finally {
      setHealthLoading(false);
    }
  }, []);

  const load = useCallback(async () => {
    try {
      await syncCloudJobs();
      const [dashboardQueue, publishQueue] = await Promise.all([
        bridge.dashboardPublishJobs(),
        bridge.listPublishQueue(),
      ]);
      setQueue(dashboardQueue);
      setPostedJobs(
        publishQueue.filter((job) => job.status === "posted" || job.posted_at !== null),
      );
      setMessage(null);
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    }
  }, [syncCloudJobs]);
  const loadCoverage = useCallback(async () => {
    try {
      await syncCloudJobs();
      const result = await bridge.dashboardScheduleCoverage();
      setCoverage(result);
      setSelectedAccountId((current) =>
        current !== null && result.accounts.some((account) => account.account_id === current)
          ? current
          : result.accounts[0]?.account_id ?? null,
      );
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    }
  }, [syncCloudJobs]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load();
      void loadHealth();
      void loadCoverage();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [load, loadCoverage, loadHealth]);

  const run = async (action: () => Promise<unknown>, success: string) => {
    try {
      await action();
      setMessage(success);
      await Promise.all([load(), loadCoverage()]);
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };
  const toggle = (id: number) =>
    setSelected((ids) =>
      ids.includes(id) ? ids.filter((value) => value !== id) : [...ids, id],
    );
  const selectedCoverageAccount: ScheduleCoverageAccount | null =
    coverage?.accounts.find((account) => account.account_id === selectedAccountId) ?? null;
  const visibleJobs =
    selectedCoverageAccount && queue
      ? queue.jobs.filter((job) => job.account_name === selectedCoverageAccount.account_name)
      : queue?.jobs ?? [];
  // Reels for the selected account that hold no slot yet — the pool a slot can be filled from.
  const fillableJobs: DashboardPublishJob[] =
    selectedCoverageAccount && queue
      ? queue.jobs.filter(
          (job) =>
            job.account_name === selectedCoverageAccount.account_name &&
            job.scheduled_at === null,
        )
      : [];

  return (
    <section className="space-y-4 rounded-xl border bg-card p-5">
      <div>
        <h2 className="text-lg font-semibold">Multi-Account Publish</h2>
        <p className="text-sm text-muted-foreground">
          Review and prepare recent exported reels across all accounts.
        </p>
      </div>
      <CloudPublisherHealthPanel
        health={health}
        error={healthError}
        loading={healthLoading}
        onRefresh={loadHealth}
      />
      <ScheduleCoveragePanel
        coverage={coverage}
        selectedAccountId={selectedAccountId}
        fillableJobs={fillableJobs}
        onSelectAccount={(accountId) => {
          setSelectedAccountId(accountId);
          writeStoredAccountId(accountId);
          setSelected([]);
        }}
        onFillSlot={(jobId, slotAt) =>
          run(() => bridge.rescheduleJob(jobId, slotAt), "Slot filled.")
        }
        onChangeSlotTime={(jobId, iso) =>
          run(() => bridge.rescheduleJob(jobId, iso), "Slot time updated.")
        }
        onRemoveSlot={(jobId) =>
          run(() => bridge.unscheduleJob(jobId), "Removed from schedule.")
        }
        onOpenVideo={(jobId) =>
          run(() => bridge.dashboardOpenOutput(jobId), "Opened reel output.")
        }
        onEditInProcessing={onOpenInProcessing}
      />
      {queue && (
        <p className="text-sm text-muted-foreground">
          {visibleJobs.length} item(s) shown
          {selectedCoverageAccount ? ` for ${selectedCoverageAccount.account_name}` : ""}.{" "}
          {queue.jobs.length} across all accounts
          {queue.failed > 0 ? (
            <span className="font-medium text-red-500">, {queue.failed} failed</span>
          ) : null}
          .
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="secondary" onClick={() => void Promise.all([load(), loadCoverage()])}>Refresh</Button>
        <Button size="sm" disabled={!selected.length} onClick={() => run(() => bridge.dashboardMarkReady(selected), "Selected reels marked ready.")}>Mark Selected Ready</Button>
        <input type="datetime-local" className="h-8 rounded-md border border-input px-2 text-xs" value={schedule} onChange={(event) => setSchedule(event.target.value)} />
        <Button size="sm" variant="outline" disabled={!selected.length || !schedule} onClick={() => run(() => Promise.all(selected.map((id) => bridge.rescheduleJob(id, new Date(schedule).toISOString()))), "Schedule updated.")}>Set Schedule</Button>
        <Button size="sm" variant="outline" disabled={!selected.length} onClick={() => run(() => Promise.all(selected.map((id) => bridge.unscheduleJob(id))), "Schedule cleared.")}>Clear Schedule</Button>
        <Button size="sm" variant="outline" disabled={selected.length !== 1} onClick={() => run(() => bridge.dashboardOpenOutput(selected[0]), "Opened reel output.")}>Open Video</Button>
      </div>
      {message && <p className="text-sm text-muted-foreground">{message}</p>}
      <DashboardTable headers={["", "Account", "Video", "Title", "Status", "Scheduled", "Profile", "Output"]}>
        {visibleJobs.map((job) => (
          <tr key={job.id} className="border-t border-border">
            <td className="px-3 py-2"><input type="checkbox" checked={selected.includes(job.id)} onChange={() => toggle(job.id)} /></td>
            <td className="whitespace-nowrap px-3 py-2">{job.account_name}</td>
            <td className="max-w-56 truncate px-3 py-2">{job.video}</td>
            <td className="max-w-md truncate px-3 py-2">{job.title ?? "-"}</td>
            <td className="px-3 py-2">
              {job.status === "failed" ? (
                <span className="flex items-center gap-2">
                  <span
                    className="font-medium text-red-500"
                    title={job.error_message ?? "Publish failed."}
                  >
                    Failed
                  </span>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-6 px-2 text-xs"
                    title={`Reschedule as due now${job.error_message ? ` (failed: ${job.error_message})` : ""}`}
                    onClick={() =>
                      run(
                        () =>
                          bridge.rescheduleJob(
                            job.id,
                            new Date().toISOString().replace("Z", "+00:00"),
                          ),
                        "Failed reel rescheduled — due now. Auto-publish or “Publish due now” will post it.",
                      )
                    }
                  >
                    Republish
                  </Button>
                </span>
              ) : job.is_due ? (
                "Due now"
              ) : job.status === "draft" ? (
                "Exported"
              ) : (
                job.status
              )}
            </td>
            <td className="whitespace-nowrap px-3 py-2">{formatDate(job.scheduled_at)}</td>
            <td className="px-3 py-2">{job.profile ?? "-"}</td>
            <td className="px-3 py-2">{job.output_name}</td>
          </tr>
        ))}
      </DashboardTable>
      <div className="space-y-2 border-t pt-4">
        <div>
          <h3 className="font-semibold">Posted metrics</h3>
          <p className="text-xs text-muted-foreground">
            Paste the latest Instagram Insights values for posted jobs.
          </p>
        </div>
        <DashboardTable headers={["Account", "Title", "Posted", "Metrics"]}>
          {postedJobs.map((job) => (
            <tr key={job.id} className="border-t border-border">
              <td className="whitespace-nowrap px-3 py-2">{job.account_name ?? "Unassigned"}</td>
              <td className="max-w-xs truncate px-3 py-2">{job.title ?? "-"}</td>
              <td className="whitespace-nowrap px-3 py-2">{formatDate(job.posted_at)}</td>
              <td className="px-3 py-2">
                <MetricsEditor
                  key={`${job.id}-${job.posted_views}-${job.posted_likes}-${job.posted_comments}-${job.posted_shares}`}
                  job={job}
                  onSaved={load}
                />
              </td>
            </tr>
          ))}
        </DashboardTable>
        {!postedJobs.length && (
          <p className="text-sm text-muted-foreground">
            No posted jobs available for metrics entry.
          </p>
        )}
      </div>
    </section>
  );
}
