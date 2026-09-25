import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { CropEditor } from "@/components/CropEditor";
import { CropPreview } from "@/components/CropPreview";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useToast } from "@/components/ui/Toast";
import { bridge } from "@/lib/bridge";
import { cn } from "@/lib/utils";
import type {
  BatchCandidateGroup,
  BatchCandidateItem,
  BatchDraftImportResult,
  FetchMissingMediaResult,
  FinishBatchPlanEntry,
  FinishBatchResult,
  FinishBatchSchedule,
  JobSnapshot,
} from "@/types";

const DEFAULT_PER_ACCOUNT = 6;
// Accounts per prepared wave. A count of ACCOUNTS rather than of reels
// because an account's reels share one voice block in the prompt and are
// cheapest to draft together: splitting an account across two chats re-sends
// its style rules for nothing.
const DEFAULT_WAVE_ACCOUNTS = 3;
const POLL_INTERVAL_MS = 900;
// Consecutive getJob failures tolerated before a wait gives up. The job outlives
// the poller, so a brief bridge outage must not be read as the job dying.
const MAX_POLL_FAILURES = 5;

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

type PendingBatch = {
  key: string;
  label: string;
  /** The reel-header ids this batch covers, exactly as the prompt and reply
   *  echo them in "===== REEL n (#N) =====". The one label that lets you match
   *  a row to the chat window you are looking at. */
  reels: string;
  targets: number[];
  framesFolder: string | null;
  needsFrames: number;
  /** Set once imported. The row STAYS so a reply pasted onto the wrong account
   *  can simply be imported again onto the right one; without this the row
   *  vanished on import and the only recovery was re-preparing the batch. */
  imported: { ok: number; failed: number; unmatched: number } | null;
};

/** Where a reel is in the export half of the flow. Absent means it has not been
 *  handed to "Finish batch" yet. */
type ExportState = "exporting" | "exported" | "failed";

/** Roll the per-reel export states up to the row the operator is looking at. */
function exportSummary(
  targets: number[],
  states: ReadonlyMap<number, ExportState>,
): { text: string; tone: ResultTone } | null {
  let exporting = 0;
  let exported = 0;
  let failed = 0;
  for (const id of targets) {
    const state = states.get(id);
    if (state === "exporting") exporting += 1;
    else if (state === "exported") exported += 1;
    else if (state === "failed") failed += 1;
  }
  if (!exporting && !exported && !failed) return null;
  const failedNote = failed ? `, ${failed} failed` : "";
  if (exporting) {
    return { text: `exporting ${exporting}/${targets.length}…${failedNote}`, tone: "warn" };
  }
  const done = exported === targets.length ? "exported" : `exported ${exported}/${targets.length}`;
  return { text: `${done}${failedNote}`, tone: failed ? "error" : "ok" };
}

// How often to re-check while distributed reels are still downloading. A shared
// asset is a few seconds to a couple of minutes, so 6s clears the list promptly
// without hammering the bridge; the poll stops the moment nothing is pending.
const PENDING_MEDIA_POLL_MS = 6000;

// distribute reports WHY it placed nothing. The old message assumed an empty
// pool for every case, which pointed the user at Pool & Distribute even when the
// real cause was an account already at its target or not publish-ready.
const DISTRIBUTE_BLOCKED_REASON: Record<string, string> = {
  no_accounts: "no accounts in this niche.",
  no_ready_accounts: "the account isn't publish-ready (log its Instagram profile in).",
  all_at_cap: "the account is already at its distribute target.",
  pool_empty: "nothing unassigned left in this niche's pool.",
  download_failed: "the replacement clip's footage failed to download.",
};

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
// Read by the Python export path, not just this screen — hence the un-namespaced
// key. Keep it in sync with export.WATERMARK_SCAN_SETTING_KEY.
const SETTING_WATERMARK_SCAN = "export_watermark_scan";
const SETTING_WAVE_ACCOUNTS = "batchDrafts.waveAccounts";
const KNOWN_NICHES = ["history", "movie"] as const;

function parseExcludedAccounts(value: unknown): ReadonlySet<number> {
  if (!Array.isArray(value)) return new Set();
  return new Set(value.filter((id): id is number => Number.isInteger(id)));
}

