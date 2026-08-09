import { useCallback, useEffect, useState, type ReactNode } from "react";

import { DashboardTable } from "@/components/DashboardTable";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAutoRefresh } from "@/hooks/useAutoRefresh";
import { bridge } from "@/lib/bridge";
import { formatDate } from "@/lib/format";
import type {
  CloudPublisherHealth,
  CloudWorkerAccount,
  CloudWorkerJob,
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

// Sentinel for the coverage picker's "All accounts" overview. Negative so it can
// never collide with a real Account.id, and still round-trips through the
// integer-only localStorage helpers below.
const ALL_ACCOUNTS_ID = -1;

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

const CLOUD_JOB_ACTIVE_STATUSES = new Set(["awaiting_upload", "scheduled", "processing"]);

// Pending / past-due jobs first, then everything else, both oldest-scheduled first.
function sortCloudJobs(jobs: CloudWorkerJob[]): CloudWorkerJob[] {
  const now = Date.now();
  return [...jobs].sort((a, b) => {
    const aActive = CLOUD_JOB_ACTIVE_STATUSES.has(a.status);
    const bActive = CLOUD_JOB_ACTIVE_STATUSES.has(b.status);
    const aPastDue = aActive && new Date(a.scheduled_at).getTime() <= now;
    const bPastDue = bActive && new Date(b.scheduled_at).getTime() <= now;
    if (aPastDue !== bPastDue) return aPastDue ? -1 : 1;
    if (aActive !== bActive) return aActive ? -1 : 1;
    return new Date(a.scheduled_at).getTime() - new Date(b.scheduled_at).getTime();
  });
}

function AccountSettingsRow({
  account,
  slotsPerDay,
  onSave,
}: {
  account: CloudWorkerAccount;
  slotsPerDay: number | null;
  onSave: (payload: { dailyLimit: number; minGapMinutes: number; enabled: boolean }) => Promise<void>;
}) {
  const [dailyLimit, setDailyLimit] = useState(String(account.daily_limit));
  const [minGapMinutes, setMinGapMinutes] = useState(String(account.min_gap_minutes));
  const [enabled, setEnabled] = useState(account.enabled);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const noHeadroom =
    slotsPerDay !== null && Number.isFinite(Number(dailyLimit)) && Number(dailyLimit) <= slotsPerDay;

  const save = async () => {
    setBusy(true);
    setMessage(null);
    try {
      await onSave({
        dailyLimit: Number(dailyLimit),
        minGapMinutes: Number(minGapMinutes),
        enabled,
      });
      setMessage("Saved.");
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <tr className="border-t border-border align-top">
      <td className="whitespace-nowrap px-3 py-2 font-medium">{account.account_key}</td>
      <td className="px-3 py-2">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
          />
          <span className="text-xs text-muted-foreground">enabled</span>
        </label>
      </td>
      <td className="px-3 py-2">
        <Input
          type="number"
          min={1}
          max={20}
          className="h-8 w-20"
          value={dailyLimit}
          onChange={(event) => setDailyLimit(event.target.value)}
        />
        {noHeadroom && (
          <p className="mt-1 max-w-40 text-xs font-medium text-amber-500">
            No headroom — delays become permanent.
          </p>
        )}
      </td>
      <td className="px-3 py-2">
        <Input
          type="number"
          min={30}
          className="h-8 w-24"
          value={minGapMinutes}
          onChange={(event) => setMinGapMinutes(event.target.value)}
        />
      </td>
      <td className="px-3 py-2">
        <Button size="sm" variant="outline" disabled={busy} onClick={save}>
          Save
        </Button>
        {message && <p className="mt-1 text-xs text-muted-foreground">{message}</p>}
      </td>
    </tr>
  );
}

function CloudPublisherControlPanel({
  jobs,
  jobsError,
  accounts,
  accountsError,
  loading,
  slotsPerDayByKey,
  onRefresh,
  onRunDue,
  onForcePublish,
  onCancel,
  onSaveAccountSettings,
}: {
  jobs: CloudWorkerJob[];
  jobsError: string | null;
  accounts: CloudWorkerAccount[];
  accountsError: string | null;
  loading: boolean;
  slotsPerDayByKey: Map<string, number>;
  onRefresh: () => Promise<void>;
  onRunDue: () => Promise<void>;
  onForcePublish: (job: CloudWorkerJob) => Promise<void>;
  onCancel: (job: CloudWorkerJob) => Promise<void>;
  onSaveAccountSettings: (
    accountKey: string,
    payload: { dailyLimit: number; minGapMinutes: number; enabled: boolean },
  ) => Promise<void>;
}) {
  const sorted = sortCloudJobs(jobs);

  return (
    <div className="space-y-4 rounded-lg border border-border bg-background/50 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="font-semibold">Cloud Publisher Control</h3>
          <p className="text-xs text-muted-foreground">
            Worker-side job queue and account safety caps — direct control over the Cloudflare
            publisher, separate from local scheduling.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={loading}
            onClick={() =>
              void (async () => {
                if (
                  window.confirm(
                    "Ask the Worker to process all due jobs right now (bypasses waiting for the next cron tick)?",
                  )
                ) {
                  await onRunDue();
                }
              })()
            }
          >
            Run due now
          </Button>
          <Button size="sm" variant="outline" disabled={loading} onClick={onRefresh}>
            {loading ? "Refreshing..." : "Refresh"}
          </Button>
        </div>
      </div>

      {jobsError && <p className="text-sm text-red-500">Job queue unavailable: {jobsError}</p>}
      <DashboardTable headers={["Account", "Slot (local)", "Worker status", "Attempts", "Gate / error", "Actions"]}>
        {sorted.map((job) => (
          <tr key={job.id} className="border-t border-border">
            <td className="whitespace-nowrap px-3 py-2">{job.account_name ?? job.account_key}</td>
            <td className="whitespace-nowrap px-3 py-2">{formatDate(job.scheduled_at)}</td>
            <td className="px-3 py-2">{job.status}</td>
            <td className="px-3 py-2">{job.attempts}</td>
            <td className="max-w-xs truncate px-3 py-2" title={job.error_message ?? undefined}>
              {job.error_message ?? "-"}
            </td>
            <td className="whitespace-nowrap px-3 py-2">
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7 px-2 text-xs"
                  disabled={job.status !== "scheduled"}
                  onClick={() => {
                    if (
                      window.confirm(
                        "This bypasses the account's daily-limit/cooldown safety check and posts " +
                          "immediately. It increases automation-flag risk. Continue?",
                      )
                    ) {
                      void onForcePublish(job);
                    }
                  }}
                >
                  Force publish
                </Button>
                <Button
                  size="sm"
                  variant="destructive"
                  className="h-7 px-2 text-xs"
                  disabled={["published", "validated", "canceled"].includes(job.status)}
                  onClick={() => {
                    if (window.confirm("Cancel this job and delete its queued media?")) {
                      void onCancel(job);
                    }
                  }}
                >
                  Cancel
                </Button>
              </div>
            </td>
          </tr>
        ))}
      </DashboardTable>
      {!sorted.length && !jobsError && (
        <p className="text-sm text-muted-foreground">No jobs on the Worker queue.</p>
      )}

      {accountsError && (
        <p className="text-sm text-red-500">Worker accounts unavailable: {accountsError}</p>
      )}
      <DashboardTable headers={["Account", "Enabled", "Daily limit", "Min gap (min)", ""]}>
        {accounts.map((account) => (
          <AccountSettingsRow
            key={account.account_key}
            account={account}
            slotsPerDay={slotsPerDayByKey.get(account.account_key) ?? null}
            onSave={(payload) => onSaveAccountSettings(account.account_key, payload)}
          />
        ))}
      </DashboardTable>
      {!accounts.length && !accountsError && (
        <p className="text-sm text-muted-foreground">No accounts registered on the Worker yet.</p>
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

// Short "Cloud · <reason>" hint for a gated/processing cloud slot; the full
// Worker message stays available via the `title` attribute (see slotGateTitle).
function slotGateHint(slot: ScheduleCoverageSlot): string | null {
  if (slot.state !== "cloud") return null;
  const status = (slot.cloud_status ?? "").toLowerCase();
  const error = (slot.cloud_error ?? slot.note ?? "").toLowerCase();
  if (status === "processing") return "Cloud · processing";
  if (error.includes("daily limit")) return "Cloud · gated: daily limit";
  if (error.includes("cooldown")) return "Cloud · gated: cooldown";
  if (error.includes("disabled")) return "Cloud · gated: account disabled";
  if (error) return "Cloud · gated";
  if (status === "scheduled" || status === "validated") return "Cloud · queued";
  return null;
}

function slotGateTitle(slot: ScheduleCoverageSlot): string | undefined {
  return slot.cloud_error ?? slot.note ?? undefined;
}

function SlotActionsPopover({
  slot,
  fillableJobs,
  onFill,
  onChangeTime,
  onRemove,
  onOpenVideo,
  onEditInProcessing,
  onForcePublish,
  onClose,
}: {
  slot: ScheduleCoverageSlot;
  fillableJobs: DashboardPublishJob[];
  onFill: (jobId: number, slotAt: string) => void;
  onChangeTime: (jobId: number, iso: string) => void;
  onRemove: (jobId: number) => void;
  onOpenVideo: (jobId: number) => void;
  onEditInProcessing?: (itemId: number | null, search: string) => void;
  onForcePublish: (jobId: number) => void;
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
        {slot.state === "cloud" && slot.note && (
          <p className="text-xs text-amber-500">{slot.note}</p>
        )}
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
        {slot.state === "cloud" && (
          <Button
            size="sm"
            variant="outline"
            className="w-full border-amber-500/50 text-amber-500 hover:bg-amber-500/10"
            onClick={() => {
              if (
                window.confirm(
                  "This bypasses the account's daily-limit/cooldown safety check and " +
                    "posts immediately. It increases automation-flag risk. Continue?",
                )
              ) {
                onForcePublish(jobId);
              }
              onClose();
            }}
          >
            Force publish now
          </Button>
        )}
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

function slotPillTitle(slot: ScheduleCoverageSlot): string {
  return (
    slotGateTitle(slot) ??
    slot.job_title ??
    `${slot.slot} slot is ${slotStateLabel(slot).toLowerCase()}`
  );
}

// Read-only cross-account roll-up: every account's two-day slot strip at once,
// so a gap anywhere in the network is visible without cycling the picker. Slot
// editing stays in the per-account grid, which is one click away via the name.
function AllAccountsCoverage({
  accounts,
  onSelectAccount,
}: {
  accounts: ScheduleCoverageAccount[];
  onSelectAccount: (accountId: number) => void;
}) {
  const filled = accounts.reduce((sum, account) => sum + account.filled, 0);
  const total = accounts.reduce((sum, account) => sum + account.total, 0);
  const covered = accounts.filter(
    (account) => account.total > 0 && account.filled === account.total,
  ).length;
  const unconfigured = accounts.filter((account) => account.total === 0).length;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
        <span>
          <strong>{filled} / {total}</strong>{" "}
          <span className="text-muted-foreground">slots filled network-wide</span>
        </span>
        <span
          className={
            covered === accounts.length
              ? "font-medium text-emerald-500"
              : "font-medium text-amber-500"
          }
        >
          {covered} / {accounts.length} account(s) fully covered
        </span>
        {unconfigured > 0 && (
          <span className="text-muted-foreground">
            {unconfigured} account(s) have no slots configured
          </span>
        )}
      </div>
      <div className="space-y-2">
        {accounts.map((account) => {
          const complete = account.total > 0 && account.filled === account.total;
          return (
            <div
              key={account.account_id}
              className="grid gap-2 rounded-md border border-border p-3 lg:grid-cols-[minmax(11rem,15rem)_1fr] lg:items-start"
            >
              <div className="space-y-0.5">
                <button
                  type="button"
                  onClick={() => onSelectAccount(account.account_id)}
                  className="text-left font-medium underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  title="Open this account's slot grid"
                >
                  {account.account_name}
                </button>
                <p className="text-xs">
                  <span
                    className={
                      complete ? "font-medium text-emerald-500" : "font-medium text-amber-500"
                    }
                  >
                    {account.filled}/{account.total} filled
                  </span>
                  <span className="text-muted-foreground">
                    {" "}
                    · {account.daily_target}/day · {account.timezone}
                  </span>
                </p>
              </div>
              <div className="space-y-1">
                {account.days.map((day) => (
                  <div key={day.date} className="flex flex-wrap items-center gap-1">
                    <span className="w-20 shrink-0 text-xs text-muted-foreground">
                      {day.is_today ? "Today" : "Tomorrow"}
                    </span>
                    {day.slots.map((slot) => (
                      <span
                        key={slot.slot_at}
                        className={`rounded border px-1.5 py-0.5 text-xs ${SLOT_STATE_STYLE[slot.state] ?? SLOT_STATE_STYLE.open}`}
                        title={slotPillTitle(slot)}
                      >
                        {slot.slot} · {slotStatusLabel(slot)}
                      </span>
                    ))}
                    {!day.slots.length && (
                      <span className="text-xs text-muted-foreground">
                        No schedule slots configured.
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
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
  onForcePublish,
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
  onForcePublish: (jobId: number) => void;
}) {
  const [openSlot, setOpenSlot] = useState<string | null>(null);
  // Switching accounts swaps the whole grid; close any stale popover. Resetting
  // during render (vs. an effect) avoids a cascading re-render.
  const [lastAccountId, setLastAccountId] = useState(selectedAccountId);
  if (selectedAccountId !== lastAccountId) {
    setLastAccountId(selectedAccountId);
    setOpenSlot(null);
  }
  const allMode = selectedAccountId === ALL_ACCOUNTS_ID;
  const account = allMode
    ? null
    : coverage?.accounts.find((candidate) => candidate.account_id === selectedAccountId) ??
      coverage?.accounts[0] ??
      null;
  if (!coverage?.accounts.length || (!allMode && !account)) {
    return (
      <div className="rounded-lg border border-border bg-background/50 p-4 text-sm text-muted-foreground">
        No account schedule slots configured yet.
      </div>
    );
  }
  const complete = account !== null && account.total > 0 && account.filled === account.total;

  const header = (
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
          value={account?.account_id ?? ALL_ACCOUNTS_ID}
          onChange={(event) => onSelectAccount(Number(event.target.value))}
        >
          <option value={ALL_ACCOUNTS_ID}>All accounts ({coverage.accounts.length})</option>
          {coverage.accounts.map((candidate) => (
            <option key={candidate.account_id} value={candidate.account_id}>
              {candidate.account_name}
            </option>
          ))}
        </select>
      </label>
    </div>
  );

  if (allMode || account === null) {
    return (
      <div className="space-y-4 rounded-lg border border-border bg-background/50 p-4">
        {header}
        <AllAccountsCoverage accounts={coverage.accounts} onSelectAccount={onSelectAccount} />
      </div>
    );
  }

  return (
    <div className="space-y-4 rounded-lg border border-border bg-background/50 p-4">
      {header}
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
                    <p className="mt-1 truncate text-xs opacity-80" title={slotGateTitle(slot)}>
                      {slot.scheduled_at
                        ? `${new Date(slot.scheduled_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} · ${slot.job_title ?? `Job ${slot.job_id}`}`
                        : "No matching job"}
                    </p>
                    {slotGateHint(slot) && (
                      <p className="truncate text-xs font-medium opacity-90" title={slotGateTitle(slot)}>
                        {slotGateHint(slot)}
                      </p>
                    )}
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
                      onForcePublish={onForcePublish}
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
  const [cloudJobs, setCloudJobs] = useState<CloudWorkerJob[]>([]);
  const [cloudJobsError, setCloudJobsError] = useState<string | null>(null);
  const [cloudAccounts, setCloudAccounts] = useState<CloudWorkerAccount[]>([]);
  const [cloudAccountsError, setCloudAccountsError] = useState<string | null>(null);
  const [cloudControlLoading, setCloudControlLoading] = useState(false);

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
        current === ALL_ACCOUNTS_ID ||
        (current !== null && result.accounts.some((account) => account.account_id === current))
          ? current
          : result.accounts[0]?.account_id ?? null,
      );
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    }
  }, [syncCloudJobs]);
  const loadCloudControl = useCallback(async () => {
    setCloudControlLoading(true);
    try {
      const result = await bridge.dashboardCloudJobs();
      setCloudJobs(result.jobs);
      setCloudJobsError(null);
    } catch (err: unknown) {
      setCloudJobsError(err instanceof Error ? err.message : String(err));
    }
    try {
      const result = await bridge.dashboardCloudAccounts();
      setCloudAccounts(result.accounts);
      setCloudAccountsError(null);
    } catch (err: unknown) {
      setCloudAccountsError(err instanceof Error ? err.message : String(err));
    } finally {
      setCloudControlLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load();
      void loadHealth();
      void loadCoverage();
      void loadCloudControl();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [load, loadCloudControl, loadCoverage, loadHealth]);

  // Auto-refresh cloud sync + coverage every 30s (and on window focus) so
  // posted slots flip from "cloud" to "posted" without a manual Refresh click.
  useAutoRefresh(() => {
    void load();
    void loadCoverage();
    void loadCloudControl();
  }, 30000);

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

  // Worker account_key -> configured slots/day, joined via account_name (the same
  // name-matching convention already used above for `visibleJobs`/`fillableJobs`) --
  // the Worker itself has no notion of "slots/day", only local Account.upload_schedule_slots.
  const slotsPerDayByKey = new Map<string, number>();
  if (coverage) {
    for (const cloudJob of cloudJobs) {
      if (!cloudJob.account_name || slotsPerDayByKey.has(cloudJob.account_key)) continue;
      const match = coverage.accounts.find(
        (account) => account.account_name === cloudJob.account_name,
      );
      if (match) {
        slotsPerDayByKey.set(cloudJob.account_key, match.daily_target);
      }
    }
  }

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
      <CloudPublisherControlPanel
        jobs={cloudJobs}
        jobsError={cloudJobsError}
        accounts={cloudAccounts}
        accountsError={cloudAccountsError}
        loading={cloudControlLoading}
        slotsPerDayByKey={slotsPerDayByKey}
        onRefresh={loadCloudControl}
        onRunDue={async () => {
          try {
            await bridge.dashboardRunCloudDue();
            setMessage("Asked the Worker to process due jobs.");
          } catch (err: unknown) {
            setMessage(err instanceof Error ? err.message : String(err));
          }
          await Promise.all([loadCloudControl(), load(), loadCoverage()]);
        }}
        onForcePublish={async (job) => {
          if (job.upload_job_id === null) {
            setMessage("This job has no linked local reel to force-publish.");
            return;
          }
          await run(
            () => bridge.dashboardForcePublishCloudJob(job.upload_job_id!),
            "Forced publish — bypassing the safety cooldown.",
          );
          await loadCloudControl();
        }}
        onCancel={async (job) => {
          await run(() => bridge.dashboardCancelCloudJob(job.id), "Job canceled.");
          await loadCloudControl();
        }}
        onSaveAccountSettings={async (accountKey, payload) => {
          // The Worker only knows `account_key`; resolve it back to a local
          // account id via the account name shared with a job for this key (the
          // same name-matching convention used for slotsPerDayByKey above).
          const accountName = cloudJobs.find((job) => job.account_key === accountKey)?.account_name;
          const localAccountId = accountName
            ? coverage?.accounts.find((candidate) => candidate.account_name === accountName)
                ?.account_id
            : undefined;
          if (localAccountId === undefined) {
            throw new Error(
              "Could not resolve this Worker account to a local account id " +
                "(no recent job to match by name yet). Edit it from Account Manager instead.",
            );
          }
          await bridge.dashboardUpdateCloudAccountSettings(
            localAccountId,
            payload.dailyLimit,
            payload.minGapMinutes,
            payload.enabled,
          );
          await loadCloudControl();
        }}
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
        onForcePublish={(jobId) =>
          run(
            () => bridge.dashboardForcePublishCloudJob(jobId),
            "Forced publish — bypassing the safety cooldown.",
          )
        }
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
      {queue && queue.unscheduled_exports.length > 0 && (
        <div className="space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3">
          <p className="text-sm font-medium text-amber-600">
            {queue.unscheduled_exports.length} exported reel(s) not scheduled yet
          </p>
          {queue.unscheduled_exports.map((item) => (
            <div
              key={item.item_id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-background px-3 py-2"
            >
              <div className="min-w-0">
                <p className="truncate text-sm">
                  <span className="font-medium">{item.account_name}</span> — {item.title}
                </p>
                <p className="truncate text-xs text-muted-foreground">
                  {item.reason} ({item.output_name})
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {onOpenInProcessing && item.account_id !== null && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => onOpenInProcessing(item.account_id!, item.item_id, item.title)}
                  >
                    Edit in Processing
                  </Button>
                )}
                <Button
                  size="sm"
                  disabled={!item.can_schedule}
                  onClick={() =>
                    run(
                      () => bridge.autoScheduleForPublish(item.item_id),
                      `Scheduled "${item.title}".`,
                    )
                  }
                >
                  Schedule now
                </Button>
              </div>
            </div>
          ))}
        </div>
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
