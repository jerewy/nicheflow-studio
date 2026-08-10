import { useCallback, useEffect, useMemo, useState } from "react";

import { CropEditor } from "@/components/CropEditor";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useToast } from "@/components/ui/Toast";
import { bridge } from "@/lib/bridge";
import { cn } from "@/lib/utils";
import type {
  BatchCandidateGroup,
  BatchCandidateItem,
  BatchDraftImportResult,
  FinishBatchPlanEntry,
  FinishBatchResult,
  FinishBatchSchedule,
  JobSnapshot,
} from "@/types";

const DEFAULT_PER_ACCOUNT = 6;
const POLL_INTERVAL_MS = 900;

// Whether a reel's post is the Worker's job rather than this machine's. A
// deferred handoff still reads status "scheduled" because the upload is running
// on a background thread, but the reel is cloud-bound either way — the local
// publish loop skips cloud-mapped accounts outright.
function isCloudBound(schedule: FinishBatchSchedule | undefined): boolean {
  return schedule?.status === "cloud" || schedule?.cloud_handoff === "deferred";
}

// Same reasons the Processing screen offers, so a clip rejected here is
// recorded identically to one rejected there.
const REJECT_REASONS: { value: string; label: string }[] = [
  { value: "wrong_niche", label: "Wrong niche" },
  { value: "low_quality", label: "Low quality" },
  { value: "duplicate", label: "Duplicate (dedup missed)" },
  { value: "ad_campaign", label: "Ad / promo" },
];

type Stage = "pick" | "prepared" | "imported";

// Remembered choices live in the app DB via the bridge, NOT in localStorage:
// pywebview runs the window in private mode on a port that changes per launch,
// and localStorage is partitioned by origin, so browser-side storage was
// silently discarded on every restart.
//
// excludedAccountIds stores the EXCLUDED set, so an account added later joins
// the batch by default rather than being silently left out.
const SETTING_EXCLUDED_ACCOUNTS = "batchDrafts.excludedAccountIds";
const SETTING_AUTO_DISTRIBUTE = "batchDrafts.autoDistributeOnReject";
const SETTING_NICHE = "batchDrafts.niche";
const KNOWN_NICHES = ["history", "movie"] as const;

function parseExcludedAccounts(value: unknown): ReadonlySet<number> {
  if (!Array.isArray(value)) return new Set();
  return new Set(value.filter((id): id is number => Number.isInteger(id)));
}

function parseNiche(value: unknown): string | null {
  // Anything unrecognised means "all niches" rather than filtering the list down
  // to nothing with a niche that no longer exists.
  return typeof value === "string" && (KNOWN_NICHES as readonly string[]).includes(value)
    ? value
    : null;
}

/**
 * Cross-account drafting: one prompt covering every account, one pasted reply,
 * then apply + export + (per account) auto-schedule for the whole batch.
 *
 * Running this per account re-sends the same ~5k tokens of style rules once per
 * account and multiplies the paste/import round-trips by the size of the
 * network, which is the bulk of the remaining hand work.
 */
type ResultTone = "ok" | "warn" | "error" | "info";

interface ResultRow {
  itemId: number;
  tone: ResultTone;
  note: string;
}

const TONE_CLASS: Record<ResultTone, string> = {
  ok: "text-emerald-500",
  warn: "text-amber-500",
  error: "text-destructive",
  info: "text-muted-foreground",
};

/**
 * One collapsible group of per-reel outcomes, each row opening that reel in
 * Processing for the full detail.
 *
 * The batch screen only ever showed counts, so verifying a batch meant leaving
 * for the Processing tab and matching reels up by hand.
 */
