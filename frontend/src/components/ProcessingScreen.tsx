import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { CropEditor } from "@/components/CropEditor";
import { OptionCard } from "@/components/OptionCard";
import { PublishDueDialog } from "@/components/PublishDueDialog";
import { PublishNowDialog } from "@/components/PublishNowDialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/components/ui/Toast";
import { useAutoRefresh } from "@/hooks/useAutoRefresh";
import { useRevisitRefresh } from "@/hooks/useKeepAlive";
import { bridge, whenBridgeReady } from "@/lib/bridge";
import { formatDate } from "@/lib/format";
import type {
  BatchDraftImportResult,
  DraftRevision,
  DueRecencyWarning,
  ExportResult,
  LibraryItem,
  ProcessingContext,
  PublishJob,
  PublishRecency,
  WorkflowSettings,
} from "@/types";

const POLL_INTERVAL_MS = 4000;
const JOB_POLL_INTERVAL_MS = 1000;
const JOB_TIMEOUT_MS = 180000;
// Publishing drives a real browser and can take several minutes.
const PUBLISH_TIMEOUT_MS = 480000;
// Exports and scheduling upload the reel to the cloud Worker, and bulk runs
// serialize those uploads through one request pipe — a job can legitimately
// sit for many minutes waiting behind other accounts' uploads.
const UPLOAD_TIMEOUT_MS = 1800000;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// How many upcoming clips to warm ahead of the user's current position.
// Set to 0: prefetch is disabled to minimize authenticated download volume to
// Instagram (account-safety). Raise it again only once downloads no longer ride a
// real account's session.
const PREFETCH_AHEAD = 0;

// Warm the next few not-yet-downloaded clips' originals so opening them is instant
// instead of triggering a live Instagram fetch on click. Best-effort and
// fire-and-forget: the on-open path still reports real per-clip errors. Only
// pending-review clips without a downloaded file are worth warming.
function prefetchUpcoming(ordered: LibraryItem[], afterId: number | null): void {
  const start = afterId === null ? 0 : ordered.findIndex((c) => c.id === afterId) + 1;
  const ids = ordered
    .slice(start)
    .filter((c) => !c.has_file && c.raw_status === "pending_review")
    .slice(0, PREFETCH_AHEAD)
    .map((c) => c.id);
  if (ids.length > 0) void bridge.prefetchOriginals(ids);
}

function isPastDate(date: Date): boolean {
  return date.getTime() <= Date.now();
}

// Workflow status -> label + colored dot (Tailwind bg class) for the videos table.
const STATUS_META: Record<string, { label: string; dot: string }> = {
  pending_review: { label: "Pending review", dot: "bg-sky-500" },
  new: { label: "New", dot: "bg-sky-500" },
  draft: { label: "Draft", dot: "bg-amber-500" },
  exported: { label: "Exported", dot: "bg-violet-500" },
  scheduled: { label: "Scheduled", dot: "bg-fuchsia-500" },
  cloud: { label: "Cloud", dot: "bg-indigo-500" },
  // Cloud-bound but the Worker upload hasn't landed yet. Same indigo family as
  // "Cloud" on purpose: it never publishes locally, so it must not read as a
  // fallback to the local path.
  cloud_pending: { label: "Cloud · uploading", dot: "bg-indigo-400" },
  posted: { label: "Posted", dot: "bg-emerald-500" },
  rejected: { label: "Rejected", dot: "bg-orange-500" },
  skipped: { label: "Skipped", dot: "bg-zinc-500" },
  failed: { label: "Failed", dot: "bg-red-500" },
};

const MANUAL_STATUS_OPTIONS = [
  { value: "pending_review", label: "Pending review" },
  { value: "draft", label: "Draft" },
  { value: "exported", label: "Exported" },
] as const;

const REVIEW_REASON_LABELS: Record<string, string> = {
  wrong_niche: "Wrong niche",
  low_quality: "Low quality",
  duplicate: "Duplicate",
  ad_campaign: "Ad / promo",
};

function statusMeta(status: string): { label: string; dot: string } {
  return STATUS_META[status] ?? { label: status, dot: "bg-zinc-400" };
}

// Statuses backed by a live publish job. The manual status dropdown stays locked
// for these: the schedule has to be cancelled first, or the row and the job
// disagree about what is about to post.
function isQueuedStatus(status: string): boolean {
  return status === "scheduled" || status === "cloud" || status === "cloud_pending";
}

function shortDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString();
}

function scheduleStatusMessage(status: string, scheduled: string): string {
  return status === "cloud"
    ? `Sent to cloud for ${scheduled}.`
    : `Scheduled locally for ${scheduled}.`;
}

// Deep-link target when arriving via "Edit in Processing" from the publish
// schedule. itemId pins the exact library item (reliable: the exported title
// differs from the item's original title); search is a fallback text seed used
// only when no item id is available.
export interface ProcessingDeepLink {
  itemId: number | null;
  search: string;
}

interface ProcessingScreenProps {
  activeAccountId: number;
  activeAccountName: string | null;
  // The screen stays mounted across tab switches (keep-alive), so the deep link
  // is a prop that changes per navigation — a fresh object each time — rather
  // than mount-time state. null on manual visits (no stale pin inherited).
  deepLink?: ProcessingDeepLink | null;
  // False while the screen is kept alive but hidden behind another tab.
  active?: boolean;
}

function workflowPayload(workflow: WorkflowSettings | null): Record<string, unknown> {
  return {
    clip_premise: workflow?.clip_premise ?? "",
    caption_style: workflow?.caption_style ?? "",
    title_style: workflow?.title_style ?? "",
    title_length: workflow?.title_length ?? "long",
    caption_mode: workflow?.caption_mode ?? "shared",
    template: workflow?.template ?? "",
  };
}

// The POLLER gave up, not the job: the backend thread keeps running and will
// finish on its own. Callers must report this as "still working in the
// background", never as a failure — the old generic Error here produced false
// "scheduling failed: The job timed out" toasts during bulk cloud uploads.
class JobTimeoutError extends Error {}

// The job was canceled at the user's request (e.g. "Cancel export", or a reject
// that aborts an in-flight export). A clean outcome, not a failure — callers
// report it quietly instead of raising an error toast.
class JobCanceledError extends Error {}

// Poll a background job until it finishes; resolve with its result or throw the
// job's error message. Reports progress on each poll.
async function waitForJob(
  jobId: string,
  onProgress?: (progress: number, message: string) => void,
  timeoutMs: number = JOB_TIMEOUT_MS,
): Promise<unknown> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const snapshot = await bridge.getJob(jobId);
    onProgress?.(snapshot.progress, snapshot.message);
    if (snapshot.status === "succeeded") return snapshot.result;
    if (snapshot.status === "canceled") {
      throw new JobCanceledError(snapshot.message || "Canceled.");
    }
    if (snapshot.status === "failed") {
      throw new Error(snapshot.error ?? "The job failed.");
    }
    if (Date.now() > deadline) throw new JobTimeoutError("The job timed out.");
    await sleep(JOB_POLL_INTERVAL_MS);
  }
}

interface EditableOptions {
  titles: string[];
  captions: string[];
}

function toEditable(revision: DraftRevision | null): EditableOptions {
  if (!revision) return { titles: [], captions: [] };
  return {
    titles: [...revision.title_options],
    captions: [...revision.caption_options],
  };
}

/**
 * The one caption every option shares, or null when the options carry distinct
 * captions (an older 3-caption revision, or one the user has edited apart).
 * Returning null is what keeps those revisions rendering per-card as before.
 */
function sharedCaptionOf(captions: string[]): string | null {
  if (captions.length < 2) return null;
  const [first] = captions;
  if (!first.trim()) return null;
  return captions.every((caption) => caption === first) ? first : null;
}

function sameOptions(a: EditableOptions, revision: DraftRevision | null): boolean {
  if (!revision) return a.titles.length === 0 && a.captions.length === 0;
  return (
    JSON.stringify(a.titles) === JSON.stringify(revision.title_options) &&
    JSON.stringify(a.captions) === JSON.stringify(revision.caption_options)
  );
}