function parseWaveAccounts(value: unknown): number {
  // An unset preference reads back as null, and Number(null) is 0 - finite, so
  // the default below never fired and the clamp pinned the wave to one account.
  // The save effect then wrote that 1 back, making it stick.
  if (value === null || value === undefined || value === "") return DEFAULT_WAVE_ACCOUNTS;
  const size = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(size)) return DEFAULT_WAVE_ACCOUNTS;
  return Math.min(20, Math.max(1, Math.trunc(size)));
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
  // How many accounts one "Prepare next wave" click covers.
  const [waveAccounts, setWaveAccounts] = useState(DEFAULT_WAVE_ACCOUNTS);
  const [niche, setNiche] = useState<string | null>(null);
  // Preferences arrive from the bridge after mount. Until they do, nothing may
  // be written back, or the defaults would overwrite what was stored.
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<number>>(() => new Set());
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The export is the only long job here (it renders and uploads video), and it
  // used to share `busy` with prepare/import - which froze the whole picker for
  // minutes and made queueing the next account impossible. Kept separate so a
  // running export blocks only a second export.
  const [exporting, setExporting] = useState(false);
  const [progress, setProgress] = useState<{ value: number; message: string } | null>(null);
  // Pinned at prepare time: reel numbers in the reply map to THIS list, so a
  // later change to the candidate pool must not shift the routing.
  // A QUEUE, not one slot. With a single `preparedIds`, preparing a second
  // account overwrote the first, and the first account's reply then routed by
  // reel POSITION onto the second account's clips - silently, because the
  // echoed "(#id)" is not in the second account's id map. Each entry carries
  // the targets the prompt was built from, so a reply can only land on the
  // reels it was written for.
  const [pending, setPending] = useState<PendingBatch[]>([]);
  const [importResult, setImportResult] = useState<BatchDraftImportResult | null>(null);
  const [plan, setPlan] = useState<FinishBatchPlanEntry[] | null>(null);
  const [finishResult, setFinishResult] = useState<FinishBatchResult | null>(null);
  // Per-reel export outcome, so a prepared row shows whether it has actually
  // been through "Finish batch". The rows deliberately survive finishing, and
  // the plan is cleared afterwards, so with several batches in flight every row
  // read "imported 6" whether it was still waiting or already exported.
  const [exportStates, setExportStates] = useState<ReadonlyMap<number, ExportState>>(
    () => new Map(),
  );
  // Accounts explicitly switched off. Absent from the set means included, so a
  // newly distributed-to account joins the batch by default rather than being
  // silently left out.
  const [excludedAccounts, setExcludedAccounts] = useState<ReadonlySet<number>>(
    () => new Set(),
  );
  // Accounts whose already-drafted reels are unfolded. Collapsed by default so
  // the list length matches "Reels per account": the drafted ones are a redraft
  // escape hatch, not part of the batch you are about to prepare.
  const [expandedDrafted, setExpandedDrafted] = useState<ReadonlySet<number>>(
    () => new Set(),
  );
  const [reviewing, setReviewing] = useState<BatchCandidateItem | null>(null);
  const [rejectReason, setRejectReason] = useState("wrong_niche");
  const [autoDistributeOnReject, setAutoDistributeOnReject] = useState(false);
  // Defaults to on, matching the Python side: an export that quietly stopped
  // covering foreign handles is worse than a slow one.
  const [watermarkScan, setWatermarkScan] = useState(true);
  // Crop is opened per review, so it never stays open across clips.
  const [cropOpen, setCropOpen] = useState(false);

  // Item ids the list last showed, so a background reload can tell an arriving
  // reel from one the user deliberately un-ticked.
  const knownItemIds = useRef<Set<number>>(new Set());

  /** Reload the candidate list. Returns the item ids still on offer, or null if
   *  the reload itself failed — callers use it to reconcile stale batch state. */
  const load = useCallback(
    async ({
      preserveSelection = false,
    }: { preserveSelection?: boolean } = {}): Promise<number[] | null> => {
      try {
        const { groups: next } = await bridge.batchDraftCandidates(niche, perAccount);
        const ids = next.flatMap((group) => group.items.map((item) => item.id));
        // Already-drafted reels are listed so a title can be redone, but they
        // are never ticked for the operator: auto-selecting them would redraft
        // every finished reel on each poll.
        const draftless = new Set(
          next.flatMap((group) =>
            group.items.filter((item) => !item.has_draft).map((item) => item.id),
          ),
        );
        const known = knownItemIds.current;
        knownItemIds.current = new Set(ids);
        setGroups(next);
        setLoadError(null);
        if (preserveSelection) {
          // A reload the user did not ask for must not undo their ticking: keep
          // every current choice (a drafted reel the user ticked on purpose
          // included) and tick only the DRAFTLESS reels that just appeared.
          setSelected(
            (prev) =>
              new Set(ids.filter((id) => prev.has(id) || (!known.has(id) && draftless.has(id)))),
          );
          return ids;
        }
        // Default to everything still undrafted: the common case is "draft the
        // next N for every account", and un-ticking is faster than ticking 36.
        setSelected(new Set(ids.filter((id) => draftless.has(id))));
        return ids;
      } catch (err: unknown) {
        setLoadError(err instanceof Error ? err.message : String(err));
        return null;
      }
    },
    [niche, perAccount],
  );

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
          SETTING_WATERMARK_SCAN,
          SETTING_WAVE_ACCOUNTS,
        ]);
        if (cancelled) return;
        setExcludedAccounts(parseExcludedAccounts(stored[SETTING_EXCLUDED_ACCOUNTS]));
        setAutoDistributeOnReject(stored[SETTING_AUTO_DISTRIBUTE] === true);
        setNiche(parseNiche(stored[SETTING_NICHE]));
        setWaveAccounts(parseWaveAccounts(stored[SETTING_WAVE_ACCOUNTS]));
        // Unset reads back as undefined, which must mean on — same default the
        // export path applies when the row has never been written.
        setWatermarkScan(stored[SETTING_WATERMARK_SCAN] !== false);
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

  useEffect(() => {
    if (!settingsLoaded) return;
    void bridge.setUiSetting(SETTING_WATERMARK_SCAN, watermarkScan);
  }, [watermarkScan, settingsLoaded]);

  useEffect(() => {
    if (!settingsLoaded) return;
    void bridge.setUiSetting(SETTING_WAVE_ACCOUNTS, waveAccounts);
  }, [waveAccounts, settingsLoaded]);

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
  // Counted across ALL groups, not just included ones: the retry is global, and
  // an excluded account's stuck footage is still worth recovering.
  const pendingMediaTotal = useMemo(
    () => groups.reduce((total, group) => total + group.pending_media, 0),
    [groups],
  );

  // Reels whose reply was imported but never finished, across every account.
  // Surfaced at the top because the per-account cards are where they hid: a
  // couple per card reads as noise, the network total reads as a backlog.
  const pendingFinishTotal = useMemo(
    () => groups.reduce((total, group) => total + group.pending_finish, 0),
    [groups],
  );

  // Distribution returns as soon as the rows exist; the footage lands seconds to
  // minutes later on a background thread that never notifies this screen. Without
  // a poll the counts a distribute produced stay frozen ("1 of 1 draftless · 2
  // waiting on footage") until the user happens to press Refresh, which reads as
  // the fix not working rather than as a download still in flight.
  //
  // Only runs while something is actually pending, so a settled screen makes no
  // requests, and pauses while `busy` so a poll can never reset `selected` out
  // from under a prepare/import/finish that is mid-flight.
  useEffect(() => {
    if (!active || !settingsLoaded || busy || pendingMediaTotal === 0) return;
    const timer = window.setInterval(
      () => void load({ preserveSelection: true }),
      PENDING_MEDIA_POLL_MS,
    );
    return () => window.clearInterval(timer);
  }, [active, settingsLoaded, busy, pendingMediaTotal, load]);

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
  // Reels already sitting in a prepared batch. Derived from the pending rows
  // rather than from a flag on `selected`, because Refresh re-ticks every
  // draftless reel - without this, one press of it would hand an account that
  // is already waiting on a chat to a second wave as well.
  const preparedItemIds = useMemo(
    () => new Set(pending.flatMap((batch) => batch.targets)),
    [pending],
  );
  // The next wave: the first `waveAccounts` included accounts that still have a
  // ticked reel nobody has prepared. Whole accounts only - splitting one across
  // two chats would re-send its style rules for nothing and risk two different
  // titles landing on sibling reels.
  const nextWave = useMemo(() => {
    const unsent: { name: string; itemIds: number[] }[] = [];
    for (const group of includedGroups) {
      const itemIds = group.items
        .filter((item) => selected.has(item.id) && !preparedItemIds.has(item.id))
        .map((item) => item.id);
      if (itemIds.length) unsent.push({ name: group.account_name, itemIds });
    }
    const wave = unsent.slice(0, Math.max(1, waveAccounts));
    return {
      names: wave.map((entry) => entry.name),
      itemIds: wave.flatMap((entry) => entry.itemIds),
      accountsRemaining: unsent.length - wave.length,
      // Built here rather than in the JSX: splitting "account" from its plural
      // "s" across two source lines makes JSX join them with a space.
      label: `${wave.length} account${wave.length === 1 ? "" : "s"}`,
    };
  }, [includedGroups, selected, preparedItemIds, waveAccounts]);

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
      // "All" means every reel still needing a draft. "None" clears the group
      // outright, including any drafted reel the user ticked to redo.
      const draftless = group.items.filter((item) => !item.has_draft);
      const allOn = draftless.length > 0 && draftless.every((item) => next.has(item.id));
      if (allOn) group.items.forEach((item) => next.delete(item.id));
      else draftless.forEach((item) => next.add(item.id));
      return next;
    });

  const toggleAccountIncluded = (accountId: number) =>
    setExcludedAccounts((prev) => {
      const next = new Set(prev);
      if (next.has(accountId)) next.delete(accountId);
      else next.add(accountId);
      return next;
    });

  /** Fold the account's already-drafted reels in or out of its list. Collapsing
   *  also un-ticks them: a selected reel that is not on screen would silently
   *  join the next prepared batch. */
  const toggleDraftedShown = (group: BatchCandidateGroup) => {
    const accountId = group.account_id;
    const willShow = !expandedDrafted.has(accountId);
    setExpandedDrafted((prev) => {
      const next = new Set(prev);
      if (willShow) next.add(accountId);
      else next.delete(accountId);
      return next;
    });
    if (!willShow) {
      setSelected((prev) => {
        const next = new Set(prev);
        group.items.filter((item) => item.has_draft).forEach((item) => next.delete(item.id));
        return next;
      });
    }
  };

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
          // `available` counts draftless reels only, so rejecting a drafted one
          // must not decrement it.
          available: Math.max(0, group.available - (item.has_draft ? 0 : 1)),
        })),
      );
      setSelected((prev) => {
        const next = new Set(prev);
        next.delete(item.id);
        return next;
      });
      // preparedIds is deliberately NOT shrunk here. Import's reel-number
      // fallback routes by POSITION in that list, so removing an entry would
      // shift every later reel onto the wrong clip. The rejected reel keeps its
      // slot and is dropped by review state on the Python side instead (see
      // draft_handoff._rejected_in_batch and batch_finish.plan_batch).
      //
      // A plan already on screen is safe to filter: it drives the Finish count
      // and nothing routes by its position.
      setPlan((prev) => prev?.filter((entry) => entry.item_id !== item.id) ?? prev);
      setReviewing(null);
      setCropOpen(false);

      // Rejecting released this clip's slot, so the account is now one under
      // target. Topping up here saves a trip to Pool & Distribute per reject.
      const niche = reviewingGroup?.niche;
      const accountId = reviewingGroup?.account_id;
      if (autoDistributeOnReject && niche && accountId != null) {
        try {
          // Scoped to THIS account, one clip: replace what was rejected, nothing
          // more. distributeNiche would top up every account in the niche, so a
          // single reject could pull dozens of clips into the network and kick
          // off their downloads — a surprise the user never asked for.
          const result = await bridge.distributeNicheExplicit(niche, { [accountId]: 1 });
          // Reload either way: reject already changed the list, and a successful
          // distribute adds clips that only a refetch will show.
          await load();
          pushToast(
            result.assigned > 0
              ? `Rejected #${item.account_seq ?? item.id}. Distributed ${result.assigned} replacement clip(s).`
              : `Rejected #${item.account_seq ?? item.id}. No replacement: ` +
                `${DISTRIBUTE_BLOCKED_REASON[result.reason ?? "pool_empty"]}`,
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
    let pollFailures = 0;
    for (;;) {
      let snapshot: JobSnapshot;
      try {
        snapshot = await bridge.getJob(jobId);
        pollFailures = 0;
      } catch (err: unknown) {
        // A failed POLL says nothing about the JOB. finish_batch renders for
        // minutes (18 reels ran ~6 minutes = ~400 polls at this interval) and
        // the Python side keeps going regardless of who is watching. Aborting on
        // the first bridge hiccup left the screen showing the pre-finish picker
        // while every reel had in fact been exported and scheduled, which reads
        // as "finish batch did nothing". Ride out a short outage instead.
        if ((pollFailures += 1) > MAX_POLL_FAILURES) throw err;
        await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
        continue;
      }
      setProgress({ value: snapshot.progress, message: snapshot.message });
      if (snapshot.status === "succeeded") return snapshot.result;
      if (snapshot.status === "canceled") throw new Error(snapshot.message || "Canceled.");
      if (snapshot.status === "failed") throw new Error(snapshot.error ?? "The job failed.");
      await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
    }
  }, []);

  // Distribution creates the Processing row before its footage lands, and the
  // background download gets exactly one attempt. When it fails or the app closes
  // mid-queue, the row keeps no local file and no retry ever runs — so this is a
  // manual retry rather than a spinner the user is told to wait on.
  const fetchMissingMedia = async () => {
    setBusy(true);
    setProgress({ value: 0, message: "Looking for missing footage…" });
    try {
      const { job_id } = await bridge.startFetchMissingBatchMedia(null);
      const result = (await waitForJob(job_id)) as FetchMissingMediaResult;
      await load();
      const recovered = result.linked_from_disk + result.fetched;
      const notes = [
        result.retired_gone ? `${result.retired_gone} gone source(s) retired` : "",
        result.failures.length ? `${result.failures.length} still failing` : "",
        result.unresolved ? `${result.unresolved} need opening in Processing` : "",
      ].filter(Boolean);
      pushToast(
        recovered
          ? `Recovered ${recovered} reel(s).${notes.length ? ` ${notes.join(", ")}.` : ""}`
          : notes.length
            ? `Nothing recovered: ${notes.join(", ")}.`
            : "Nothing was waiting on footage.",
        recovered || !notes.length ? "success" : "error",
      );
    } catch (err: unknown) {
      pushToast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setBusy(false);
      setProgress(null);
    }
  };

  // `ids` rather than `selectedIds` so the same path serves both the
  // everything-at-once button and one wave of accounts.
  const prepare = async (ids: number[]) => {
    if (!ids.length) {
      pushToast("Pick at least one reel first.", "error");
      return;
    }
    setBusy(true);
    setProgress({ value: 0, message: "Preparing…" });
    setFinishResult(null);
    try {
      const { job_id } = await bridge.startPrepareMultiAccountBatchFrames(ids);
      const frames = (await waitForJob(job_id)) as {
        folder: string;
        frames: { item_id: number }[];
      };
      const { prompt } = await bridge.buildMultiAccountBatchChatPrompt(ids, {});
      await bridge.copyTextToClipboard(prompt);
      // Order the prompt actually used, so REEL n routes to the right reel.
      const { item_ids: ordered } = await bridge.multiAccountBatchOrder(ids);
      const names = [
        ...new Set(
          groups
            .filter((group) => group.items.some((item) => ordered.includes(item.id)))
            .map((group) => group.account_name),
        ),
      ];
      // The GLOBAL item id, not the account-relative "#401" the picker shows.
      // A cross-account batch echoes the global id in its reel headers, because
      // account-relative numbers collide once a batch spans accounts - so the
      // account-relative number would label the row with an id that appears
      // nowhere in the chat the operator is matching against.
      const seqs = ordered;
      setPending((prev) => [
        ...prev,
        {
          key: `${Date.now()}-${ordered.join(",")}`,
          label: `${names.join(", ") || "batch"} · ${ordered.length} reel(s)`,
          reels: seqs.length
            ? seqs.length > 1
              ? `#${Math.min(...seqs)}-#${Math.max(...seqs)}`
              : `#${seqs[0]}`
            : "",
          targets: ordered,
          framesFolder: frames.frames.length ? frames.folder : null,
          needsFrames: frames.frames.length,
          imported: null,
        },
      ]);
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

  // Waves put several prompts in flight at once, and each prepare overwrites
  // the clipboard - so the only way back to an earlier wave's prompt used to be
  // discarding the row and re-ticking it. Rebuilds from the row's own targets,
  // which is the same id list (and therefore the same reel order) the prompt
  // was built from.
  const copyPrompt = async (batch: PendingBatch) => {
    setBusy(true);
    try {
      const { prompt } = await bridge.buildMultiAccountBatchChatPrompt(batch.targets, {});
      await bridge.copyTextToClipboard(prompt);
      pushToast(`Prompt for ${batch.label} copied again.`, "success");
    } catch (err: unknown) {
      pushToast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setBusy(false);
    }
  };

  // Merge rather than replace: with several batches in flight, "Finish batch"
  // has to cover everything queued so far, not just the last thing added.
  const mergePlan = (next: FinishBatchPlanEntry[]) => {
    setPlan((prev) => {
      const merged = new Map((prev ?? []).map((entry) => [entry.item_id, entry]));
      for (const entry of next) merged.set(entry.item_id, entry);
      return [...merged.values()];
    });
  };

  // Reels whose reply was imported but never finished keep their draft in the
  // revision, unapplied. The plan that drives "Finish batch" was only ever built
  // by an import in THIS session, so closing the app stranded them: counted as
  // drafted, folded away, with nothing left that could act on them. Planning
  // them straight from the card hands Finish exactly what an import would have.
  const queueForFinish = async (ids: number[]) => {
    if (!ids.length) return;
    setBusy(true);
    try {
      const { plan: next } = await bridge.planFinishBatch(ids);
      mergePlan(next);
      const ready = next.filter((entry) => entry.ready).length;
      pushToast(
        ready
          ? `${ready} reel(s) queued — press "3. Finish batch" to export them.`
          : "None of those reels can be finished. Check their review state.",
        ready ? "success" : "error",
      );
    } catch (err: unknown) {
      pushToast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setBusy(false);
    }
  };

  const importReply = async (batch: PendingBatch) => {
    const targets = batch.targets;
    if (!targets.length) return;
    setBusy(true);
    try {
      const text = await bridge.readClipboardText();
      const result = await bridge.importMultiAccountBatchDraft(text, targets);
      setImportResult(result);
      const { plan: next } = await bridge.planFinishBatch(targets);
      mergePlan(next);
      setPending((prev) =>
        prev.map((entry) =>
          entry.key === batch.key
            ? {
                ...entry,
                imported: {
                  ok: result.imported.length,
                  failed: result.failed.length,
                  unmatched: result.unmatched.length,
                },
              }
            : entry,
        ),
      );
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

  const markExports = (ids: number[], state: ExportState) =>
    setExportStates((prev) => {
      const next = new Map(prev);
      for (const id of ids) next.set(id, state);
      return next;
    });

  /** Close out a run: anything the job did not account for loses its
   *  "exporting" mark rather than spinning forever. */
  const settleExports = (inFlight: number[], exported: number[], failed: number[]) =>
    setExportStates((prev) => {
      const next = new Map(prev);
      for (const id of inFlight) next.delete(id);
      for (const id of exported) next.set(id, "exported");
      for (const id of failed) next.set(id, "failed");
      return next;
    });

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
    const inFlight = readyPlan.map((entry) => entry.item_id);
    setExporting(true);
    markExports(inFlight, "exporting");
    setProgress({ value: 0, message: "Starting…" });
    try {
      const { job_id } = await bridge.startFinishBatch(inFlight);
      const result = (await waitForJob(job_id)) as FinishBatchResult;
      setFinishResult(result);
      settleExports(
        inFlight,
        result.exported.map((entry) => entry.item_id),
        result.failed.map((entry) => entry.item_id),
      );
      // Every exported reel now has a draft revision and a publish job, so it is
      // no longer a candidate. Without this the picker kept offering the reels it
      // had just exported, still ticked, and a second "Finish batch" would
      // re-render and re-schedule the lot.
      //
      // Only the PLAN is cleared. `pending` holds batches whose reply has not
      // come back yet, and finishing an exported batch has nothing to do with
      // them - clearing it here threw away prompts the user was still waiting
      // on, so finishing account 1 silently destroyed prepared account 2.
      //
      // And only the reels THIS export covered leave it. A bare `setPlan(null)`
      // also dropped anything imported WHILE the export ran, which is the whole
      // point of running it in the background: 12 reels queued during an
      // 18-reel render fell off the Finish count the moment the 18 landed, and
      // had to be re-queued from the account card. The updater form is required
      // - `plan` in this closure is the pre-export list and does not contain
      // them.
      const finished = new Set(inFlight);
      setPlan((prev) => {
        const kept = (prev ?? []).filter((entry) => !finished.has(entry.item_id));
        return kept.length ? kept : null;
      });
      // preserveSelection: the export runs in the background now, so the user
      // may have ticked the next account's reels while it was going. A bare
      // load() resets the selection to "everything", silently undoing that.
      await load({ preserveSelection: true });
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
      // Losing sight of the job does NOT mean the job stopped: the export runs
      // on a Python thread that finishes whether or not anyone is polling. So
      // reconcile against the database rather than leaving the pre-finish picker
      // on screen — that is what made a fully successful 18-reel export read as
      // "finish batch did nothing". Reels that did export drop out of the
      // candidate list, so pruning the plan to what is still a candidate also
      // stops a retry from re-rendering and re-scheduling them.
      const stillCandidates = await load();
      if (stillCandidates) {
        const remaining = new Set(stillCandidates);
        // Counted from `inFlight`, not from the plan's length: only reels this
        // export actually covered can have exported, and the plan may since have
        // grown with imports made while it ran.
        const exported = inFlight.filter((id) => !remaining.has(id)).length;
        // Updater form for the same reason as the success path above: `plan`
        // here is the list as it stood when Finish was pressed.
        setPlan((prev) => {
          const pruned = (prev ?? []).filter((entry) => remaining.has(entry.item_id));
          return pruned.length ? pruned : null;
        });
        // Leaving the row's status on "exporting…" after the poller gave up
        // would be the same lie the plan pruning above exists to undo: a reel
        // that dropped out of the candidate list did finish.
        settleExports(
          inFlight,
          inFlight.filter((id) => !remaining.has(id)),
          [],
        );
        if (exported > 0) {
          pushToast(
            `Lost track of the export job, but ${exported} reel(s) did finish and are no ` +
              "longer pending. Check Multi-Account Publish before re-running.",
            "error",
          );
          return;
        }
      }
      pushToast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setExporting(false);
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
                A wave of accounts per prompt: prepare one, paste it into a chat, then
                prepare the next while that one is still answering.
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
              <label className="flex items-center gap-2 text-sm">
                <span className="text-muted-foreground">Accounts per wave</span>
                <input
                  type="number"
                  min={1}
                  max={20}
                  value={waveAccounts}
                  disabled={busy}
                  onChange={(e) => setWaveAccounts(Math.max(1, Number(e.target.value) || 1))}
                  className="w-16 rounded-md border border-input bg-background px-2 py-1"
                  title="How many accounts one 'Prepare next wave' click covers. Prepare a wave, paste it into a chat, then prepare the next wave while the first is still answering."
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
            {pendingFinishTotal > 0 && (
              <span
                className="text-amber-500"
                title='These reels already have a draft that was never applied or exported. Use the "Finish" button on the account card — no new prompt needed.'
              >
                {pendingFinishTotal} drafted, never exported
              </span>
            )}
            <Button size="sm" variant="secondary" onClick={() => void load()} disabled={busy}>
              Refresh
            </Button>
            <label
              className="flex items-center gap-2 text-muted-foreground"
              title="Scans every reel for a foreign @handle and covers it with yours. Adds roughly 17s per reel, about half of an export. Turn it off when the source accounts don't watermark."
            >
              <input
                type="checkbox"
                checked={watermarkScan}
                disabled={busy}
                onChange={(event) => setWatermarkScan(event.target.checked)}
              />
              <span>
                Cover foreign watermarks
                {!watermarkScan && " (off — exports ~17s/reel faster)"}
              </span>
            </label>
            {/* Lives here rather than inside the review popup: it is a remembered
                batch preference like the one above, and buried in the modal it
                read as "the feature doesn't exist". */}
            <label
              className="flex items-center gap-2 text-muted-foreground"
              title="After you reject a clip, pull one replacement into that same account from its niche pool."
            >
              <input
                type="checkbox"
                checked={autoDistributeOnReject}
                disabled={busy}
                onChange={(event) => setAutoDistributeOnReject(event.target.checked)}
              />
              <span>Replace rejected clips automatically</span>
            </label>
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

          {/* One row per prepared batch. The import button lives on the ROW, not
              on the toolbar, because a global one has to guess which chat the
              clipboard came from - and guessing wrong files six titles onto
              another account's reels without erroring. */}
          {pending.length > 0 && (
            <div className="space-y-2 rounded-md border border-border p-3">
              <p className="text-xs text-muted-foreground">
                Prepare as many accounts as you like, then import each one when its chat comes
                back. Match the reel range to the "===== REEL n (#N) =====" headers in that
                chat. Rows stay after importing, so pasting the wrong reply is fixable: copy
                the right one and press Import again.
              </p>
              {pending.map((batch) => {
                const exportState = exportSummary(batch.targets, exportStates);
                return (
                  <div key={batch.key} className="flex flex-wrap items-center gap-2">
                    <span className="text-sm">{batch.label}</span>
                    {batch.reels && (
                      <code className="rounded bg-muted px-1.5 py-0.5 text-xs">
                        {batch.reels}
                      </code>
                    )}
                    {batch.imported && (
                      <span
                        className={
                          batch.imported.failed || batch.imported.unmatched
                            ? "text-xs text-destructive"
                            : "text-xs text-muted-foreground"
                        }
                      >
                        imported {batch.imported.ok}
                        {batch.imported.failed ? `, ${batch.imported.failed} failed` : ""}
                        {batch.imported.unmatched ? `, ${batch.imported.unmatched} unmatched` : ""}
                      </span>
                    )}
                    {/* Whether this row has actually been through "Finish batch".
                        Rows survive finishing on purpose, so without this an
                        exported batch and one still waiting look identical. */}
                    {exportState && (
                      <span className={cn("text-xs", TONE_CLASS[exportState.tone])}>
                        {exportState.text}
                      </span>
                    )}
                    {batch.needsFrames > 0 && (
                      <button
                        type="button"
                        className="text-xs underline text-muted-foreground"
                        onClick={() =>
                          batch.framesFolder && bridge.openBatchFramesFolder(batch.framesFolder)
                        }
                      >
                        {batch.needsFrames} frame(s)
                      </button>
                    )}
                    <button
                      type="button"
                      className="text-xs underline text-muted-foreground"
                      disabled={busy}
                      onClick={() => void copyPrompt(batch)}
                      title="Put this batch's prompt back on the clipboard — preparing another wave overwrites it."
                    >
                      copy prompt
                    </button>
                    <Button
                      variant={batch.imported ? "ghost" : "secondary"}
                      disabled={busy}
                      onClick={() => importReply(batch)}
                    >
                      {batch.imported ? "Import again" : "Import this reply"}
                    </Button>
                    <button
                      type="button"
                      className="text-xs underline text-muted-foreground"
                      disabled={busy}
                      onClick={() =>
                        setPending((prev) => prev.filter((entry) => entry.key !== batch.key))
                      }
                    >
                      discard
                    </button>
                  </div>
                );
              })}
            </div>
          )}

          {/* What actually landed, before anything is exported. The counts alone
              ("imported 6") said a reply parsed, never whether the right title
              reached the right reel - so a misrouted or wrong-account import was
              invisible until the reel was already published. */}
          {plan && plan.length > 0 && (
            <div className="space-y-1 rounded-md border border-border p-3">
              <p className="text-xs text-muted-foreground">
                Drafted and ready to export. Click a reel to open it in Processing and
                check the title against the footage; anything not ready says why.
              </p>
              {plan.map((entry) => {
                // itemMeta folds in the plan's own account_id, so a row still
                // resolves after the candidate list it came from is refreshed
                // away. Without a handler the row stays inert text rather than
                // offering a click that would go nowhere.
                const canOpen =
                  Boolean(onOpenInProcessing) &&
                  itemMeta.get(entry.item_id)?.accountId != null;
                const body = (
                  <>
                    <code className="shrink-0 rounded bg-muted px-1.5 py-0.5">
                      {entry.account_seq != null ? `#${entry.account_seq}` : entry.item_id}
                    </code>
                    <span className="shrink-0 w-40 truncate text-muted-foreground">
                      {entry.account_name ?? "unknown account"}
                    </span>
                    <span className={entry.ready ? "" : "text-destructive"}>
                      {entry.title || "(no title applied)"}
                      {entry.option ? ` · option ${entry.option}` : ""}
                      {entry.ready === false
                        ? ` · NOT READY: ${entry.reason ?? "unknown"}`
                        : ""}
                    </span>
                  </>
                );
                if (!canOpen) {
                  return (
                    <div key={entry.item_id} className="flex items-start gap-2 text-xs">
                      {body}
                    </div>
                  );
                }
                return (
                  <button
                    key={entry.item_id}
                    type="button"
                    onClick={() => openInProcessing(entry.item_id)}
                    title="Open this reel in Processing"
                    className="flex w-full items-start gap-2 rounded-sm px-1 py-0.5 text-left text-xs transition-colors hover:bg-muted/60 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  >
                    {body}
                  </button>
                );
              })}
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            {/* The default path once a network has more accounts than one chat
                can draft well: prepare a few, paste, and prepare the next few
                while that chat is still answering. */}
            <Button
              onClick={() => void prepare(nextWave.itemIds)}
              disabled={busy || !nextWave.itemIds.length}
              title="Prepares only the next few accounts that nothing has been prepared for yet, and copies just their prompt."
            >
              1. Prepare next wave ({nextWave.label})
            </Button>
            <Button
              variant="outline"
              onClick={() => void prepare(selectedIds)}
              disabled={busy || !selectedIds.length}
              title="One prompt covering every ticked reel, already-prepared ones included."
            >
              Prepare all {selectedIds.length} in one
            </Button>
            {exporting && (
              <span className="self-center text-xs text-muted-foreground">
                Exporting in the background. Keep picking and preparing; only a second
                export has to wait.
              </span>
            )}
            <Button
              variant="secondary"
              onClick={finish}
              disabled={busy || exporting || !readyPlan.length}
            >
              3. Finish batch ({readyPlan.length})
            </Button>
          </div>
          {nextWave.names.length > 0 ? (
            <p className="text-xs text-muted-foreground">
              Next wave: {nextWave.names.join(", ")} · {nextWave.itemIds.length} reel(s)
              {nextWave.accountsRemaining > 0
                ? ` · ${nextWave.accountsRemaining} account(s) still waiting after this one`
                : " · last wave"}
            </p>
          ) : (
            selectedIds.length > 0 && (
              <p className="text-xs text-muted-foreground">
                Every ticked account is already prepared. Import the replies below, or tick
                more reels.
              </p>
            )
          )}
          {pending.length > 0 && (
            <p className="text-xs text-muted-foreground">
              Paste each prompt into ChatGPT or Claude, copy that chat's whole reply, then
              press Import on the matching row above.
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
        {pendingMediaTotal > 0 && (
          <Button
            size="sm"
            variant="outline"
            className="ml-auto"
            disabled={busy}
            onClick={fetchMissingMedia}
            title="Re-download footage for distributed reels that never finished downloading"
          >
            Fetch footage ({pendingMediaTotal})
          </Button>
        )}
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        {groups.map((group) => {
          const draftlessItems = group.items.filter((i) => !i.has_draft);
          // Imported but never finished. Shown next to the draftless reels
          // rather than folded in with the finished ones: they are outstanding
          // work, and burying them is what let 23 of them pile up unnoticed.
          const unfinishedItems = group.items.filter((i) => i.needs_finish);
          const draftedItems = group.items.filter((i) => i.has_draft && !i.needs_finish);
          const allOn =
            draftlessItems.length > 0 && draftlessItems.every((i) => selected.has(i.id));
          const included = !excludedAccounts.has(group.account_id);
          const more = group.available - draftlessItems.length;
          // Drafted reels are folded away by default, so the row count matches
          // the per-account limit instead of doubling it.
          const draftedShown = expandedDrafted.has(group.account_id);
          const visibleItems = draftedShown
            ? group.items
            : group.items.filter((i) => !i.has_draft || i.needs_finish);
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
                        {draftlessItems.length} of {group.available} draftless
                        {group.pending_finish > 0
                          ? ` · ${group.pending_finish} drafted, not exported`
                          : ""}
                        {draftedItems.length > 0 ? ` · ${draftedItems.length} drafted` : ""}
                        {more > 0 ? ` · ${more} more available` : ""}
                        {group.pending_media > 0
                          ? ` · ${group.pending_media} waiting on footage`
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
                      ? `${group.pending_media} distributed reel(s) have no footage yet. If this doesn't clear on its own, use "Fetch footage" above.`
                      : "No reels with a downloaded file. Distribute more from Pool & Distribute."}
                  </p>
                ) : visibleItems.length === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    Every reel here already has a draft.
                  </p>
                ) : (
                  <ul className="space-y-1">
                    {visibleItems.map((item) => (
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
                        {item.needs_finish ? (
                          <span
                            className="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-xs text-amber-500"
                            title={
                              "Its draft was imported but never applied, so nothing was exported" +
                              (item.review_state
                                ? ` · ${item.review_state.replace(/_/g, " ")}`
                                : "") +
                              '. Use "Finish" to export it, or tick it to draft a new title.'
                            }
                          >
                            not exported
                          </span>
                        ) : (
                          item.has_draft && (
                            <span
                              className="shrink-0 rounded bg-emerald-500/15 px-1.5 py-0.5 text-xs text-emerald-500"
                              title={
                                "A batch reply has been imported for this reel" +
                                (item.review_state
                                  ? ` · ${item.review_state.replace(/_/g, " ")}`
                                  : "") +
                                ". Tick it to draft a new title."
                              }
                            >
                              drafted
                            </span>
                          )
                        )}
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
                {unfinishedItems.length > 0 && (
                  <Button
                    size="sm"
                    variant="secondary"
                    className="w-full"
                    disabled={busy || exporting}
                    onClick={() => queueForFinish(unfinishedItems.map((i) => i.id))}
                    title="Apply the drafts these reels already have and export them. No new prompt, no new reply."
                  >
                    Finish {unfinishedItems.length} drafted reel
                    {unfinishedItems.length === 1 ? "" : "s"}
                    {group.pending_finish > unfinishedItems.length
                      ? ` (${group.pending_finish} total)`
                      : ""}
                  </Button>
                )}
                {draftedItems.length > 0 && (
                  <button
                    type="button"
                    className="text-xs text-muted-foreground hover:underline"
                    onClick={() => toggleDraftedShown(group)}
                    title="Reels that already have an imported draft. Show them to redo a title."
                  >
                    {draftedShown
                      ? `Hide ${draftedItems.length} already drafted`
                      : `Show ${draftedItems.length} already drafted`}
                  </button>
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
                {/* Shown without asking: the automatic crop is what ships when
                    nobody opens the editor, so it has to be visible by default
                    rather than behind the disclosure below. */}
                {!cropOpen && (
                  <div className="border-b border-border p-3">
                    <CropPreview
                      itemId={reviewing.id}
                      onAdjust={() => setCropOpen(true)}
                    />
                  </div>
                )}
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

              {/* The control itself sits in the toolbar; this just says what
                  rejecting will do right now, with the pool named. */}
              <p className="text-xs text-muted-foreground">
                {autoDistributeOnReject
                  ? `A replacement clip will be pulled into ${reviewingGroup?.account_name ?? "this account"}` +
                    `${reviewingGroup?.niche ? ` from the ${reviewingGroup.niche} pool` : ""}.`
                  : "No replacement will be pulled in. Turn on “Replace rejected clips automatically” above to change that."}
              </p>
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
                {importResult.skipped.length > 0 && (
                  <>
                    {" · "}
                    <span>{importResult.skipped.length} rejected</span>
                  </>
                )}
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
              rows={importResult.skipped.map((row) => ({
                itemId: row.item_id,
                tone: "info" as const,
                note: row.reason,
              }))}
              heading="Skipped (rejected)"
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