function ResultList({
  rows,
  heading,
  itemMeta,
  labelFor,
  onOpen,
}: {
  rows: ResultRow[];
  heading: string;
  itemMeta: Map<
    number,
    { seq: number | null; title: string | null; accountId: number | null; accountName: string | null }
  >;
  labelFor: (itemId: number) => string;
  onOpen?: (itemId: number) => void;
}) {
  if (!rows.length) return null;
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {heading} ({rows.length})
      </p>
      <ul className="space-y-0.5">
        {rows.map((row) => {
          const meta = itemMeta.get(row.itemId);
          const canOpen = Boolean(onOpen && meta?.accountId != null);
          return (
            <li key={`${heading}-${row.itemId}`} className="flex items-baseline gap-2 text-xs">
              <button
                type="button"
                className={cn(
                  "shrink-0 font-medium",
                  canOpen ? "hover:underline" : "cursor-default",
                )}
                disabled={!canOpen}
                title={canOpen ? "Open this reel in Processing" : "No account for this reel"}
                onClick={() => onOpen?.(row.itemId)}
              >
                {labelFor(row.itemId)}
              </button>
              {meta?.accountName && (
                <span className="shrink-0 text-muted-foreground">{meta.accountName}</span>
              )}
              <span className={cn("truncate", TONE_CLASS[row.tone])} title={row.note}>
                {row.note}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

interface BatchDraftsScreenProps {
  active?: boolean;
  /** Jump to Processing focused on one reel, for the full detail view. */
  onOpenInProcessing?: (accountId: number, itemId: number | null, search: string) => void;
}

export function BatchDraftsScreen({
  active = true,
  onOpenInProcessing,
}: BatchDraftsScreenProps) {
  const { pushToast } = useToast();
  const [groups, setGroups] = useState<BatchCandidateGroup[]>([]);
  const [perAccount, setPerAccount] = useState(DEFAULT_PER_ACCOUNT);
  const [niche, setNiche] = useState<string | null>(null);
  // Preferences arrive from the bridge after mount. Until they do, nothing may
  // be written back, or the defaults would overwrite what was stored.
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<number>>(() => new Set());
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState<Stage>("pick");
  const [progress, setProgress] = useState<{ value: number; message: string } | null>(null);
  // Pinned at prepare time: reel numbers in the reply map to THIS list, so a
  // later change to the candidate pool must not shift the routing.
  const [preparedIds, setPreparedIds] = useState<number[] | null>(null);
  const [importResult, setImportResult] = useState<BatchDraftImportResult | null>(null);
  const [plan, setPlan] = useState<FinishBatchPlanEntry[] | null>(null);
  const [finishResult, setFinishResult] = useState<FinishBatchResult | null>(null);
  // Accounts explicitly switched off. Absent from the set means included, so a
  // newly distributed-to account joins the batch by default rather than being
  // silently left out.
  const [excludedAccounts, setExcludedAccounts] = useState<ReadonlySet<number>>(
    () => new Set(),
  );
  const [reviewing, setReviewing] = useState<BatchCandidateItem | null>(null);
  const [rejectReason, setRejectReason] = useState("wrong_niche");
  const [autoDistributeOnReject, setAutoDistributeOnReject] = useState(false);
  // Crop is opened per review, so it never stays open across clips.
  const [cropOpen, setCropOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const { groups: next } = await bridge.batchDraftCandidates(niche, perAccount);
      setGroups(next);
      setLoadError(null);
      // Default to everything on offer: the common case is "draft the next N
      // for every account", and un-ticking is faster than ticking 36 boxes.
      setSelected(new Set(next.flatMap((group) => group.items.map((item) => item.id))));
    } catch (err: unknown) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, [niche, perAccount]);

  // Wait for the stored niche before fetching, or the first paint would show
  // every account and then visibly re-filter once the preference arrives.
  useEffect(() => {
    if (active && settingsLoaded) void load();
  }, [active, load, settingsLoaded]);

  // One place to persist, so every path that changes the inclusion (per-account
  // chip, "include all", "none") is remembered without each having to say so.
  // Restore remembered choices once, before anything is allowed to save.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const stored = await bridge.getUiSettings([
          SETTING_EXCLUDED_ACCOUNTS,
          SETTING_AUTO_DISTRIBUTE,
          SETTING_NICHE,
        ]);
        if (cancelled) return;
        setExcludedAccounts(parseExcludedAccounts(stored[SETTING_EXCLUDED_ACCOUNTS]));
        setAutoDistributeOnReject(stored[SETTING_AUTO_DISTRIBUTE] === true);
        setNiche(parseNiche(stored[SETTING_NICHE]));
      } catch {
        // A preference that won't load must not block the screen; defaults stand.
      } finally {
        if (!cancelled) setSettingsLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // One place to persist, so every path that changes a remembered choice is
  // saved without each having to say so. Gated on settingsLoaded so the initial
  // defaults never overwrite what was restored.
  useEffect(() => {
    if (!settingsLoaded) return;
    void bridge.setUiSetting(SETTING_EXCLUDED_ACCOUNTS, [...excludedAccounts]);
  }, [excludedAccounts, settingsLoaded]);

  useEffect(() => {
    if (!settingsLoaded) return;
    void bridge.setUiSetting(SETTING_AUTO_DISTRIBUTE, autoDistributeOnReject);
  }, [autoDistributeOnReject, settingsLoaded]);

  useEffect(() => {
    if (!settingsLoaded) return;
    void bridge.setUiSetting(SETTING_NICHE, niche);
  }, [niche, settingsLoaded]);

  // Esc closes the review popup — and closes the crop editor first when it is
  // open, so one press never discards an in-progress crop along with the popup.
  useEffect(() => {
    if (!reviewing) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      if (cropOpen) setCropOpen(false);
      else setReviewing(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [reviewing, cropOpen]);

  // A clip only belongs to one account, and reject/distribute both need that
  // account's niche.
  const reviewingGroup = useMemo(
    () =>
      reviewing
        ? groups.find((group) => group.items.some((item) => item.id === reviewing.id)) ?? null
        : null,
    [groups, reviewing],
  );

  const includedGroups = useMemo(
    () => groups.filter((group) => !excludedAccounts.has(group.account_id)),
    [groups, excludedAccounts],
  );
  const selectedIds = useMemo(
    () =>
      includedGroups.flatMap((group) =>
        group.items.filter((i) => selected.has(i.id)).map((i) => i.id),
      ),
    [includedGroups, selected],
  );
  const visionBacked = useMemo(
    () =>
      includedGroups.flatMap((group) =>
        group.items.filter((i) => selected.has(i.id) && i.has_vision),
      ).length,
    [includedGroups, selected],
  );
  // queue_for_publish flips a scheduled job to "cloud" once the Worker takes it
  // over; anything else is still waiting on this machine's publish loop.
  const cloudScheduled = useMemo(
    () => (finishResult?.scheduled ?? []).filter((row) => isCloudBound(row.schedule)).length,
    [finishResult],
  );
  // finish_batch reports exports and schedules as separate lists; the per-reel
  // view needs them joined so one row can say "exported AND in cloud".
  const scheduleByItem = useMemo(
    () =>
      new Map(
        (finishResult?.scheduled ?? []).map((row) => [row.item_id, row.schedule]),
      ),
    [finishResult],
  );
  const activeAccounts = useMemo(
    () => includedGroups.filter((group) => group.items.some((i) => selected.has(i.id))).length,
    [includedGroups, selected],
  );

  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const toggleAccountItems = (group: BatchCandidateGroup) =>
    setSelected((prev) => {
      const next = new Set(prev);
      const allOn = group.items.every((item) => next.has(item.id));
      group.items.forEach((item) => (allOn ? next.delete(item.id) : next.add(item.id)));
      return next;
    });

  const toggleAccountIncluded = (accountId: number) =>
    setExcludedAccounts((prev) => {
      const next = new Set(prev);
      if (next.has(accountId)) next.delete(accountId);
      else next.add(accountId);
      return next;
    });

  const setAllAccountsIncluded = (included: boolean) =>
    setExcludedAccounts(
      included ? new Set() : new Set(groups.map((group) => group.account_id)),
    );

  const closeReview = () => {
    setReviewing(null);
    setCropOpen(false);
  };

  /** Reject the clip under review, then drop it from the candidate list. */
  const rejectReviewed = async () => {
    if (!reviewing) return;
    const item = reviewing;
    setBusy(true);
    try {
      await bridge.rejectItem(item.id, rejectReason);
      setGroups((prev) =>
        prev.map((group) => ({
          ...group,
          items: group.items.filter((row) => row.id !== item.id),
          available: Math.max(0, group.available - 1),
        })),
      );
      setSelected((prev) => {
        const next = new Set(prev);
        next.delete(item.id);
        return next;
      });
      setReviewing(null);
      setCropOpen(false);

      // Rejecting released this clip's slot, so the account is now one under
      // target. Topping up here saves a trip to Pool & Distribute per reject.
      const niche = reviewingGroup?.niche;
      if (autoDistributeOnReject && niche) {
        try {
          const result = await bridge.distributeNiche(niche, null);
          // Reload either way: reject already changed the list, and a successful
          // distribute adds clips that only a refetch will show.
          await load();
          pushToast(
            result.assigned > 0
              ? `Rejected #${item.account_seq ?? item.id}. Distributed ${result.assigned} replacement clip(s).`
              : `Rejected #${item.account_seq ?? item.id}. Nothing left in the ${niche} pool to replace it.`,
            "success",
          );
          return;
        } catch (distributeError: unknown) {
          // The reject itself succeeded; say so rather than reporting a failure.
          pushToast(
            `Rejected #${item.account_seq ?? item.id}, but auto-distribute failed: ` +
              (distributeError instanceof Error
                ? distributeError.message
                : String(distributeError)),
            "error",
          );
          return;
        }
      }
      pushToast(`Rejected #${item.account_seq ?? item.id}.`, "success");
    } catch (err: unknown) {
      pushToast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setBusy(false);
    }
  };

  const waitForJob = useCallback(async (jobId: string): Promise<unknown> => {
    for (;;) {
      const snapshot: JobSnapshot = await bridge.getJob(jobId);
      setProgress({ value: snapshot.progress, message: snapshot.message });
      if (snapshot.status === "succeeded") return snapshot.result;
      if (snapshot.status === "canceled") throw new Error(snapshot.message || "Canceled.");
      if (snapshot.status === "failed") throw new Error(snapshot.error ?? "The job failed.");
      await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
    }
  }, []);

  const prepare = async () => {
    if (!selectedIds.length) {
      pushToast("Pick at least one reel first.", "error");
      return;
    }
    setBusy(true);
    setProgress({ value: 0, message: "Preparing…" });
    setImportResult(null);
    setFinishResult(null);
    try {
      const { job_id } = await bridge.startPrepareMultiAccountBatchFrames(selectedIds);
      const frames = (await waitForJob(job_id)) as {
        folder: string;
        frames: { item_id: number }[];
      };
      const { prompt } = await bridge.buildMultiAccountBatchChatPrompt(selectedIds, {});
      await bridge.copyTextToClipboard(prompt);
      // Order the prompt actually used, so REEL n routes to the right reel.
      const { item_ids: ordered } = await bridge.multiAccountBatchOrder(selectedIds);
      setPreparedIds(ordered);
      setStage("prepared");
      if (frames.frames.length) await bridge.openBatchFramesFolder(frames.folder);
      pushToast(
        frames.frames.length
          ? `Prompt copied. ${frames.frames.length} reel(s) need their frame attached; the rest carry vision JSON.`
          : "Prompt copied. Every reel carries its own vision JSON, so no images to attach.",
        "success",
      );
    } catch (err: unknown) {
      pushToast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setBusy(false);
      setProgress(null);
    }
  };

  const importReply = async () => {
    const targets = preparedIds ?? selectedIds;
    if (!targets.length) return;
    setBusy(true);
    try {
      const text = await bridge.readClipboardText();
      const result = await bridge.importMultiAccountBatchDraft(text, targets);
      setImportResult(result);
      const { plan: next } = await bridge.planFinishBatch(targets);
      setPlan(next);
      setStage("imported");
      pushToast(
        `Imported ${result.imported.length}, failed ${result.failed.length}, unmatched ${result.unmatched.length}.`,
        result.failed.length || result.unmatched.length ? "error" : "success",
      );
    } catch (err: unknown) {
      pushToast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setBusy(false);
    }
  };

  const readyPlan = plan?.filter((entry) => entry.ready) ?? [];
  const willSchedule = readyPlan.filter((entry) => entry.auto_schedules).length;
  // Of the reels that will be scheduled, how many the Worker will own. Knowing
  // this BEFORE confirming matters: a local schedule only fires while this app
  // is running, a cloud one doesn't care.
  const willScheduleViaCloud = readyPlan.filter(
    (entry) => entry.auto_schedules && entry.publishes_via_cloud,
  ).length;

  // Results come back as bare item ids. Both result sections need a human label
  // and the account to jump to, so build one lookup from the candidate list
  // (which has the per-account "#N") and the finish plan (which has the account
  // and the applied title). The plan survives a reload of `groups`, so it is the
  // fallback rather than the primary.
  const itemMeta = useMemo(() => {
    const meta = new Map<
      number,
      { seq: number | null; title: string | null; accountId: number | null; accountName: string | null }
    >();
    for (const group of groups) {
      for (const item of group.items) {
        meta.set(item.id, {
          seq: item.account_seq,
          title: item.title,
          accountId: group.account_id,
          accountName: group.account_name,
        });
      }
    }
    for (const entry of plan ?? []) {
      const existing = meta.get(entry.item_id);
      meta.set(entry.item_id, {
        seq: entry.account_seq ?? existing?.seq ?? null,
        // The plan's title is the option that was applied, which is more useful
        // after import than the original scraped title.
        title: entry.title ?? existing?.title ?? null,
        accountId: entry.account_id ?? existing?.accountId ?? null,
        accountName: entry.account_name ?? existing?.accountName ?? null,
      });
    }
    return meta;
  }, [groups, plan]);

  const labelFor = (itemId: number) => {
    const meta = itemMeta.get(itemId);
    return `#${meta?.seq ?? itemId}`;
  };

  /** Open one reel in Processing. Falls back to a search when the account is
   *  unknown, so the row is never a dead end. */
  const openInProcessing = (itemId: number) => {
    const meta = itemMeta.get(itemId);
    if (!onOpenInProcessing || meta?.accountId == null) return;
    onOpenInProcessing(meta.accountId, itemId, "");
  };

  const finish = async () => {
    if (!readyPlan.length) return;
    // Name where each post actually runs from. "Scheduled to post" alone read as
    // local-only, which is the opposite of what a cloud-mapped account does.
    const localScheduled = willSchedule - willScheduleViaCloud;
    let scheduleNote = "";
    if (willSchedule) {
      const where =
        willScheduleViaCloud === willSchedule
          ? "posted by the CLOUD Worker (no need to keep this app open)"
          : willScheduleViaCloud === 0
            ? "posted from THIS MACHINE (the app must be running at the scheduled time)"
            : `${willScheduleViaCloud} posted by the CLOUD Worker and ${localScheduled} from ` +
              "THIS MACHINE (the app must be running for those)";
      scheduleNote =
        `\n\n${willSchedule} of them will also be SCHEDULED, because their account has ` +
        `auto-schedule-on-export turned on. They will be ${where}.`;
    }
    if (
      !window.confirm(
        `Apply the recommended option and export ${readyPlan.length} reel(s) across ${activeAccounts} account(s)?${scheduleNote}`,
      )
    )
      return;
    setBusy(true);
    setProgress({ value: 0, message: "Starting…" });
    try {
      const { job_id } = await bridge.startFinishBatch(readyPlan.map((entry) => entry.item_id));
      const result = (await waitForJob(job_id)) as FinishBatchResult;
      setFinishResult(result);
      pushToast(
        `Exported ${result.exported.length}, scheduled ${result.scheduled.length}, failed ${result.failed.length}.` +
          // Uploads are pipelined against the renders, so the last few are still
          // in flight here. Say so, or their "Scheduled" rows read as a stall.
          (result.pending_cloud
            ? ` ${result.pending_cloud} cloud upload(s) still finishing in the background.`
            : ""),
        result.failed.length ? "error" : "success",
      );
    } catch (err: unknown) {
      pushToast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setBusy(false);
      setProgress(null);
    }
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-3 p-5">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold">Batch drafts across accounts</h2>
              <p className="text-sm text-muted-foreground">
                One prompt for every account, one pasted reply, then apply and export the lot.
              </p>
            </div>
            <div className="flex items-center gap-4">
              <label className="flex items-center gap-2 text-sm">
                <span className="text-muted-foreground">Niche</span>
                <select
                  value={niche ?? ""}
                  disabled={busy}
                  onChange={(event) => setNiche(event.target.value || null)}
                  className="rounded-md border border-input bg-background px-2 py-1"
                  title="Batch one niche at a time. Each reel without visual evidence costs an attachment, and chat clients cap those, so a narrower batch is also a batch that fits."
                >
                  <option value="">All niches</option>
                  <option value="history">History</option>
                  <option value="movie">Movie</option>
                </select>
              </label>
              <label className="flex items-center gap-2 text-sm">
                <span className="text-muted-foreground">Reels per account</span>
                <input
                  type="number"
                  min={1}
                  max={20}
                  value={perAccount}
                  disabled={busy}
                  onChange={(e) => setPerAccount(Math.max(1, Number(e.target.value) || 1))}
                  className="w-16 rounded-md border border-input bg-background px-2 py-1"
                />
              </label>
            </div>
          </div>

          {loadError && <p className="text-sm text-destructive">{loadError}</p>}

          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span className="font-medium">
              {selectedIds.length} reel(s) across {activeAccounts} account(s)
            </span>
            <span className="text-muted-foreground">
              {visionBacked} vision-backed · {selectedIds.length - visionBacked} need a frame
            </span>
            <Button size="sm" variant="secondary" onClick={load} disabled={busy}>
              Refresh
            </Button>
          </div>

          {progress && (
            <div className="space-y-1">
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full bg-primary transition-[width]"
                  style={{ width: `${Math.round((progress.value ?? 0) * 100)}%` }}
                />
              </div>
              <p className="text-xs text-muted-foreground">{progress.message}</p>
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            <Button onClick={prepare} disabled={busy || !selectedIds.length}>
              1. Prepare batch &amp; copy prompt
            </Button>
            <Button
              variant="secondary"
              onClick={importReply}
              disabled={busy || stage === "pick"}
            >
              2. Import pasted reply
            </Button>
            <Button
              variant="secondary"
              onClick={finish}
              disabled={busy || !readyPlan.length}
            >
              3. Finish batch ({readyPlan.length})
            </Button>
          </div>
          {stage === "prepared" && (
            <p className="text-xs text-muted-foreground">
              Paste the prompt into ChatGPT or Claude, copy the whole reply, then use step 2.
            </p>
          )}
        </CardContent>
      </Card>

      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted-foreground">Accounts in this batch:</span>
        {groups.map((group) => {
          const included = !excludedAccounts.has(group.account_id);
          return (
            <button
              key={group.account_id}
              type="button"
              disabled={busy}
              onClick={() => toggleAccountIncluded(group.account_id)}
              className={cn(
                "rounded-full border px-3 py-1 text-xs transition-colors",
                included
                  ? "border-ring bg-secondary text-foreground"
                  : "border-border text-muted-foreground line-through opacity-60",
              )}
              title={included ? "Click to leave this account out" : "Click to include it"}
            >
              {group.account_name}
            </button>
          );
        })}
        <button
          type="button"
          className="text-xs text-muted-foreground underline"
          disabled={busy}
          onClick={() => setAllAccountsIncluded(excludedAccounts.size > 0)}
        >
          {excludedAccounts.size > 0 ? "include all" : "clear all"}
        </button>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        {groups.map((group) => {
          const allOn = group.items.length > 0 && group.items.every((i) => selected.has(i.id));
          const included = !excludedAccounts.has(group.account_id);
          const more = group.available - group.items.length;
          return (
            <Card
              key={group.account_id}
              className={cn(!included && "opacity-50")}
            >
              <CardContent className="space-y-2 p-4">
                <div className="flex items-center justify-between gap-2">
                  <label className="flex cursor-pointer items-start gap-2">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={included}
                      disabled={busy}
                      onChange={() => toggleAccountIncluded(group.account_id)}
                    />
                    <span>
                      <span className="block font-medium">{group.account_name}</span>
                      <span className="block text-xs text-muted-foreground">
                        {group.items.length} of {group.available} draftless
                        {more > 0 ? ` · ${more} more available` : ""}
                        {group.pending_media > 0
                          ? ` · ${group.pending_media} still downloading`
                          : ""}
                        {group.auto_schedules ? " · auto-schedules on export" : ""}
                      </span>
                    </span>
                  </label>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => toggleAccountItems(group)}
                    disabled={busy || !included || !group.items.length}
                  >
                    {allOn ? "None" : "All"}
                  </Button>
                </div>
                {group.items.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    {group.pending_media > 0
                      ? `${group.pending_media} distributed reel(s) still downloading. Refresh in a moment.`
                      : "No undrafted reels with a downloaded file. Distribute more from Pool & Distribute."}
                  </p>
                ) : (
                  <ul className="space-y-1">
                    {group.items.map((item) => (
                      <li key={item.id} className="flex items-start gap-2 text-sm">
                        <input
                          type="checkbox"
                          className="mt-1"
                          checked={included && selected.has(item.id)}
                          disabled={busy || !included}
                          onChange={() => toggle(item.id)}
                        />
                        <button
                          type="button"
                          className="flex-1 truncate text-left hover:underline"
                          title="Watch this clip before drafting it"
                          onClick={() => setReviewing(item)}
                        >
                          #{item.account_seq ?? item.id} {item.title ?? "(untitled)"}
                        </button>
                        <span
                          className={cn(
                            "shrink-0 text-xs",
                            item.has_vision ? "text-emerald-500" : "text-muted-foreground",
                          )}
                          title={
                            item.has_vision
                              ? "Visual evidence JSON present, no image needed"
                              : "No visual evidence; its frame will be attached"
                          }
                        >
                          {item.has_vision ? "vision" : "frame"}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>

      {reviewing && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6"
          onClick={closeReview}
        >
          <Card
            className="max-h-full w-full max-w-3xl overflow-auto"
            onClick={(event) => event.stopPropagation()}
          >
            <CardContent className="space-y-3 p-5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate font-medium">
                    #{reviewing.account_seq ?? reviewing.id} {reviewing.title ?? "(untitled)"}
                  </p>
                  {reviewing.source_url && (
                    <p className="truncate text-xs text-muted-foreground">
                      {reviewing.source_url}
                    </p>
                  )}
                </div>
                <Button size="sm" variant="ghost" onClick={closeReview} title="Esc">
                  Close
                </Button>
              </div>

              {reviewing.preview_url ? (
                <video
                  key={reviewing.preview_url}
                  src={reviewing.preview_url}
                  controls
                  autoPlay
                  className="max-h-[60vh] w-full rounded-md bg-black"
                />
              ) : (
                <p className="text-sm text-muted-foreground">
                  No local preview available for this clip.
                </p>
              )}

              {/* Every listed reel has a downloaded file (batch_candidates
                  requires one), so crop is always available here. Gating on
                  preview_url would hide it whenever the WebView media mapping
                  simply hasn't finished installing yet. */}
              <div className="rounded-md border border-border">
                <button
                  type="button"
                  className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-muted/50"
                  onClick={() => setCropOpen((open) => !open)}
                >
                  <span className="font-medium">Adjust crop</span>
                  <span className="text-xs text-muted-foreground">
                    {cropOpen ? "Hide" : "Set the 9:16 keep-region before drafting"}
                  </span>
                </button>
                {cropOpen && (
                  <div className="border-t border-border p-3">
                    <CropEditor
                      itemId={reviewing.id}
                      onClose={() => setCropOpen(false)}
                      onSaved={(message) => {
                        setCropOpen(false);
                        pushToast(message, "success");
                      }}
                    />
                  </div>
                )}
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <select
                  className="rounded-md border border-input bg-background px-2 py-1 text-sm"
                  value={rejectReason}
                  disabled={busy}
                  onChange={(event) => setRejectReason(event.target.value)}
                >
                  {REJECT_REASONS.map((reason) => (
                    <option key={reason.value} value={reason.value}>
                      {reason.label}
                    </option>
                  ))}
                </select>
                <Button variant="destructive" disabled={busy} onClick={rejectReviewed}>
                  Reject clip
                </Button>
                <span className="text-xs text-muted-foreground">
                  Rejecting pulls the footage from the pool and skips this reel. Reversible from
                  Processing.
                </span>
              </div>

              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={autoDistributeOnReject}
                  disabled={busy}
                  onChange={(event) => setAutoDistributeOnReject(event.target.checked)}
                />
                Distribute a replacement automatically after rejecting
                {reviewingGroup?.niche ? ` (${reviewingGroup.niche} pool)` : ""}
              </label>
            </CardContent>
          </Card>
        </div>
      )}

      {importResult && (
        <Card>
          <CardContent className="space-y-3 p-4 text-sm">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <p className="font-medium">Import results</p>
              <p className="text-muted-foreground">
                <span className={importResult.imported.length ? "text-emerald-500" : ""}>
                  {importResult.imported.length} imported
                </span>
                {" · "}
                <span className={importResult.failed.length ? "text-destructive" : ""}>
                  {importResult.failed.length} failed
                </span>
                {" · "}
                <span className={importResult.unmatched.length ? "text-amber-500" : ""}>
                  {importResult.unmatched.length} unmatched
                </span>
              </p>
            </div>

            {/* Everything landing cleanly is the case worth stating outright —
                otherwise a silent section reads as "did it even run?". */}
            {!importResult.failed.length && !importResult.unmatched.length ? (
              <p className="text-xs text-emerald-500">
                Every reel in the batch got a draft. Ready for Finish batch.
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Fix these before finishing, or they export with no applied draft.
              </p>
            )}

            <ResultList
              rows={importResult.imported.map((itemId) => ({
                itemId,
                tone: "ok" as const,
                note: itemMeta.get(itemId)?.title ?? "draft imported",
              }))}
              heading="Imported"
              itemMeta={itemMeta}
              labelFor={labelFor}
              onOpen={onOpenInProcessing ? openInProcessing : undefined}
            />
            <ResultList
              rows={importResult.failed.map((row) => ({
                itemId: row.item_id,
                tone: "error" as const,
                note: row.error,
              }))}
              heading="Failed"
              itemMeta={itemMeta}
              labelFor={labelFor}
              onOpen={onOpenInProcessing ? openInProcessing : undefined}
            />
            <ResultList
              rows={importResult.unmatched.map((itemId) => ({
                itemId,
                tone: "warn" as const,
                // Unmatched means the reply had no block for this reel at all —
                // usually a reply that was cut short or lost a header.
                note: "no block for this reel in the pasted reply",
              }))}
              heading="Unmatched"
              itemMeta={itemMeta}
              labelFor={labelFor}
              onOpen={onOpenInProcessing ? openInProcessing : undefined}
            />
          </CardContent>
        </Card>
      )}

      {plan && (
        <Card>
          <CardContent className="space-y-1 p-4 text-sm">
            <p className="font-medium">Finish plan</p>
            <p className="text-muted-foreground">
              {readyPlan.length} ready · {plan.length - readyPlan.length} blocked ·{" "}
              {willSchedule} will also be scheduled
              {willSchedule > 0 && (
                <>
                  {" ("}
                  <span className="text-emerald-500">{willScheduleViaCloud} cloud</span>
                  {" · "}
                  {willSchedule - willScheduleViaCloud} local{")"}
                </>
              )}
            </p>
            {plan
              .filter((entry) => !entry.ready)
              .map((entry) => (
                <p key={entry.item_id} className="text-xs text-muted-foreground">
                  #{entry.item_id} blocked: {entry.reason}
                </p>
              ))}
          </CardContent>
        </Card>
      )}

      {finishResult && (
        <Card>
          <CardContent className="space-y-3 p-4 text-sm">
            <p className="font-medium">Finish results</p>
            <p className="text-muted-foreground">
              {finishResult.exported.length} exported ·{" "}
              {finishResult.scheduled.length} scheduled
              {finishResult.scheduled.length > 0 && (
                // Which of the scheduled reels the Worker now owns. Without this
                // the only way to tell cloud from local was to open the
                // publishing dashboard and check each job.
                <>
                  {" ("}
                  <span className="text-emerald-500">{cloudScheduled} cloud</span>
                  {" · "}
                  {finishResult.scheduled.length - cloudScheduled} local{")"}
                </>
              )}{" "}
              · {finishResult.failed.length} failed
            </p>
            {/* Per reel: did it export, and did the Worker take the post over?
                Anything still "local" publishes from this machine, so it only
                goes out while the app is running — worth seeing per reel, not
                just as a count. */}
            <ResultList
              rows={finishResult.exported.map((row) => {
                const schedule = scheduleByItem.get(row.item_id);
                const cloudBound = isCloudBound(schedule);
                return {
                  itemId: row.item_id,
                  tone: cloudBound ? ("ok" as const) : ("info" as const),
                  note: cloudBound
                    ? schedule?.cloud_handoff === "deferred"
                      ? "exported · cloud upload finishing in the background"
                      : "exported · scheduled in cloud"
                    : schedule?.status
                      ? "exported · scheduled locally (posts only while this app runs)"
                      : "exported · not scheduled",
                };
              })}
              heading="Exported"
              itemMeta={itemMeta}
              labelFor={labelFor}
              onOpen={onOpenInProcessing ? openInProcessing : undefined}
            />
            <ResultList
              rows={finishResult.failed.map((row) => ({
                itemId: row.item_id,
                tone: "error" as const,
                note: `${row.stage}: ${row.error}`,
              }))}
              heading="Failed"
              itemMeta={itemMeta}
              labelFor={labelFor}
              onOpen={onOpenInProcessing ? openInProcessing : undefined}
            />
            <ResultList
              rows={finishResult.skipped.map((row) => ({
                itemId: row.item_id,
                tone: "warn" as const,
                note: row.reason,
              }))}
              heading="Skipped"
              itemMeta={itemMeta}
              labelFor={labelFor}
              onOpen={onOpenInProcessing ? openInProcessing : undefined}
            />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