export function ProcessingScreen({
  activeAccountId,
  activeAccountName,
  deepLink,
  active = true,
}: ProcessingScreenProps) {
  const { pushToast } = useToast();
  const [context, setContext] = useState<ProcessingContext | null>(null);
  const [itemsLoaded, setItemsLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Refilling this niche's pool from the header (same action as Pool & Distribute),
  // tracked separately from `busy` so it doesn't gate per-item editing.
  const [distributing, setDistributing] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [canGenerate, setCanGenerate] = useState(false);
  // Export progress keyed by item id so exports run in the background: the
  // user can switch items mid-export, and a finishing export only updates the
  // screen if its item is still the one being viewed.
  // Live progress of a first-open original fetch (one at a time; opening a clip
  // is a user action, not a batch).
  const [itemDownload, setItemDownload] = useState<{
    itemId: number;
    value: number;
    message: string;
  } | null>(null);
  const [exportJobs, setExportJobs] = useState<Record<number, { value: number; message: string }>>(
    {},
  );
  // Job id of each item's in-flight export, so a "Cancel export" click or a
  // reject can target the running job. A ref (not state) because it's only read
  // inside async handlers — no re-render needs to depend on it.
  const exportJobIdsRef = useRef<Record<number, string>>({});
  const [exportedPath, setExportedPath] = useState<string | null>(null);
  const [watermarkStatus, setWatermarkStatus] = useState<{
    replaced: boolean;
    detected: string | null;
    skippedReason: string | null;
  } | null>(null);
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [publishJobs, setPublishJobs] = useState<PublishJob[]>([]);
  const [scheduleAt, setScheduleAt] = useState("");
  const [publishMessage, setPublishMessage] = useState<string | null>(null);
  // Cloud scheduling can upload the full reel, so track it per item and let the
  // user keep working elsewhere while the background bridge job finishes.
  const [schedulingItems, setSchedulingItems] = useState<Record<number, string>>({});
  // Live-posting state: the opt-in auto-publish toggle and how many scheduled
  // reels are currently past due.
  //
  // Manual posts are tracked per item id (message keyed by item) so a post runs
  // in the BACKGROUND: the user can switch item/niche and keep working while it
  // posts, and ONLY the posting item has its conflicting actions disabled.
  const [publishingItems, setPublishingItems] = useState<Record<number, string>>({});
  // The "Publish due now" batch flow (not tied to a single item).
  const [publishingDue, setPublishingDue] = useState(false);
  const [cancelingScheduleIds, setCancelingScheduleIds] = useState<Record<number, boolean>>({});
  const [updatingStatusIds, setUpdatingStatusIds] = useState<Record<number, boolean>>({});
  const [autoPublish, setAutoPublish] = useState(false);
  const [dueCount, setDueCount] = useState(0);
  // Open recent-post confirmation for "Publish due now" (null = closed).
  const [dueDialog, setDueDialog] = useState<DueRecencyWarning[] | null>(null);
  // Open recent-post confirmation for "Publish Now" on this item (null = closed).
  const [nowDialog, setNowDialog] = useState<PublishRecency | null>(null);
  const [isDesktop, setIsDesktop] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  // A deep link pins one item by id; the text seed is only used without an id.
  const [itemSearch, setItemSearch] = useState(
    deepLink?.itemId != null ? "" : (deepLink?.search ?? ""),
  );
  const [focusItemId, setFocusItemId] = useState<number | null>(deepLink?.itemId ?? null);
  const [itemFilter, setItemFilter] = useState("all");
  const [handoffMessage, setHandoffMessage] = useState<string | null>(null);
  const [batchSize, setBatchSize] = useState(6);
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchResult, setBatchResult] = useState<BatchDraftImportResult | null>(null);
  // Snapshot of the reel->item mapping captured at "Prepare batch" time. Import
  // routes REEL <n> by POSITION, so it must target exactly the items the pasted
  // prompt was built from, not the live draftable window (which slides as items
  // gain drafts). null until a batch is prepared and after a fully clean import;
  // import falls back to the live window only then (e.g. after an app restart).
  const [preparedBatch, setPreparedBatch] = useState<{ id: number; label: string }[] | null>(
    null,
  );
  // Ids the user hand-picked (via list checkboxes) for the next batch; empty =
  // fall back to the auto "first N draftless" window.
  const [manualBatchIds, setManualBatchIds] = useState<number[]>([]);
  const [workflow, setWorkflow] = useState<WorkflowSettings | null>(null);
  const [finalTitle, setFinalTitle] = useState("");
  const [finalCaption, setFinalCaption] = useState("");
  // Separate error state for the exported-reel preview shown beside the final
  // title/caption, so a broken export file there doesn't affect the top preview.
  const [exportPreviewError, setExportPreviewError] = useState<string | null>(null);
  // Reason used by the Processing "Reject clip" cleanup action.
  const [reviewReason, setReviewReason] = useState("wrong_niche");
  // Whether the manual crop editor is open.
  const [cropOpen, setCropOpen] = useState(false);

  // The revision currently loaded into the editor, and the user's edits on top.
  const [loadedRevision, setLoadedRevision] = useState<DraftRevision | null>(null);
  const [edits, setEdits] = useState<EditableOptions>({ titles: [], captions: [] });
  // A newer revision detected while the user has unsaved edits (dirty-edit guard).
  const [pendingRevision, setPendingRevision] = useState<DraftRevision | null>(null);

  const itemId = context?.item.id ?? null;
  // Ref mirror so async export completions can tell whether the user is still
  // viewing the item they exported (state writes are gated on this).
  const itemIdRef = useRef<number | null>(itemId);
  // Whether the open recency dialog was triggered by "Publish via Browser"
  // (force-local) vs "Publish Now" (cloud-allowed). The dialog itself is shared,
  // so without this the "Post anyway" path would lose the force-local intent and
  // a cloud-mapped account would silently hand off to the Worker instead of the
  // local browser.
  const nowDialogForceLocalRef = useRef(false);
  // Ref mirror of the active niche account. A background job (export, publish)
  // captures the account at call time; if the user switches niche while it runs,
  // its completion must NOT repaint the Videos list with the now-previous
  // account's items. Completion writes to `items` are gated on this ref.
  const activeAccountIdRef = useRef(activeAccountId);
  useEffect(() => {
    itemIdRef.current = itemId;
  }, [itemId]);
  useEffect(() => {
    activeAccountIdRef.current = activeAccountId;
  }, [activeAccountId]);
  const exporting = itemId !== null && exportJobs[itemId] !== undefined;
  const exportProgress = itemId !== null ? exportJobs[itemId] ?? null : null;
  // The viewed item's in-flight post message (undefined = not posting). Only the
  // posting item is gated; other items stay fully editable.
  const thisItemPublishMessage =
    itemId !== null ? publishingItems[itemId] : undefined;
  const isThisItemPublishing = thisItemPublishMessage !== undefined;
  const thisItemScheduleMessage =
    itemId !== null ? schedulingItems[itemId] : undefined;
  const isThisItemScheduling = thisItemScheduleMessage !== undefined;
  const dirty = useMemo(
    () => loadedRevision !== null && !sameOptions(edits, loadedRevision),
    [edits, loadedRevision],
  );
  // Keep the latest dirty flag readable inside the polling closure without
  // resubscribing the interval on every keystroke.
  const dirtyRef = useRef(dirty);
  const loadedRef = useRef<DraftRevision | null>(loadedRevision);

  useEffect(() => {
    dirtyRef.current = dirty;
    loadedRef.current = loadedRevision;
  }, [dirty, loadedRevision]);

  useEffect(() => {
    if (!actionError) return;
    const timer = window.setTimeout(() => setActionError(null), 8000);
    return () => window.clearTimeout(timer);
  }, [actionError]);

  // Apply a deep link in place: the screen no longer remounts per visit (tabs
  // are kept alive), so the pin is adjusted when the prop changes — a fresh
  // object per navigation. A manual tab visit clears the link (null), which
  // drops any lingering schedule pin but keeps the user's own search text.
  // Render-phase adjustment per react.dev "adjusting state when a prop changes".
  const [prevDeepLink, setPrevDeepLink] = useState(deepLink);
  if (deepLink !== prevDeepLink) {
    setPrevDeepLink(deepLink);
    if (deepLink) {
      setFocusItemId(deepLink.itemId);
      setItemSearch(deepLink.itemId != null ? "" : deepLink.search);
    } else {
      setFocusItemId(null);
    }
  }

  const loadRevisionIntoEditor = useCallback((revision: DraftRevision | null) => {
    setLoadedRevision(revision);
    setEdits(toEditable(revision));
    setPendingRevision(null);
  }, []);

  const refreshPublishJobs = useCallback((id: number) => {
    bridge
      .listPublishJobs(id)
      .then(setPublishJobs)
      .catch(() => setPublishJobs([]));
  }, []);

  // Repaint the Videos list for `accountId`, but ONLY if the user is still on
  // that niche when the fetch returns — a background job (export, publish) or a
  // scheduling action that finishes after a niche switch must not clobber the
  // new account's list with the previous account's items.
  const refreshItems = useCallback((accountId: number) => {
    bridge
      .listLibraryItems(accountId)
      .then((list) => {
        if (activeAccountIdRef.current === accountId) setItems(list);
      })
      .catch(() => undefined);
  }, []);

  const applyContext = useCallback(
    (ctx: ProcessingContext) => {
      setContext(ctx);
      loadRevisionIntoEditor(ctx.latest_revision);
      setExportedPath(null);
      setWatermarkStatus(null);
      setPublishMessage(null);
      setPreviewError(null);
      setExportPreviewError(null);
      setHandoffMessage(null);
      refreshPublishJobs(ctx.item.id);
      bridge
        .getWorkflowSettings(ctx.item.id)
        .then((settings) => {
          setWorkflow(settings);
          setFinalTitle(settings.title_draft);
          setFinalCaption(settings.caption_draft);
        })
        .catch(() => setWorkflow(null));
      // Tell the backend which item is active so the Codex CLI `current`
      // command follows this window's selection. Best-effort.
      void bridge.setActiveItem(ctx.item.id).catch(() => undefined);
    },
    [loadRevisionIntoEditor, refreshPublishJobs],
  );

  // Load the active account's videos, then open the first one that opens. Re-runs
  // when the active niche account changes.
  useEffect(() => {
    let cancelled = false;
    whenBridgeReady().then(async (ready) => {
      if (cancelled) return;
      setIsDesktop(ready);
      bridge
        .canGenerate()
        .then(setCanGenerate)
        .catch(() => setCanGenerate(false));

      let list: LibraryItem[];
      try {
        list = await bridge.listLibraryItems(activeAccountId);
      } catch (err: unknown) {
        // The listing itself failing is the only thing that genuinely blocks the
        // whole screen.
        if (!cancelled) setLoadError(err instanceof Error ? err.message : String(err));
        return;
      }
      if (cancelled) return;
      setItems(list);
      setItemsLoaded(true);
      if (list.length === 0) {
        setContext(null);
        return;
      }

      // Open the first clip that loads. A single un-openable clip (deleted/private
      // source) must not take down the whole screen — skip past it so the queue
      // still works and the user can reject the offender. Bounded so a systemic
      // failure (e.g. an expired login) still surfaces quickly as a screen error.
      const MAX_OPEN_ATTEMPTS = 5;
      let opened = false;
      let skipped = 0;
      let lastError: unknown = null;
      for (const candidate of list.slice(0, MAX_OPEN_ATTEMPTS)) {
        try {
          const ctx = await bridge.getContext(candidate.id);
          if (cancelled) return;
          applyContext(ctx);
          opened = true;
          // Warm the next few clips so sequential review doesn't wait on a fetch.
          prefetchUpcoming(list, candidate.id);
          break;
        } catch (err: unknown) {
          if (cancelled) return;
          skipped += 1;
          lastError = err;
        }
      }
      if (cancelled) return;
      if (!opened) {
        setLoadError(lastError instanceof Error ? lastError.message : String(lastError));
        return;
      }
      if (skipped > 0) {
        setActionError(
          `Skipped ${skipped} clip${skipped > 1 ? "s" : ""} whose Instagram source couldn't be opened — reject ${skipped > 1 ? "them" : "it"} from the queue.`,
        );
      }
    });
    return () => {
      cancelled = true;
    };
  }, [applyContext, activeAccountId]);

  const switchItem = async (id: number) => {
    if (id === itemId) return;
    setActionError(null);
    // Opening an item clears its NEW badge (the backend marks it seen on
    // getContext); reflect that immediately in the list.
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, is_new: false } : it)));
    try {
      // A clip with no local file triggers a live Instagram fetch on open.
      // Run it as a job first so the user sees progress (and retry attempts)
      // instead of a frozen screen while yt-dlp works.
      const target = items.find((it) => it.id === id);
      if (target && !target.has_file) {
        setItemDownload({ itemId: id, value: 0, message: "Starting download…" });
        try {
          const { job_id } = await bridge.startItemDownload(id);
          await waitForJob(job_id, (value, message) =>
            setItemDownload({ itemId: id, value, message }),
          );
          setItems((prev) => prev.map((it) => (it.id === id ? { ...it, has_file: true } : it)));
        } finally {
          setItemDownload(null);
        }
      }
      const ctx = await bridge.getContext(id);
      applyContext(ctx);
      // Warm the clips after this one in the current (filtered) view order.
      prefetchUpcoming(filteredItems, id);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  };

  const queueForPublish = async (scheduled: string | null) => {
    if (itemId === null) return;
    setActionError(null);
    setPublishMessage(null);
    let scheduledIso: string | null = null;
    try {
      if (scheduled) {
        // datetime-local gives a naive string like "2026-06-09T12:05". Convert
        // to a UTC ISO string so the backend never guesses the local timezone.
        const d = new Date(scheduled);
        if (isNaN(d.getTime())) throw new Error("Invalid scheduled time.");
        if (isPastDate(d)) {
          throw new Error("Scheduled time is in the past — pick a future time.");
        }
        // Python 3.10 fromisoformat does not support "Z"; use "+00:00" instead.
        scheduledIso = d.toISOString().replace("Z", "+00:00");
      }
      if (scheduledIso !== null) {
        void runScheduleInBackground(itemId, activeAccountId, scheduledIso, false);
        return;
      }
      setBusy(true);
      const result = await bridge.queueForPublish(itemId, scheduledIso);
      setPublishMessage(
        `${result.created ? "Added to" : "Updated in"} publish queue as ${result.status}.`,
      );
      refreshPublishJobs(itemId);
      // Reflect the new queue state (e.g. "Scheduled") in the Videos list.
      refreshItems(activeAccountId);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      if (scheduledIso === null) setBusy(false);
    }
  };

  const autoScheduleForPublish = async () => {
    if (itemId === null) return;
    setActionError(null);
    setPublishMessage(null);
    void runScheduleInBackground(itemId, activeAccountId, null, true);
  };

  const cancelSchedule = async (job: PublishJob) => {
    if (itemId === null || !job.scheduled_at || job.posted_at) return;
    if (
      !window.confirm(
        "Cancel this scheduled publish? If it was sent to cloud, the cloud job will be canceled too.",
      )
    )
      return;
    setActionError(null);
    setPublishMessage(null);
    setCancelingScheduleIds((current) => ({ ...current, [job.id]: true }));
    try {
      const result = await bridge.unscheduleJob(job.id);
      setPublishMessage(`Canceled schedule for ${result.title ?? "this reel"}.`);
      pushToast("Scheduled publish canceled.", "success");
      refreshPublishJobs(itemId);
      refreshItems(activeAccountId);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setActionError(message);
      pushToast(`Could not cancel schedule: ${message}`, "error");
    } finally {
      setCancelingScheduleIds((current) => {
        const next = { ...current };
        delete next[job.id];
        return next;
      });
    }
  };

  const runScheduleInBackground = async (
    scheduleItemId: number,
    scheduleAccountId: number,
    scheduledIso: string | null,
    automatic: boolean,
  ) => {
    const accountLabel = activeAccountName ?? "Account";
    setSchedulingItems((current) => ({
      ...current,
      [scheduleItemId]: automatic ? "Auto-scheduling..." : "Scheduling...",
    }));
    pushToast(
      `${accountLabel} - scheduling #${scheduleItemId} in the background.`,
      "info",
    );
    try {
      const { job_id } = automatic
        ? await bridge.startAutoScheduleForPublish(scheduleItemId)
        : await bridge.startQueueForPublish(scheduleItemId, scheduledIso);
      const result = (await waitForJob(job_id, undefined, UPLOAD_TIMEOUT_MS)) as {
        status: string;
        scheduled_at: string | null;
      };
      const scheduled = result.scheduled_at
        ? new Date(result.scheduled_at).toLocaleString()
        : "the next open slot";
      const statusMessage = scheduleStatusMessage(result.status, scheduled);
      pushToast(
        `${accountLabel} - #${scheduleItemId} ${statusMessage}`,
        "success",
      );
      if (itemIdRef.current === scheduleItemId) {
        setPublishMessage(statusMessage);
        refreshPublishJobs(scheduleItemId);
      }
      refreshItems(scheduleAccountId);
    } catch (err: unknown) {
      if (err instanceof JobTimeoutError) {
        // Only the poller gave up — the backend is still scheduling/uploading
        // and the cloud sync sweep guarantees the outcome. The 30s refresh
        // flips the row when it lands; a "failed" toast here would be false.
        pushToast(
          `${accountLabel} - #${scheduleItemId} is still scheduling in the background ` +
            `(cloud upload). The list updates automatically when it lands.`,
          "info",
        );
        return;
      }
      const message = err instanceof Error ? err.message : String(err);
      if (itemIdRef.current === scheduleItemId) setActionError(message);
      pushToast(`${accountLabel} - #${scheduleItemId} scheduling failed: ${message}`, "error");
    } finally {
      setSchedulingItems((current) => {
        const next = { ...current };
        delete next[scheduleItemId];
        return next;
      });
    }
  };

  // --- Live publishing (Publish Now + opt-in auto-publish-due) --- //

  const refreshDueCount = useCallback(async () => {
    try {
      setDueCount((await bridge.publishDueCount()).due);
    } catch {
      // Non-fatal; the due count just won't update.
    }
  }, []);

  // Kept-alive screens don't remount on revisit; restore the freshness a
  // remount used to give (new scraped items, changed statuses) without
  // resetting the open item, editor, or in-flight job progress.
  useRevisitRefresh(active, () => {
    refreshItems(activeAccountId);
    void refreshDueCount();
    if (itemIdRef.current !== null) refreshPublishJobs(itemIdRef.current);
  });

  // A row's derived status can change with no action on this screen: pool
  // distribution schedules exported clips in the background and the cloud
  // Worker posts them later, while download_items.status stays pending_review
  // (the schedule outranks it in the derived status). Without a periodic pull
  // the table shows "Pending review" long after a clip went to cloud.
  useAutoRefresh(() => {
    refreshItems(activeAccountId);
    void refreshDueCount();
  }, 30000);

  const publishNow = async () => {
    if (itemId === null) return;
    // Gate on the same-account recency window: if the account posted too
    // recently, open the confirmation dialog (reschedule / post anyway) instead
    // of posting. Otherwise a plain confirm is enough.
    const recency = await bridge
      .itemPublishRecency(itemId)
      .catch(() => ({ on_cooldown: false }) as PublishRecency);
    // Open the dialog for both "posted recently" and "currently posting"
    // (in_progress) — the dialog adapts its options (the in-flight case hides
    // "Post anyway" since two posts can't run at once).
    if (recency.on_cooldown) {
      nowDialogForceLocalRef.current = false;
      setNowDialog(recency);
      return;
    }
    if (
      !window.confirm(
        "Post this reel to your Instagram account now? This logs in and publishes live — it can't be undone.",
      )
    )
      return;
    await doPublishNow(false, false);
  };

  const publishViaBrowser = async () => {
    if (itemId === null) return;
    const recency = await bridge
      .itemPublishRecency(itemId)
      .catch(() => ({ on_cooldown: false }) as PublishRecency);
    if (recency.on_cooldown) {
      nowDialogForceLocalRef.current = true;
      setNowDialog(recency);
      return;
    }
    if (
      !window.confirm(
        "Publish this reel now with the local Instagram browser session? This requires the app to stay open and cannot be undone.",
      )
    )
      return;
    await doPublishNow(false, true);
  };

  // The actual live post. `allowRecent` true is the explicit "post anyway"
  // override from the recency dialog; false posts only when not on cooldown
  // (the backend also refuses and returns "on_cooldown" as a backstop).
  //
  // Runs in the BACKGROUND keyed by item id: the user can switch item/niche and
  // keep working while it posts. On completion we DON'T snap the view back to the
  // posted item — a toast reports the result, and the screen only refreshes if
  // the user is still viewing that item.
  const doPublishNow = async (allowRecent: boolean, forceLocal = false) => {
    if (itemId === null) return;
    // Pin id + account name at call time; the user may move on before this
    // multi-minute post finishes.
    const publishItemId = itemId;
    const accountLabel = activeAccountName ?? "Account";
    // Keep the dialog's force-local intent in sync with the actual call, so if the
    // backend backstop re-opens the recency dialog its "Post anyway" preserves it.
    nowDialogForceLocalRef.current = forceLocal;
    setNowDialog(null);
    setPublishingItems((m) => ({ ...m, [publishItemId]: "Publishing…" }));
    setActionError(null);
    setPublishMessage(null);
    try {
      const { job_id } = await bridge.startPublishNow(publishItemId, allowRecent, forceLocal);
      const result = (await waitForJob(
        job_id,
        (_value, message) => {
          if (!message) return;
          setPublishingItems((m) =>
            publishItemId in m ? { ...m, [publishItemId]: message } : m,
          );
        },
        PUBLISH_TIMEOUT_MS,
      )) as {
        status: string;
        posted_url?: string | null;
        error?: string | null;
        account_name?: string | null;
        minutes_since?: number;
        recommended_next_at?: string | null;
      };
      if (result.status === "posted") {
        const when = new Date().toLocaleTimeString([], {
          hour: "numeric",
          minute: "2-digit",
        });
        pushToast(`✅ ${accountLabel} — posted #${publishItemId} at ${when}`, "success");
      } else if (result.status === "cloud") {
        // Cloud-mapped account: the Worker (Meta Graph API) publishes it; the
        // reel lands in ~1–3 min and the cloud-sync poll flips it to posted.
        pushToast(
          `☁️ ${accountLabel} — #${publishItemId} sent to cloud, posting shortly`,
          "success",
        );
      } else if (result.status === "on_cooldown") {
        // Backend backstop tripped (shouldn't normally happen since the UI
        // pre-checks). Re-open the dialog only if still on this item, else toast.
        if (itemIdRef.current === publishItemId) {
          setNowDialog({ on_cooldown: true, ...result });
        } else {
          pushToast(
            `⏳ ${accountLabel} — #${publishItemId} not posted (posted too recently).`,
            "info",
          );
        }
        return;
      } else {
        pushToast(
          `⚠️ ${accountLabel} — #${publishItemId} publish ${result.status}${result.error ? `: ${result.error}` : ""}.`,
          "error",
        );
      }
      // Only refresh THIS screen if the user is still viewing the posted item
      // (refreshing the list with a now-stale active account would clobber the
      // niche they switched to). Otherwise the toast is the only feedback.
      if (itemIdRef.current === publishItemId) {
        refreshPublishJobs(publishItemId);
        refreshItems(activeAccountId);
        const ctx = await bridge.getContext(publishItemId);
        if (itemIdRef.current === publishItemId) setContext(ctx);
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      if (itemIdRef.current === publishItemId) {
        setActionError(message);
      } else {
        pushToast(`⚠️ ${accountLabel} — #${publishItemId} publish failed: ${message}`, "error");
      }
    } finally {
      setPublishingItems((m) => {
        const next = { ...m };
        delete next[publishItemId];
        return next;
      });
    }
  };

  const setProcessingStatus = async (candidate: LibraryItem, status: string) => {
    if (status === candidate.status) return;
    if (candidate.status === "posted") {
      const label = statusMeta(status).label;
      if (
        !window.confirm(
          `Change only #${candidate.account_seq ?? candidate.id} from Posted to ${label}? The original post history will be kept and a new publish attempt will be created.`,
        )
      )
        return;
    } else if (status === "posted") {
      // Reverting a reopened item: discard the draft repost attempt and return to
      // Posted. The original post history is kept.
      if (
        !window.confirm(
          `Set #${candidate.account_seq ?? candidate.id} back to Posted? This discards the reopened draft attempt and keeps the original post history.`,
        )
      )
        return;
    }
    setActionError(null);
    setPublishMessage(null);
    setUpdatingStatusIds((current) => ({ ...current, [candidate.id]: true }));
    try {
      const result = await bridge.setProcessingStatus(candidate.id, status);
      const label = statusMeta(result.status).label;
      pushToast(`#${candidate.account_seq ?? candidate.id} changed to ${label}.`, "success");
      setItems((current) =>
        current.map((row) =>
          row.id === result.item_id ? { ...row, status: result.status } : row,
        ),
      );
      setContext((current) =>
        current && current.item.id === result.item_id
          ? { ...current, item: { ...current.item, status: result.status } }
          : current,
      );
      if (itemId === result.item_id) refreshPublishJobs(result.item_id);
      refreshItems(activeAccountId);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setActionError(message);
      pushToast(`Could not change status: ${message}`, "error");
    } finally {
      setUpdatingStatusIds((current) => {
        const next = { ...current };
        delete next[candidate.id];
        return next;
      });
    }
  };

  // Run the due-publish job. `allowRecent` false defers reels whose account
  // posted too recently; true is the explicit "publish anyway" path.
  const runPublishDue = async (allowRecent: boolean) => {
    setDueDialog(null);
    setPublishingDue(true);
    setActionError(null);
    setPublishMessage(null);
    try {
      const { job_id } = await bridge.startPublishDue(allowRecent);
      const result = (await waitForJob(job_id, undefined, PUBLISH_TIMEOUT_MS)) as {
        due: number;
        posted: number;
        failed: number;
        deferred: number;
      };
      if (result.due > 0) {
        const rescheduled = result.deferred ? `, rescheduled ${result.deferred}` : "";
        pushToast(
          `Publish due: posted ${result.posted}, failed ${result.failed}${rescheduled} of ${result.due} due.`,
          result.failed > 0 ? "error" : "success",
        );
      }
      await refreshDueCount();
      if (itemId !== null) refreshPublishJobs(itemId);
      refreshItems(activeAccountId);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setPublishingDue(false);
    }
  };

  const publishDueNow = async () => {
    // If any due reel would post too soon after its account's last post, let the
    // user choose (reschedule / publish anyway / cancel) instead of silently
    // posting. With no such conflict, a plain confirm is enough.
    const warnings = await bridge
      .duePublishRecency()
      .catch(() => [] as DueRecencyWarning[]);
    if (warnings.length > 0) {
      setDueDialog(warnings);
      return;
    }
    if (
      !window.confirm(
        `Post ${dueCount} due scheduled reel(s) to Instagram now? This publishes live.`,
      )
    )
      return;
    await runPublishDue(false);
  };

  const toggleAutoPublish = async (next: boolean) => {
    if (
      next &&
      !window.confirm(
        "Turn on auto-publish? While NicheFlow Studio is open, scheduled reels will post to Instagram automatically once their time passes. You can use any screen.",
      )
    )
      return;
    try {
      const { enabled } = await bridge.setAutoPublish(next);
      setAutoPublish(enabled);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  };

  // Load the auto-publish toggle + due count on mount.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      bridge
        .getAutoPublish()
        .then((r) => setAutoPublish(r.enabled))
        .catch(() => undefined);
      void refreshDueCount();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refreshDueCount]);

  // Pull cloud job outcomes (the Worker posts off-PC) into the local list on a
  // timer, and refresh the table when something changed. No-op unless cloud
  // publishing is configured, so this is free for accounts on the local path.
  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      bridge
        .syncCloudPublishJobs()
        .then((result) => {
          if (!cancelled && result.updated > 0) refreshItems(activeAccountId);
        })
        .catch(() => undefined);
    };
    tick();
    const id = window.setInterval(tick, 30000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [activeAccountId, refreshItems]);

  // Poll for newer revisions (Codex writes, or another window's edits).
  useEffect(() => {
    if (itemId === null) return;
    const timer = window.setInterval(async () => {
      try {
        const latest = await bridge.getLatestRevision(itemId);
        if (!latest) return;
        const loaded = loadedRef.current;
        if (loaded && latest.revision_number <= loaded.revision_number) return;
        if (dirtyRef.current) {
          setPendingRevision(latest);
        } else {
          loadRevisionIntoEditor(latest);
        }
      } catch {
        // Transient bridge errors during polling are non-fatal; try again next tick.
      }
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [itemId, loadRevisionIntoEditor]);

  const updateTitle = (index: number, value: string) =>
    setEdits((prev) => {
      const titles = [...prev.titles];
      titles[index] = value;
      return { ...prev, titles };
    });

  const updateCaption = (index: number, value: string) =>
    setEdits((prev) => {
      const captions = [...prev.captions];
      captions[index] = value;
      return { ...prev, captions };
    });

  // The chat handoff now returns three titles and ONE caption that works under
  // any of them, fanned out across the option slots on import. Editing it in
  // one place keeps every slot in sync, so Apply still writes a matching
  // title/caption pair whichever option the user picks.
  const updateSharedCaption = (value: string) =>
    setEdits((prev) => ({ ...prev, captions: prev.captions.map(() => value) }));

  const sharedCaption = sharedCaptionOf(edits.captions);

  const generate = async () => {
    if (itemId === null) return;
    setGenerating(true);
    setActionError(null);
    try {
      const { job_id } = await bridge.startGeneration(itemId, {
        clip_premise: workflow?.clip_premise ?? "",
        caption_style: workflow?.caption_style ?? null,
        title_style: workflow?.title_style || null,
        title_length: workflow?.title_length ?? "long",
      });
      const revision = (await waitForJob(job_id)) as DraftRevision;
      // Respect dirty-edit protection: don't clobber unsaved edits.
      if (dirtyRef.current) {
        setPendingRevision(revision);
      } else {
        loadRevisionIntoEditor(revision);
      }
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setGenerating(false);
    }
  };

  const copyChatPrompt = async () => {
    if (itemId === null) return;
    setActionError(null);
    setHandoffMessage(null);
    try {
      const { prompt } = await bridge.buildChatPrompt(itemId, workflowPayload(workflow));
      await bridge.copyTextToClipboard(prompt);
      setHandoffMessage("Chat prompt copied. Paste it into ChatGPT, Claude, Codex, or Claude Code.");
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  };

  // Persist a workflow field change immediately so style/template selections are
  // "sticky": they survive switching clips and restarting the app without needing
  // the Save button. Account-scoped (mirrors the backend), fire-and-forget so the
  // dropdown stays snappy. Clip premise stays manual — it is per-clip free text.
  const applyWorkflowChange = (patch: Partial<WorkflowSettings>) => {
    if (itemId === null || !workflow) return;
    const next = { ...workflow, ...patch };
    setWorkflow(next);
    void bridge.saveWorkflowSettings(itemId, workflowPayload(next)).catch(() => undefined);
  };

  const saveWorkflow = async () => {
    if (itemId === null || !workflow) return;
    setBusy(true);
    setActionError(null);
    try {
      setWorkflow(await bridge.saveWorkflowSettings(itemId, workflowPayload(workflow)));
      setHandoffMessage("Workflow settings saved.");
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const saveFinalDraft = async () => {
    if (itemId === null) return;
    setBusy(true);
    setActionError(null);
    try {
      await bridge.saveFinalDraft(itemId, finalTitle, finalCaption);
      const ctx = await bridge.getContext(itemId);
      setContext(ctx);
      setHandoffMessage("Selected title and caption saved.");
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  // --- Niche review / cleanup (keep the shared pool + candidate queue clean) --- //

  const removeFromPool = async () => {
    if (itemId === null) return;
    if (
      !window.confirm(
        "Remove this clip's footage from its niche pool? It stops distributing but is reversible from Pool & Distribute.",
      )
    )
      return;
    setBusy(true);
    setActionError(null);
    setHandoffMessage(null);
    try {
      const result = await bridge.removeItemFromPool(itemId);
      setHandoffMessage(
        result.removed_pool_items > 0
          ? `Removed from pool (${result.removed_pool_items} item${result.removed_pool_items === 1 ? "" : "s"}).`
          : "This clip wasn't in any pool.",
      );
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const rejectClip = async (targetItemId: number = itemId ?? 0) => {
    if (!targetItemId) return;
    const reasonLabel = REVIEW_REASON_LABELS[reviewReason] ?? reviewReason;
    const blocksFutureScrapes = reviewReason === "ad_campaign";
    if (
      !window.confirm(
        blocksFutureScrapes
          ? `Block this clip as "${reasonLabel}"? It will be hidden, removed from pools, and saved to the blocked-assets list so future scrapes skip it.`
          : `Reject this clip as "${reasonLabel}"? It ignores the originating candidate and removes the footage from the pool. Reversible.`,
      )
    )
      return;
    setBusy(true);
    setActionError(null);
    setHandoffMessage(null);
    try {
      // Stop any in-flight export for this clip first: killing the render (and
      // the auto-schedule it would trigger) before flipping review state means a
      // rejected clip can't finish exporting and slip back into the schedule. The
      // backend reject then unschedules any schedule/cloud job already committed.
      await cancelExport(targetItemId);
      if (blocksFutureScrapes) {
        const result = await bridge.rejectItemGlobally(targetItemId, reasonLabel);
        const list = await bridge.listLibraryItems(activeAccountId);
        setItems(list);
        setHandoffMessage(
          `Blocked as ${reasonLabel} - removed ${result.removed_pool_items} pool item(s), ` +
            `dropped ${result.dropped_assignments} assignment(s), and future scrapes will skip it.`,
        );
        if (targetItemId === itemId) {
          if (list.length > 0) {
            const nextItem = list.find((row) => row.id !== targetItemId) ?? list[0];
            const ctx = await bridge.getContext(nextItem.id);
            applyContext(ctx);
          } else {
            setContext(null);
          }
        }
        return;
      }
      const result = await bridge.rejectItem(targetItemId, reviewReason);
      setHandoffMessage(
        `Rejected — ${result.rejected_candidates} candidate(s) ignored, ${result.removed_pool_items} pool item(s) removed.`,
      );
      // Reflect the new 'skipped' status in the list and the loaded context.
      const list = await bridge.listLibraryItems(activeAccountId);
      setItems(list);
      if (targetItemId === itemId) {
        const ctx = await bridge.getContext(targetItemId);
        setContext(ctx);
      }
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const restoreClip = async (targetItemId: number) => {
    if (!targetItemId) return;
    setBusy(true);
    setActionError(null);
    setHandoffMessage(null);
    try {
      // Setting status back to pending_review clears the rejected review_state on
      // the backend (see publish_queue.set_processing_status), so the clip becomes
      // openable again instead of snapping back to "rejected" on refresh.
      await bridge.setProcessingStatus(targetItemId, "pending_review");
      const list = await bridge.listLibraryItems(activeAccountId);
      setItems(list);
      setHandoffMessage("Restored to Pending review.");
      if (targetItemId === itemId) {
        const ctx = await bridge.getContext(targetItemId);
        setContext(ctx);
      }
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const rejectGlobally = async () => {
    if (itemId === null) return;
    if (
      !window.confirm(
        "Globally reject this clip? It's removed from the pool and every account, " +
          "blocklisted so future scrapes skip it, and hidden from Processing. " +
          "The local file is kept.",
      )
    )
      return;
    setBusy(true);
    setActionError(null);
    setHandoffMessage(null);
    try {
      const result = await bridge.rejectItemGlobally(itemId);
      // The item is now hidden; reload the list and open the first remaining one.
      const list = await bridge.listLibraryItems(activeAccountId);
      setItems(list);
      setHandoffMessage(
        `Globally rejected & blocklisted — removed ${result.removed_pool_items} pool item(s), ` +
          `dropped ${result.dropped_assignments} assignment(s).`,
      );
      if (list.length > 0) {
        const ctx = await bridge.getContext(list[0].id);
        applyContext(ctx);
      } else {
        setContext(null);
      }
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  // Refill this niche's accounts from the pool without leaving Processing — the
  // same engagement-ranked auto-distribute as Pool & Distribute. Handy right after
  // rejecting a gone clip: the freed slot restocks in place. Runs on `distributing`
  // (not `busy`) so the user can keep editing the open item while it works.
  const distributeActiveNiche = async () => {
    const niche = context?.account?.niche;
    if (!niche) {
      pushToast(
        "This account has no niche set — assign one in Account settings first.",
        "error",
      );
      return;
    }
    const nicheLabel = context?.account?.niche_label ?? niche;
    if (
      !window.confirm(
        `Distribute the ${nicheLabel} pool now?\n\nRanks undistributed clips by engagement ` +
          "and tops up each account in this niche. Footage downloads in the background.",
      )
    )
      return;
    setDistributing(true);
    setActionError(null);
    setHandoffMessage(null);
    try {
      const result = await bridge.distributeNiche(niche);
      // Newly-distributed clips only reach this account's list once fetched, but
      // refresh anyway so any already-on-disk assignments show up immediately.
      refreshItems(activeAccountId);
      if (result.assigned === 0) {
        const reasonMessages: Record<string, string> = {
          no_accounts: `No accounts are set to the "${nicheLabel}" niche — assign accounts first in Account settings.`,
          no_ready_accounts: `No "${nicheLabel}" accounts are currently ready to publish.`,
          all_at_cap:
            "All accounts already hold their daily backlog target — they'll auto-top-up as posts drain it.",
          pool_empty: "Pool is fully distributed — no unused clips remain.",
        };
        pushToast(reasonMessages[result.reason ?? ""] ?? "Nothing to distribute.", "info");
      } else {
        const breakdown = result.accounts
          .map(
            (a) =>
              `${a.account_name} (${a.count}/${a.target}${a.pinned ? `, ${a.pinned} pinned` : ""})`,
          )
          .join(", ");
        pushToast(
          `✓ Distributed ${result.assigned} clip(s) — ${breakdown}. Footage downloads in the background.`,
          "success",
        );
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      setActionError(message);
      pushToast(`Distribute failed: ${message}`, "error");
    } finally {
      setDistributing(false);
    }
  };

  const pasteDraft = async () => {
    if (itemId === null) return;
    setBusy(true);
    setActionError(null);
    setHandoffMessage(null);
    try {
      const text = await bridge.readClipboardText();
      if (!text.trim()) throw new Error("Clipboard is empty. Copy the generated draft first.");
      const revision = await bridge.importPastedDraft(itemId, text);
      loadRevisionIntoEditor(revision);
      setHandoffMessage(`Imported clipboard draft as revision ${revision.revision_number}.`);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const draftableItems = items.filter(
    (candidate) =>
      !candidate.has_draft &&
      candidate.status !== "posted" &&
      candidate.status !== "skipped" &&
      candidate.review_state !== "rejected",
  );

  // Hand-picked batch: when the user ticks rows, those drive the batch (in list
  // order) instead of the auto "first N draftless" window — this is what lets
  // them re-pick an already-drafted reel (e.g. #143) to regenerate it. Derived
  // from the full `items` so a pick survives search/status filtering.
  const manualBatchItems = items.filter((candidate) => manualBatchIds.includes(candidate.id));
  const isManualBatch = manualBatchItems.length > 0;
  const selectedBatchItems = isManualBatch
    ? manualBatchItems
    : draftableItems.slice(0, Math.max(1, batchSize));
  const selectedBatchIds = selectedBatchItems.map((candidate) => candidate.id);

  const toggleBatchSelection = (id: number) =>
    setManualBatchIds((prev) =>
      prev.includes(id) ? prev.filter((existing) => existing !== id) : [...prev, id],
    );

  const prepareBatchDraft = async () => {
    if (!selectedBatchIds.length) {
      setActionError("No draftable reels found for this account.");
      return;
    }
    setBatchBusy(true);
    setActionError(null);
    setBatchResult(null);
    setHandoffMessage(null);
    try {
      // Reels with no local video file fail crop_preview_frame downstream.
      // Download them one at a time (never parallel — Instagram throttling)
      // before handing the batch to prepareAccountBatchFrames.
      const missing = selectedBatchItems.filter((candidate) => !candidate.has_file);
      for (const [index, candidate] of missing.entries()) {
        const label = `#${candidate.account_seq ?? candidate.id}`;
        try {
          setHandoffMessage(`Downloading reel ${index + 1}/${missing.length} (${label})...`);
          const { job_id } = await bridge.startItemDownload(candidate.id);
          await waitForJob(job_id, (_value, message) =>
            setHandoffMessage(
              `Downloading reel ${index + 1}/${missing.length} (${label}): ${message}`,
            ),
          );
        } catch (err: unknown) {
          throw new Error(
            `Reel ${label}: ${err instanceof Error ? err.message : String(err)} ` +
              `Untick it or reject it, then Prepare again.`,
          );
        }
      }
      if (missing.length) refreshItems(activeAccountId);

      const frames = await bridge.prepareAccountBatchFrames(selectedBatchIds);
      const { prompt } = await bridge.buildAccountBatchChatPrompt(
        activeAccountId,
        selectedBatchIds,
        workflowPayload(workflow),
      );
      await bridge.copyTextToClipboard(prompt);
      await bridge.openBatchFramesFolder(frames.folder);
      // Pin exactly what this prompt was built from so a later Import lands on
      // these reels even if the draftable window shifts in the meantime.
      setPreparedBatch(
        selectedBatchItems.map((candidate) => ({
          id: candidate.id,
          label: `#${candidate.account_seq ?? candidate.id}${
            candidate.title ? ` ${candidate.title}` : ""
          }`,
        })),
      );
      setHandoffMessage(
        `Batch prompt copied and ${frames.frames.length} frame(s) opened. Attach the frames in ChatGPT or Claude, paste the prompt, then paste the reply back.`,
      );
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBatchBusy(false);
    }
  };

  const importBatchDraft = async () => {
    // Route against the PINNED batch when present, so REEL <n> maps to the item
    // that reel was generated for, not the live window (which may have slid).
    // Fall back to the live selection only when nothing is pinned (e.g. import
    // after an app restart cleared the in-memory snapshot).
    const targetIds = preparedBatch ? preparedBatch.map((entry) => entry.id) : selectedBatchIds;
    if (!targetIds.length) {
      setActionError("No draftable reels found for this account.");
      return;
    }
    setBatchBusy(true);
    setActionError(null);
    setHandoffMessage(null);
    try {
      const text = await bridge.readClipboardText();
      if (!text.trim()) throw new Error("Clipboard is empty. Copy the ChatGPT or Claude reply first.");
      const result = await bridge.importAccountBatchDraft(text, targetIds);
      setBatchResult(result);
      setHandoffMessage(
        `Batch imported ${result.imported.length} reel(s), ${result.failed.length} failed, ${result.unmatched.length} unmatched.`,
      );
      // Keep the pin when something did not land cleanly so a corrected reply
      // can be re-imported to the same reels; clear it only on a clean import.
      if (!result.failed.length && !result.unmatched.length) {
        setPreparedBatch(null);
        setManualBatchIds([]);
      }
      refreshItems(activeAccountId);
      if (itemId !== null && result.imported.includes(itemId)) {
        const ctx = await bridge.getContext(itemId);
        applyContext(ctx);
      }
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBatchBusy(false);
    }
  };

  // Flip the ACTIVE ACCOUNT's persisted auto-schedule setting from the export
  // footer, so the choice is visible where the export happens instead of only
  // in Account Manager. Per account, saved in the DB.
  const toggleAutoScheduleOnExport = async () => {
    const voice = context?.account;
    if (!voice) return;
    const next = !voice.auto_schedule_on_export;
    try {
      await bridge.updateAccount(voice.id, { auto_schedule_on_export: next });
      setContext((ctx) =>
        ctx && ctx.account
          ? { ...ctx, account: { ...ctx.account, auto_schedule_on_export: next } }
          : ctx,
      );
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  };

  const exportReel = async () => {
    if (itemId === null) return;
    // Pin the id: the user may select another item while this export runs in
    // the background, and every completion write below must target THIS item
    // (or be skipped when the user has moved on) — never the current selection.
    const exportItemId = itemId;
    setActionError(null);
    setExportedPath(null);
    setWatermarkStatus(null);
    setExportPreviewError(null);
    setExportJobs((jobs) => ({ ...jobs, [exportItemId]: { value: 0, message: "Starting…" } }));
    try {
      const { job_id } = await bridge.startExport(exportItemId);
      // Remember the job id so "Cancel export" (or a reject of this item) can
      // stop it while it runs in the background.
      exportJobIdsRef.current[exportItemId] = job_id;
      // Export jobs include the auto-schedule cloud upload, so they get the
      // long upload window, not the generic job timeout.
      const result = (await waitForJob(
        job_id,
        (value, message) =>
          setExportJobs((jobs) => ({ ...jobs, [exportItemId]: { value, message } })),
        UPLOAD_TIMEOUT_MS,
      )) as ExportResult;
      // The scheduling warning must survive the user moving to another item or
      // niche mid-export: an exported-but-unscheduled reel is otherwise silent.
      if (result.warning) {
        setActionError(
          `Exported successfully, but auto-scheduling needs attention: ${result.warning} ` +
            `(see "not scheduled yet" on the Publishing Dashboard)`,
        );
      }
      if (itemIdRef.current === exportItemId) {
        setExportedPath(result.processed_path);
        setWatermarkStatus({
          replaced: Boolean(result.watermark_replaced),
          detected: result.watermark_detected_text ?? null,
          skippedReason: result.watermark_skipped_reason ?? null,
        });
        if (result.scheduled_publish) {
          const scheduled = result.scheduled_publish.scheduled_at
            ? new Date(result.scheduled_publish.scheduled_at).toLocaleString()
            : "the next open slot";
          const scheduleMessage = scheduleStatusMessage(result.scheduled_publish.status, scheduled);
          const statusMessage = `Exported. ${scheduleMessage}`;
          setPublishMessage(statusMessage);
          pushToast(`${activeAccountName ?? "Account"} - ${statusMessage}`, "success");
        }
        // Refetch so the item's processed_path is reflected, and refresh the queue.
        const ctx = await bridge.getContext(exportItemId);
        if (itemIdRef.current === exportItemId) {
          setContext(ctx);
          refreshPublishJobs(exportItemId);
        }
      }
      // Repaint the Videos list ONLY if the user is still on this niche. A
      // background export that finishes after a niche switch must not overwrite
      // the new account's list with this (now-previous) account's items.
      refreshItems(activeAccountId);
    } catch (err: unknown) {
      if (err instanceof JobCanceledError) {
        // User-requested cancel (button or reject). Clean outcome — no error
        // banner; a quiet toast and a list refresh so the row reflects reality.
        pushToast(`${activeAccountName ?? "Account"} - #${exportItemId} export canceled.`, "info");
        refreshItems(activeAccountId);
      } else if (err instanceof JobTimeoutError) {
        // Poller gave up; the export/schedule keeps running in the backend and
        // the row updates via the periodic refresh. Not a failure.
        pushToast(
          `${activeAccountName ?? "Account"} - #${exportItemId} export is still running in ` +
            `the background. The list updates automatically when it finishes.`,
          "info",
        );
      } else if (itemIdRef.current === exportItemId) {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      delete exportJobIdsRef.current[exportItemId];
      setExportJobs((jobs) => {
        const next = { ...jobs };
        delete next[exportItemId];
        return next;
      });
    }
  };

  // Cancel an in-flight export for one item: kills the FFmpeg render and stops
  // the export job before it can auto-schedule. Best-effort — if the job already
  // finished there's nothing to cancel. The running exportReel poller observes
  // the "canceled" status and clears its own progress/state.
  const cancelExport = async (targetItemId: number) => {
    const jobId = exportJobIdsRef.current[targetItemId];
    if (!jobId) return;
    // Reflect intent immediately so the button doesn't look unresponsive while
    // the render process is torn down.
    setExportJobs((jobs) =>
      targetItemId in jobs
        ? { ...jobs, [targetItemId]: { ...jobs[targetItemId], message: "Canceling…" } }
        : jobs,
    );
    try {
      await bridge.cancelJob(jobId);
    } catch {
      // Best-effort: the job may already be finishing. The poller settles state.
    }
  };

  const apply = async (optionNumber: number) => {
    if (itemId === null) return;
    setBusy(true);
    setActionError(null);
    try {
      const applied = await bridge.applyRevision(itemId, optionNumber, loadedRevision?.id ?? null);
      setFinalTitle(applied.title_draft);
      setFinalCaption(applied.caption_draft);
      const ctx = await bridge.getContext(itemId);
      setContext(ctx);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const saveEdits = async () => {
    if (itemId === null) return;
    setBusy(true);
    setActionError(null);
    try {
      const saved = await bridge.saveRevision(itemId, {
        title_options: edits.titles,
        caption_options: edits.captions,
        summary: loadedRevision?.summary ?? null,
        option_notes: loadedRevision?.option_notes ?? null,
        option_tiers: loadedRevision?.option_tiers ?? null,
        source: "ui",
      });
      loadRevisionIntoEditor(saved);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (loadError) {
    return (
      <div className="mx-auto max-w-2xl p-8">
        <Card>
          <CardHeader>
            <CardTitle>Could not load Processing</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">{loadError}</CardContent>
        </Card>
      </div>
    );
  }

  if (!context) {
    if (itemsLoaded && items.length === 0) {
      return (
        <div className="mx-auto max-w-2xl p-8">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">No videos for this account</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              {activeAccountName ? `"${activeAccountName}" has` : "This account has"} no downloaded
              clips yet. Scrape or import clips in the desktop app (and assign them to this
              account), then they'll appear here.
            </CardContent>
          </Card>
        </div>
      );
    }
    return <div className="p-8 text-sm text-muted-foreground">Loading…</div>;
  }

  const { item, account } = context;
  const appliedIndex = loadedRevision?.applied_title_index ?? null;
  const previewUrl = item.original_preview_url;
  const filteredItems = items.filter((candidate) => {
    // Deep link from the schedule: show only the pinned item, regardless of the
    // status filter or search text (status may be anything; title won't match).
    if (focusItemId != null) return candidate.id === focusItemId;
    // "Cloud" covers both the handed-off reels and the ones still uploading —
    // they're the same publish path, so splitting them across two filter entries
    // would just hide half a cloud account's queue behind the wrong option.
    const matchesStatus =
      itemFilter === "all" ||
      candidate.status === itemFilter ||
      (itemFilter === "cloud" && candidate.status === "cloud_pending");
    const search = itemSearch.trim().toLowerCase();
    const matchesSearch =
      !search ||
      (candidate.account_seq != null && String(candidate.account_seq).includes(search)) ||
      String(candidate.id).includes(search) ||
      (candidate.title ?? "").toLowerCase().includes(search) ||
      candidate.source_url.toLowerCase().includes(search);
    return matchesStatus && matchesSearch;
  });

  return (
    <div className="mx-auto max-w-7xl space-y-4 p-6">
      <header className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-semibold tracking-tight">Processing</h1>
            {!isDesktop && <Badge variant="outline">browser preview</Badge>}
            {dirty && <Badge variant="destructive">unsaved edits</Badge>}
          </div>
          <p className="text-sm text-muted-foreground">
            {activeAccountName ?? account?.name ?? "no account"}
            {account?.niche_label ? ` · ${account.niche_label}` : ""}
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={distributeActiveNiche}
          disabled={distributing || !account?.niche}
          title="Refill this niche's accounts from the pool — same as Pool & Distribute on the Dashboard"
        >
          {distributing ? "Distributing…" : "Distribute"}
        </Button>
      </header>

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(300px,0.8fr)_minmax(360px,1.2fr)]">
        <section className="overflow-hidden rounded-xl border bg-card">
          <div className="space-y-3 border-b p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-semibold">Videos</p>
                <p className="text-xs text-muted-foreground">{filteredItems.length} shown</p>
              </div>
              <Badge variant="secondary">{items.length} total</Badge>
            </div>
            <div className="grid grid-cols-[1fr_120px_120px] gap-2">
              <input
                className="h-9 min-w-0 rounded-md border border-input bg-transparent px-3 text-sm"
                placeholder="Search videos..."
                value={itemSearch}
                onChange={(event) => {
                  setItemSearch(event.target.value);
                  setFocusItemId(null);
                }}
              />
              <select
                className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
                value={itemFilter}
                onChange={(event) => {
                  setItemFilter(event.target.value);
                  setFocusItemId(null);
                }}
              >
                <option value="all">All</option>
                <option value="new">New</option>
                <option value="pending_review">Pending review</option>
                <option value="draft">Draft</option>
                <option value="exported">Exported</option>
                <option value="scheduled">Scheduled</option>
                <option value="cloud">Cloud</option>
                <option value="posted">Posted</option>
                <option value="failed">Failed</option>
                <option value="skipped">Skipped</option>
                <option value="rejected">Rejected</option>
              </select>
              <select
                className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
                value={reviewReason}
                title="Reason used by the row Reject buttons"
                onChange={(event) => setReviewReason(event.target.value)}
              >
                <option value="wrong_niche">Wrong niche</option>
                <option value="ad_campaign">Ad / promo</option>
                <option value="low_quality">Low quality</option>
                <option value="duplicate">Duplicate</option>
              </select>
            </div>
            {focusItemId != null && (
              <div className="flex items-center justify-between gap-2 rounded-md border border-ring/40 bg-accent/40 px-3 py-2 text-xs">
                <span className="text-muted-foreground">
                  {filteredItems.length === 1
                    ? "Showing the reel linked from the schedule."
                    : "Linked reel not found in this account's list."}
                </span>
                <button
                  type="button"
                  className="font-medium text-foreground underline-offset-2 hover:underline"
                  onClick={() => setFocusItemId(null)}
                >
                  Show all
                </button>
              </div>
            )}
          </div>
          <div className="max-h-[600px] overflow-auto">
            <table className="w-full table-fixed text-left text-sm">
              <thead className="sticky top-0 bg-muted text-xs text-muted-foreground">
                <tr>
                  <th className="w-10 px-2 py-2 text-center font-medium" title="Tick to add to batch">
                    Batch
                  </th>
                  <th className="w-36 px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Title</th>
                  <th className="w-24 px-3 py-2 font-medium">Added</th>
                  <th className="w-20 px-3 py-2 text-right font-medium">Action</th>
                </tr>
              </thead>
              <tbody>
                {filteredItems.map((candidate) => {
                  const meta = statusMeta(candidate.status);
                  const rejected = candidate.review_state === "rejected";
                  const posted = candidate.status === "posted";
                  return (
                    <tr
                      key={candidate.id}
                      className={`cursor-pointer border-t transition-colors hover:bg-accent ${
                        candidate.id === item.id ? "bg-accent" : ""
                      }`}
                      onClick={() => switchItem(candidate.id)}
                    >
                      <td
                        className="px-2 py-1 text-center"
                        onClick={(event) => event.stopPropagation()}
                      >
                        <input
                          type="checkbox"
                          className="h-4 w-4 cursor-pointer align-middle"
                          checked={manualBatchIds.includes(candidate.id)}
                          onChange={() => toggleBatchSelection(candidate.id)}
                          title="Add this reel to the batch"
                        />
                      </td>
                      <td className="px-2 py-1" onClick={(event) => event.stopPropagation()}>
                        <span className="flex items-center gap-1.5">
                          <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${meta.dot}`} />
                          <select
                            className="h-8 min-w-0 flex-1 rounded-md border border-input bg-background px-1 text-xs"
                            value={candidate.status}
                            disabled={
                              updatingStatusIds[candidate.id] || isQueuedStatus(candidate.status)
                            }
                            title={
                              isQueuedStatus(candidate.status)
                                ? "Cancel the schedule before changing this status"
                                : "Change this video's workflow status"
                            }
                            onChange={(event) =>
                              void setProcessingStatus(candidate, event.target.value)
                            }
                          >
                            {!MANUAL_STATUS_OPTIONS.some(
                              (option) => option.value === candidate.status,
                            ) && <option value={candidate.status}>{meta.label}</option>}
                            {MANUAL_STATUS_OPTIONS.map((option) => (
                              <option key={option.value} value={option.value}>
                                {option.label}
                              </option>
                            ))}
                            {/* Only a reopened posted item can be set back to Posted
                                (undoing the reopen); see _revert_reopen_to_posted. */}
                            {candidate.reopened && <option value="posted">Posted</option>}
                          </select>
                        </span>
                      </td>
                      <td
                        className="truncate px-3 py-2"
                        title={candidate.title ?? candidate.source_url}
                      >
                        <span className="flex items-center gap-2">
                          {candidate.is_new && (
                            <Badge variant="default" className="px-1.5 py-0 text-[10px]">
                              NEW
                            </Badge>
                          )}
                          <span className="truncate">
                            #{candidate.account_seq ?? candidate.id}{" "}
                            {candidate.title ?? candidate.source_url}
                          </span>
                        </span>
                      </td>
                      <td className="px-3 py-2 text-xs text-muted-foreground">
                        {shortDate(candidate.created_at)}
                      </td>
                      <td
                        className="px-2 py-1 text-right"
                        onClick={(event) => event.stopPropagation()}
                      >
                        {posted ? (
                          <span className="text-xs text-muted-foreground">Posted</span>
                        ) : rejected ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2 text-xs"
                            disabled={busy}
                            title="Restore this clip to Pending review"
                            onClick={() => void restoreClip(candidate.id)}
                          >
                            Restore
                          </Button>
                        ) : (
                          <Button
                            size="sm"
                            variant="destructive"
                            className="h-7 px-2 text-xs"
                            disabled={busy}
                            title={`Reject as ${REVIEW_REASON_LABELS[reviewReason] ?? reviewReason}`}
                            onClick={() => void rejectClip(candidate.id)}
                          >
                            Reject
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        <section className="overflow-hidden rounded-xl bg-zinc-950">
          <div className="flex items-center justify-between border-b border-white/10 px-4 py-3 text-white">
            <div>
              <p className="text-xs font-medium uppercase tracking-wider text-zinc-400">
                Preview
              </p>
              <p className="mt-1 text-sm">Original video</p>
            </div>
          </div>
          <div className="flex min-h-[600px] items-center justify-center bg-black p-4">
            <div className="flex aspect-[9/16] max-h-[568px] w-full max-w-[320px] items-center justify-center overflow-hidden bg-black">
              {cropOpen ? (
                <p className="px-4 text-center text-sm text-zinc-400">
                  Original preview is paused while adjusting the crop below.
                </p>
              ) : previewUrl && !previewError ? (
                <video
                  key={previewUrl}
                  className="h-full w-full object-contain"
                  src={previewUrl}
                  controls
                  preload="metadata"
                  onError={(event) => {
                    const code = event.currentTarget.error?.code;
                    const message = event.currentTarget.error?.message;
                    setPreviewError(
                      `Media error${code ? ` ${code}` : ""}${message ? `: ${message}` : ""}`,
                    );
                  }}
                />
              ) : (
                <p className="px-4 text-center text-sm text-zinc-400">
                  {previewError
                    ? `The selected video could not be loaded. ${previewError}`
                    : "This item has no local video file to preview."}
                </p>
              )}
            </div>
          </div>
        </section>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Niche review &amp; cleanup</CardTitle>
          <p className="text-sm text-muted-foreground">
            Off-niche, or a clip that slipped past dedup? Remove its footage from the shared pool,
            reject it (also ignores the candidate), or globally reject it — which blocklists the
            footage so it never re-pools and hides it here.
          </p>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          <Button variant="outline" disabled={busy} onClick={removeFromPool}>
            Remove from pool
          </Button>
          <Button variant="outline" disabled={busy} onClick={rejectGlobally}>
            Reject globally (block)
          </Button>
          <div className="grow" />
          <select
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
            value={reviewReason}
            onChange={(event) => setReviewReason(event.target.value)}
          >
            <option value="wrong_niche">Wrong niche</option>
            <option value="low_quality">Low quality</option>
            <option value="duplicate">Duplicate (dedup missed)</option>
            <option value="ad_campaign">Ad / promo</option>
          </select>
          <Button variant="destructive" disabled={busy} onClick={() => void rejectClip()}>
            Reject clip
          </Button>
        </CardContent>
      </Card>

      {workflow && (
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <div>
              <CardTitle className="text-base">Workflow Settings</CardTitle>
              <p className="mt-1 text-sm text-muted-foreground">
                These values guide Groq and the copied Codex/Claude prompt.
              </p>
            </div>
            <Button variant="outline" onClick={() => bridge.openItemFolder(item.id)}>
              Open Folder
            </Button>
          </CardHeader>
          <CardContent className="space-y-4">
            <label className="block space-y-1">
              <span className="text-sm font-medium">Clip premise</span>
              <textarea
                className="min-h-20 w-full rounded-md border border-input bg-transparent p-3 text-sm"
                placeholder="Optional direction, context, joke, or anomaly for the AI..."
                value={workflow.clip_premise}
                onChange={(event) =>
                  setWorkflow({ ...workflow, clip_premise: event.target.value })
                }
              />
            </label>
            <div className="grid gap-3 md:grid-cols-4">
              <label className="space-y-1">
                <span className="text-sm font-medium">Caption Style</span>
                <select
                  className="h-10 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                  value={workflow.caption_style}
                  onChange={(event) =>
                    applyWorkflowChange({ caption_style: event.target.value })
                  }
                >
                  {workflow.caption_style_options.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label className="space-y-1">
                <span className="text-sm font-medium">Title Length</span>
                <select
                  className="h-10 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                  value={workflow.title_length}
                  onChange={(event) =>
                    applyWorkflowChange({ title_length: event.target.value })
                  }
                >
                  {workflow.title_length_options.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label className="space-y-1">
                <span className="text-sm font-medium">Title Style</span>
                <select
                  className="h-10 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                  value={workflow.title_style}
                  onChange={(event) =>
                    applyWorkflowChange({ title_style: event.target.value })
                  }
                >
                  {workflow.title_style_options.map((option) => (
                    <option key={option.value || "auto"} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label className="space-y-1">
                <span className="text-sm font-medium">Template</span>
                <select
                  className="h-10 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                  value={workflow.template}
                  onChange={(event) =>
                    applyWorkflowChange({ template: event.target.value })
                  }
                >
                  {workflow.template_options.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
            </div>
            <Button onClick={saveWorkflow} disabled={busy}>Save workflow settings</Button>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Draft Generation</CardTitle>
          <p className="text-sm text-muted-foreground">
            Generate directly with Groq, or use a chat/coding agent and return the result
            through the clipboard or automatic database handoff.
          </p>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          <section className="space-y-3 rounded-lg border p-4">
            <div>
              <p className="font-medium">Generate with Groq API</p>
              <p className="text-sm text-muted-foreground">
                Runs in the background and saves the generated options as a new revision.
              </p>
            </div>
            <Button
              onClick={generate}
              disabled={generating || busy || !canGenerate}
              title={canGenerate ? undefined : "No draft provider configured (set GROQ_API_KEY)"}
            >
              {generating ? "Generating…" : "Generate with Groq"}
            </Button>
            {!canGenerate && (
              <p className="text-xs text-muted-foreground">
                No provider configured. Set GROQ_API_KEY or use the chat prompt path.
              </p>
            )}
          </section>

          <section className="space-y-3 rounded-lg border p-4">
            <div>
              <p className="font-medium">Copy Chat Prompt</p>
              <p className="text-sm text-muted-foreground">
                Use ChatGPT, Claude, Codex, or Claude Code to inspect the local video and
                write the options.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" onClick={copyChatPrompt} disabled={busy}>
                Copy Chat Prompt
              </Button>
              <Button variant="outline" onClick={pasteDraft} disabled={busy}>
                Paste Draft from Clipboard
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              Automatic Codex/Claude Code handoff: ask the agent to save through
              <code className="mx-1">scripts/nicheflow_drafts.py</code>. New revisions
              appear here automatically without pasting.
            </p>
          </section>

          <section className="space-y-3 rounded-lg border p-4 md:col-span-2">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="font-medium">Batch draft (ChatGPT or Claude)</p>
                <p className="text-sm text-muted-foreground">
                  Prepare the next draftless reels for this account, attach the extracted
                  frames in ChatGPT or Claude, then import one pasted reply.
                </p>
              </div>
              <div className="flex flex-wrap items-start gap-3">
                {/* Sits with the batch controls, not the per-item workflow panel:
                    it only changes the prompt this card copies. */}
                {workflow ? (
                  <label className="grid gap-1 text-xs text-muted-foreground">
                    Captions per reel
                    <select
                      className="h-9 w-56 rounded-md border border-input bg-transparent px-2 text-sm text-foreground"
                      value={workflow.caption_mode}
                      title="One shared caption is about 55% fewer output tokens per reel. Importing accepts either shape, so a reply already in flight still works."
                      onChange={(event) =>
                        applyWorkflowChange({ caption_mode: event.target.value })
                      }
                    >
                      {workflow.caption_mode_options.map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                  </label>
                ) : null}
                <label className="grid gap-1 text-xs text-muted-foreground">
                  Batch size
                  <input
                    type="number"
                    min="1"
                    max="20"
                    className="h-9 w-24 rounded-md border border-input bg-transparent px-2 text-sm text-foreground disabled:opacity-50"
                    value={batchSize}
                    disabled={isManualBatch}
                    title={
                      isManualBatch
                        ? "Using your ticked selection; batch size applies only to auto mode."
                        : undefined
                    }
                    onChange={(event) =>
                      setBatchSize(Math.max(1, Number(event.target.value) || 1))
                    }
                  />
                </label>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              {isManualBatch ? (
                <>
                  <span>{selectedBatchItems.length} hand-picked reel(s) selected.</span>
                  <button
                    type="button"
                    className="underline hover:text-foreground"
                    onClick={() => setManualBatchIds([])}
                  >
                    Clear selection
                  </button>
                </>
              ) : (
                <span>
                  {selectedBatchItems.length} selected for the next batch from{" "}
                  {draftableItems.length} draftless reel(s). Tick rows in the list to pick
                  specific reels (including ones that already have a draft).
                </span>
              )}
            </div>
            {selectedBatchItems.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {selectedBatchItems.map((candidate, index) => (
                  <Badge key={candidate.id} variant="secondary" className="max-w-[18rem]">
                    <span className="truncate">
                      Reel {index + 1}: #{candidate.account_seq ?? candidate.id}{" "}
                      {candidate.title ?? ""}
                    </span>
                  </Badge>
                ))}
              </div>
            )}
            {preparedBatch && (
              <div className="space-y-1 rounded-md border border-primary/40 bg-primary/5 p-2">
                <p className="text-xs font-medium text-foreground">
                  Pending import: paste the reply for these reels. Import writes here, not the
                  list above.
                </p>
                <div className="flex flex-wrap gap-1">
                  {preparedBatch.map((entry, index) => (
                    <Badge key={entry.id} variant="outline" className="max-w-[18rem]">
                      <span className="truncate">
                        Reel {index + 1}: {entry.label}
                      </span>
                    </Badge>
                  ))}
                </div>
              </div>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                variant="secondary"
                onClick={prepareBatchDraft}
                disabled={batchBusy || !selectedBatchItems.length}
              >
                {batchBusy ? "Working..." : "Prepare batch"}
              </Button>
              <Button
                variant="outline"
                onClick={importBatchDraft}
                disabled={batchBusy || (!preparedBatch && !selectedBatchItems.length)}
              >
                Import batch results
              </Button>
            </div>
            {batchResult && (
              <div className="space-y-1 rounded-md border border-border bg-muted/30 p-3 text-xs text-muted-foreground">
                <p>Imported: {batchResult.imported.join(", ") || "none"}</p>
                <p>Unmatched: {batchResult.unmatched.join(", ") || "none"}</p>
                {batchResult.failed.map((failure) => (
                  <p key={failure.item_id} className="text-destructive">
                    Item {failure.item_id}: {failure.error}
                  </p>
                ))}
              </div>
            )}
          </section>

          {handoffMessage && (
            <p className="text-sm text-emerald-600 md:col-span-2">{handoffMessage}</p>
          )}
        </CardContent>
      </Card>

      {pendingRevision && (
        <Card className="border-amber-500/50 bg-amber-500/5">
          <CardContent className="flex flex-wrap items-center justify-between gap-2 p-4">
            <span className="text-sm">
              A newer revision (#{pendingRevision.revision_number}) is available. You
              have unsaved edits.
            </span>
            <div className="flex gap-2">
              <Button size="sm" onClick={() => loadRevisionIntoEditor(pendingRevision)}>
                Review update
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setPendingRevision(null)}
              >
                Keep my edits
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {actionError && (
        <div
          role="alert"
          aria-live="assertive"
          className="fixed right-5 top-5 z-50 flex max-w-md items-start gap-3 rounded-lg border border-destructive/60 bg-destructive px-4 py-3 text-sm font-medium text-destructive-foreground shadow-2xl shadow-black/40"
        >
          <span className="grow">{actionError}</span>
          <button
            type="button"
            aria-label="Dismiss warning"
            className="rounded px-1 text-lg leading-none text-destructive-foreground/80 transition hover:bg-black/20 hover:text-destructive-foreground active:scale-90"
            onClick={() => setActionError(null)}
          >
            ×
          </button>
        </div>
      )}

      {loadedRevision?.summary && (
        <p className="text-sm text-muted-foreground">{loadedRevision.summary}</p>
      )}
      {loadedRevision?.recommended_title_index != null && (
        <p className="text-sm">
          <span className="font-medium">Recommended pick:</span>{" "}
          Title Option {loadedRevision.recommended_title_index}
          {loadedRevision.recommended_caption_index != null &&
          loadedRevision.recommended_caption_index !== loadedRevision.recommended_title_index
            ? ` + Caption Option ${loadedRevision.recommended_caption_index}`
            : ""}
          {loadedRevision.recommendation_reason
            ? ` : ${loadedRevision.recommendation_reason}`
            : ""}
        </p>
      )}

      {!loadedRevision ? (
        <Card>
          <CardContent className="p-6 text-sm text-muted-foreground">
            No draft revisions yet. Ask Codex to generate options (or run{" "}
            <code>scripts/nicheflow_drafts.py save</code>) and they will appear here
            automatically.
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="grid gap-4 md:grid-cols-3">
            {edits.titles.map((title, index) => (
              <OptionCard
                key={index}
                optionNumber={index + 1}
                title={title}
                caption={sharedCaption === null ? edits.captions[index] ?? "" : undefined}
                note={loadedRevision.option_notes[index]}
                tier={loadedRevision.option_tiers[index]}
                recommended={loadedRevision.recommended_title_index === index + 1}
                applied={appliedIndex === index + 1}
                busy={busy}
                onTitleChange={(v) => updateTitle(index, v)}
                onCaptionChange={
                  sharedCaption === null ? (v) => updateCaption(index, v) : undefined
                }
                onApply={() => apply(index + 1)}
              />
            ))}
          </div>
          {sharedCaption !== null && (
            <Card className="mt-4">
              <CardContent className="space-y-2 p-4">
                <div className="flex items-baseline justify-between">
                  <label
                    className="text-xs font-medium text-muted-foreground"
                    htmlFor="shared-caption"
                  >
                    Shared caption
                  </label>
                  <span className="text-xs text-muted-foreground">
                    Applies to whichever title you choose
                  </span>
                </div>
                <Textarea
                  id="shared-caption"
                  value={sharedCaption}
                  rows={8}
                  onChange={(e) => updateSharedCaption(e.target.value)}
                  placeholder="Caption"
                />
              </CardContent>
            </Card>
          )}
        </>
      )}

      {loadedRevision && (
        <footer className="flex flex-wrap items-center gap-2">
          <Button onClick={saveEdits} disabled={!dirty || busy}>
            Save edits as new revision
          </Button>
          {dirty && (
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => loadRevisionIntoEditor(loadedRevision)}
            >
              Discard edits
            </Button>
          )}
          <div className="grow" />
          {account && (
            <label
              className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground"
              title={
                account.auto_schedule_on_export
                  ? `Exports auto-schedule into ${account.name}'s next open slot. Saved per account.`
                  : `Exports stay unscheduled; schedule manually from the Publishing Dashboard. Saved per account.`
              }
            >
              <input
                type="checkbox"
                checked={account.auto_schedule_on_export}
                disabled={exporting}
                onChange={() => void toggleAutoScheduleOnExport()}
              />
              Auto-schedule on export
            </label>
          )}
          {exporting && (
            <Button
              variant="outline"
              onClick={() => itemId !== null && void cancelExport(itemId)}
              title="Stop this export — kills the render and skips auto-scheduling"
            >
              Cancel export
            </Button>
          )}
          <Button
            variant="secondary"
            onClick={exportReel}
            disabled={exporting || dirty || isThisItemPublishing}
          >
            {exporting ? "Exporting…" : "Export Reel"}
          </Button>
        </footer>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Selected Title and Caption</CardTitle>
          <p className="text-sm text-muted-foreground">
            Applying an option fills these fields. Edit and save the final text used for export and publishing.
          </p>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 lg:grid-cols-[1fr_minmax(220px,300px)]">
            <div className="space-y-3">
              <label className="block space-y-1">
                <span className="text-sm font-medium">Title</span>
                <textarea
                  className="min-h-20 w-full rounded-md border border-input bg-transparent p-3 text-sm"
                  value={finalTitle}
                  onChange={(event) => setFinalTitle(event.target.value)}
                />
              </label>
              <label className="block space-y-1">
                <span className="text-sm font-medium">Caption</span>
                <textarea
                  className="min-h-48 w-full rounded-md border border-input bg-transparent p-3 text-sm"
                  value={finalCaption}
                  onChange={(event) => setFinalCaption(event.target.value)}
                />
              </label>
              <Button onClick={saveFinalDraft} disabled={busy}>Save selected title and caption</Button>
            </div>
            {/* Exported reel preview, kept beside the final text so the result is
                visible right after Export without scrolling back to the top. */}
            <div className="space-y-2">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                    Exported reel
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Adjust the source crop, then re-export to update this result.
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setCropOpen((open) => !open)}
                  disabled={!item.original_preview_url || isThisItemPublishing}
                  title="Adjust the source crop used for the exported reel"
                >
                  {cropOpen ? "Close crop editor" : "Adjust crop"}
                </Button>
              </div>
              <div className="flex aspect-[9/16] w-full items-center justify-center overflow-hidden rounded-lg border bg-black">
                {cropOpen ? (
                  <p className="px-3 text-center text-xs text-zinc-400">
                    Exported preview is paused while adjusting the source crop.
                  </p>
                ) : item.exported_preview_url && !exportPreviewError ? (
                  <video
                    key={item.exported_preview_url}
                    className="h-full w-full object-contain"
                    src={item.exported_preview_url}
                    controls
                    preload="metadata"
                    onError={(event) => {
                      const code = event.currentTarget.error?.code;
                      const message = event.currentTarget.error?.message;
                      setExportPreviewError(
                        `Media error${code ? ` ${code}` : ""}${message ? `: ${message}` : ""}`,
                      );
                    }}
                  />
                ) : (
                  <p className="px-3 text-center text-xs text-zinc-400">
                    {exportPreviewError
                      ? `Could not load the exported reel. ${exportPreviewError}`
                      : "Export the reel to preview it here."}
                  </p>
                )}
              </div>
            </div>
          </div>

          {cropOpen && item.original_preview_url && (
            <div className="mt-4">
              <CropEditor
                itemId={item.id}
                onClose={() => setCropOpen(false)}
                onSaved={(msg) => {
                  setHandoffMessage(msg);
                  setCropOpen(false);
                  // If the clip was already exported, re-render now so the new
                  // crop is actually applied and the preview refreshes.
                  if (item.processed_path) void exportReel();
                }}
              />
            </div>
          )}
        </CardContent>
      </Card>

      {exportProgress && (
        <div className="space-y-1">
          <div className="h-2 w-full overflow-hidden rounded-full bg-secondary">
            <div
              className="h-full bg-primary transition-[width] duration-300"
              style={{ width: `${Math.round(exportProgress.value * 100)}%` }}
            />
          </div>
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-muted-foreground">{exportProgress.message}</p>
            <Button
              size="sm"
              variant="outline"
              className="h-7 shrink-0 px-2 text-xs"
              onClick={() => itemId !== null && void cancelExport(itemId)}
            >
              Cancel export
            </Button>
          </div>
        </div>
      )}

      {itemDownload && (
        <div className="space-y-1">
          <div className="h-2 w-full overflow-hidden rounded-full bg-secondary">
            <div
              className="h-full bg-primary transition-[width] duration-300"
              style={{ width: `${Math.round(itemDownload.value * 100)}%` }}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            Fetching original clip — {itemDownload.message}
          </p>
        </div>
      )}

      {exportedPath && !exporting && (
        <p className="text-sm text-emerald-600">Exported: {exportedPath}</p>
      )}

      {watermarkStatus && !exporting && watermarkStatus.replaced && (
        <p className="text-sm text-emerald-600">
          Watermark covered: @{(watermarkStatus.detected ?? "").replace(/^@+/, "") || "handle"}
        </p>
      )}

      {watermarkStatus &&
        !exporting &&
        !watermarkStatus.replaced &&
        watermarkStatus.skippedReason === "no watermark detected" && (
          <p className="text-sm text-muted-foreground">No watermark detected</p>
        )}

      {dirty && (
        <p className="text-xs text-muted-foreground">
          Save or discard your edits before exporting.
        </p>
      )}

      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <CardTitle className="text-base">Publish</CardTitle>
          {!item.processed_path && (
            <span className="text-xs text-muted-foreground">export the reel first</span>
          )}
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              disabled={!item.processed_path || busy || isThisItemPublishing || isThisItemScheduling}
              onClick={() => queueForPublish(null)}
            >
              Add to Publish Queue
            </Button>
            <Button
              variant="secondary"
              disabled={!item.processed_path || busy || isThisItemPublishing || isThisItemScheduling}
              onClick={autoScheduleForPublish}
            >
              {isThisItemScheduling ? (
                <>
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                  {thisItemScheduleMessage}
                </>
              ) : (
                "Auto Schedule"
              )}
            </Button>
            <input
              type="datetime-local"
              className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
              value={scheduleAt}
              onChange={(e) => setScheduleAt(e.target.value)}
            />
            <Button
              disabled={
                !item.processed_path ||
                !scheduleAt ||
                busy ||
                isThisItemPublishing ||
                isThisItemScheduling
              }
              onClick={() => queueForPublish(scheduleAt)}
            >
              {isThisItemScheduling ? (
                <>
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                  {thisItemScheduleMessage}
                </>
              ) : (
                "Schedule"
              )}
            </Button>
            <Button
              disabled={!item.processed_path || busy || isThisItemPublishing || isThisItemScheduling}
              onClick={publishNow}
            >
              {thisItemPublishMessage ?? "Publish Now"}
            </Button>
            <Button
              variant="outline"
              disabled={!item.processed_path || busy || isThisItemPublishing || isThisItemScheduling}
              onClick={publishViaBrowser}
            >
              Publish via Browser
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-3 border-t border-border pt-3 text-sm">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={autoPublish}
                onChange={(event) => toggleAutoPublish(event.target.checked)}
              />
              Auto-publish scheduled reels when due
            </label>
            <span className="text-muted-foreground">
              {dueCount > 0 ? `${dueCount} due now` : "none due"}
            </span>
            <Button
              size="sm"
              variant="outline"
              disabled={publishingDue || dueCount === 0}
              onClick={() => publishDueNow()}
            >
              {publishingDue ? "Publishing…" : `Publish due now${dueCount > 0 ? ` (${dueCount})` : ""}`}
            </Button>
            <span className="text-xs text-muted-foreground">
              Live posts run only while this window is open.
            </span>
          </div>
          {publishMessage && <p className="text-sm text-emerald-600">{publishMessage}</p>}
          {dueDialog && (
            <PublishDueDialog
              warnings={dueDialog}
              dueCount={dueCount}
              busy={publishingDue}
              onRescheduleSafely={() => runPublishDue(false)}
              onPublishAnyway={() => runPublishDue(true)}
              onCancel={() => setDueDialog(null)}
            />
          )}
          {nowDialog && (
            <PublishNowDialog
              recency={nowDialog}
              busy={isThisItemPublishing || isThisItemScheduling || busy}
              onSchedule={() => {
                setNowDialog(null);
                void autoScheduleForPublish();
              }}
              onPublishAnyway={() => void doPublishNow(true, nowDialogForceLocalRef.current)}
              onCancel={() => setNowDialog(null)}
            />
          )}
          {publishJobs.length > 0 ? (
            <div className="space-y-2 border-t border-border pt-3">
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Scheduled / queued publish
              </p>
              <ul className="space-y-1.5 text-sm">
              {publishJobs.map((job) => {
                const failed = job.status === "failed";
                const canCancelSchedule =
                  !job.posted_at &&
                  job.scheduled_at !== null &&
                  (job.status === "scheduled" || job.status === "cloud");
                const canceling = Boolean(cancelingScheduleIds[job.id]);
                return (
                  <li key={job.id} className="space-y-0.5">
                    <span className="flex flex-wrap items-center gap-2">
                      <Badge
                        variant={failed ? "destructive" : job.posted_at ? "default" : "secondary"}
                      >
                        {job.status}
                      </Badge>
                      <span className="text-muted-foreground">
                        {job.title ?? "(untitled)"}
                        {job.scheduled_at
                          ? ` — ${failed ? "last attempt" : "scheduled"} ${formatDate(job.scheduled_at)}`
                          : ""}
                        {job.posted_at ? ` — posted ${formatDate(job.posted_at)}` : ""}
                      </span>
                      {canCancelSchedule && (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={canceling || busy || isThisItemPublishing || isThisItemScheduling}
                          onClick={() => void cancelSchedule(job)}
                        >
                          {canceling ? "Canceling..." : "Cancel schedule"}
                        </Button>
                      )}
                    </span>
                    {failed && (
                      <p className="text-xs text-red-500">
                        {job.error_message ?? "Publish failed."} — republish with Publish Now,
                        or Schedule/Auto Schedule to retry later.
                      </p>
                    )}
                  </li>
                );
              })}
              </ul>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              No publish-queue entries for this item yet. Posting to Instagram still happens
              from the desktop app's Publish Queue.
            </p>
          )}
        </CardContent>
      </Card>

    </div>
  );
}
