// Typed wrapper around the pywebview Python bridge (window.pywebview.api).
//
// Every backend method returns an envelope { ok, data | error }. `call` unwraps
// it: it resolves with `data` on success and throws an Error(message) on a
// handled backend failure, so React code can use plain try/catch.
//
// When running in a plain browser (npm run dev with no pywebview host), a small
// in-memory mock stands in so the UI is still developable without the desktop
// shell.

import type {
  AccountDetail,
  AccountSummary,
  ApifyUsage,
  ApplyResult,
  BatchDraftImportResult,
  BatchFramesResult,
  CloudAccountSettings,
  CloudPublisherHealth,
  CropRect,
  DeleteAccountResult,
  DistributeNicheResult,
  DraftRevision,
  DueRecencyWarning,
  DashboardPublishQueue,
  ItemSummary,
  JobSnapshot,
  LibraryItem,
  NicheAccount,
  PoolClip,
  PoolItemPreview,
  PoolReviewItem,
  PoolSource,
  PoolSourceClip,
  PoolingOverview,
  ProcessingContext,
  SetProcessingStatusResult,
  PublishEvent,
  PublishJob,
  PublishQueueJob,
  PublishRecency,
  AccountReadiness,
  DashboardAccountStats,
  QueueResult,
  ScheduleCoverage,
  ScrapeCandidate,
  SourceProfile,
  WorkflowSettings,
} from "@/types";

type Envelope<T> = { ok: true; data: T } | { ok: false; error: string };

interface PywebviewApi {
  list_items(): Promise<Envelope<ItemSummary[]>>;
  get_context(itemId?: number | null): Promise<Envelope<ProcessingContext>>;
  get_latest_revision(itemId: number): Promise<Envelope<DraftRevision | null>>;
  list_revisions(itemId: number): Promise<Envelope<DraftRevision[]>>;
  save_revision(
    itemId: number,
    payload: Record<string, unknown>,
  ): Promise<Envelope<DraftRevision>>;
  revise_option(
    itemId: number,
    optionNumber: number,
    payload: Record<string, unknown>,
  ): Promise<Envelope<DraftRevision>>;
  apply_revision(
    itemId: number,
    optionNumber: number,
    revisionId?: number | null,
  ): Promise<Envelope<ApplyResult>>;
  set_active_item(itemId: number): Promise<Envelope<unknown>>;
  can_generate(): Promise<Envelope<{ can_generate: boolean }>>;
  build_chat_prompt(
    itemId: number,
    payload: Record<string, unknown>,
  ): Promise<Envelope<{ prompt: string }>>;
  import_pasted_draft(itemId: number, text: string): Promise<Envelope<DraftRevision>>;
  build_account_batch_chat_prompt(
    accountId: number,
    itemIds: number[],
    payload?: Record<string, unknown> | null,
  ): Promise<Envelope<{ prompt: string }>>;
  import_account_batch_draft(
    text: string,
    itemIds: number[],
  ): Promise<Envelope<BatchDraftImportResult>>;
  prepare_account_batch_frames(itemIds: number[]): Promise<Envelope<BatchFramesResult>>;
  open_batch_frames_folder(folder: string): Promise<Envelope<{ folder: string }>>;
  get_workflow_settings(itemId: number): Promise<Envelope<WorkflowSettings>>;
  save_workflow_settings(
    itemId: number,
    payload: Record<string, unknown>,
  ): Promise<Envelope<WorkflowSettings>>;
  save_final_draft(
    itemId: number,
    title: string,
    caption: string,
  ): Promise<Envelope<{ title_draft: string; caption_draft: string }>>;
  open_item_folder(itemId: number): Promise<Envelope<{ folder: string }>>;
  get_crop_override(itemId: number): Promise<Envelope<CropRect | null>>;
  get_crop_preview(itemId: number): Promise<Envelope<{ item_id: number; preview_url: string }>>;
  save_crop_override(
    itemId: number,
    payload: CropRect,
  ): Promise<Envelope<{ item_id: number; crop_override: CropRect }>>;
  clear_crop_override(
    itemId: number,
  ): Promise<Envelope<{ item_id: number; crop_override: null }>>;
  start_generation(
    itemId: number,
    payload: Record<string, unknown>,
  ): Promise<Envelope<{ job_id: string }>>;
  start_export(itemId: number): Promise<Envelope<{ job_id: string }>>;
  start_item_download(itemId: number): Promise<Envelope<{ job_id: string }>>;
  prefetch_originals(itemIds: number[]): Promise<Envelope<{ job_id: string | null }>>;
  get_job(jobId: string): Promise<Envelope<JobSnapshot>>;
  list_publish_jobs(itemId: number): Promise<Envelope<PublishJob[]>>;
  set_processing_status(
    itemId: number,
    status: string,
  ): Promise<Envelope<SetProcessingStatusResult>>;
  queue_for_publish(
    itemId: number,
    scheduledAt?: string | null,
  ): Promise<Envelope<QueueResult>>;
  start_queue_for_publish(
    itemId: number,
    scheduledAt?: string | null,
  ): Promise<Envelope<{ job_id: string }>>;
  auto_schedule_for_publish(itemId: number): Promise<Envelope<QueueResult>>;
  start_auto_schedule_for_publish(itemId: number): Promise<Envelope<{ job_id: string }>>;
  sync_cloud_publish_jobs(): Promise<Envelope<{ synced: boolean; updated: number }>>;
  start_publish_now(
    itemId: number,
    allowRecent?: boolean,
    forceLocal?: boolean,
  ): Promise<Envelope<{ job_id: string }>>;
  publish_due_count(): Promise<Envelope<{ due: number }>>;
  item_publish_recency(itemId: number): Promise<Envelope<PublishRecency>>;
  due_publish_recency(): Promise<Envelope<DueRecencyWarning[]>>;
  start_publish_due(allowRecent?: boolean): Promise<Envelope<{ job_id: string }>>;
  drain_publish_events(): Promise<Envelope<PublishEvent[]>>;
  get_auto_publish(): Promise<Envelope<{ enabled: boolean }>>;
  set_auto_publish(enabled: boolean): Promise<Envelope<{ enabled: boolean }>>;
  list_accounts(): Promise<Envelope<AccountSummary[]>>;
  get_account(accountId: number): Promise<Envelope<AccountDetail>>;
  create_account(payload: Record<string, unknown>): Promise<Envelope<AccountDetail>>;
  update_account(
    accountId: number,
    payload: Record<string, unknown>,
  ): Promise<Envelope<AccountDetail>>;
  delete_account(accountId: number): Promise<Envelope<DeleteAccountResult>>;
  get_active_account(): Promise<Envelope<{ active_account_id: number | null }>>;
  set_active_account(
    accountId: number | null,
  ): Promise<Envelope<{ active_account_id: number | null }>>;
  list_library_items(accountId?: number | null): Promise<Envelope<LibraryItem[]>>;
  assign_account(
    itemId: number,
    accountId: number | null,
  ): Promise<Envelope<{ item_id: number; account_id: number | null; account_name: string | null }>>;
  remove_library_item(
    itemId: number,
  ): Promise<Envelope<{ removed_item_id: number; deleted_revisions: number }>>;
  remove_item_from_pool(
    itemId: number,
    reason: string,
  ): Promise<Envelope<{ item_id: number; removed_pool_items: number }>>;
  reject_item(
    itemId: number,
    reason: string,
  ): Promise<
    Envelope<{
      item_id: number;
      rejected_candidates: number;
      removed_pool_items: number;
      review_state: string;
    }>
  >;
  reject_item_globally(
    itemId: number,
    reason: string,
  ): Promise<
    Envelope<{
      item_id: number;
      removed_pool_items: number;
      dropped_assignments: number;
      review_state: string;
      blocked: boolean;
    }>
  >;
  list_publish_queue(accountId?: number | null): Promise<Envelope<PublishQueueJob[]>>;
  mark_job_posted(
    jobId: number,
    payload: Record<string, unknown>,
  ): Promise<Envelope<PublishQueueJob>>;
  update_job_metrics(
    jobId: number,
    payload: Record<string, unknown>,
  ): Promise<Envelope<PublishQueueJob>>;
  reschedule_job(jobId: number, scheduledAt: string): Promise<Envelope<PublishQueueJob>>;
  unschedule_job(jobId: number): Promise<Envelope<PublishQueueJob>>;
  remove_publish_job(jobId: number): Promise<Envelope<{ removed_job_id: number }>>;
  pooling_overview(): Promise<Envelope<PoolingOverview>>;
  list_pool_items(niche: string): Promise<Envelope<PoolClip[]>>;
  list_pool_sources(niche: string): Promise<Envelope<PoolSource[]>>;
  list_pool_source_clips(
    niche: string,
    sourceLabel: string,
    includeRemoved?: boolean,
  ): Promise<Envelope<PoolSourceClip[]>>;
  pool_review_queue(
    niche: string,
    sourceLabel?: string | null,
  ): Promise<Envelope<PoolReviewItem[]>>;
  pool_item_preview(poolItemId: number): Promise<Envelope<PoolItemPreview>>;
  start_pool_item_preview_download(
    poolItemId: number,
  ): Promise<Envelope<{ job_id: string }>>;
  approve_pool_items(ids: number[]): Promise<Envelope<{ approved: number }>>;
  reject_pool_items(ids: number[], reason: string): Promise<Envelope<{ rejected: number }>>;
  remove_pool_item(
    poolItemId: number,
    reason: string,
  ): Promise<Envelope<{ pool_item_id: number; acceptance_status: string }>>;
  restore_pool_item(
    poolItemId: number,
  ): Promise<Envelope<{ pool_item_id: number; acceptance_status: string }>>;
  pool_niche_accounts(niche: string): Promise<Envelope<NicheAccount[]>>;
  distribute_pool_item(
    poolItemId: number,
    accountIds: number[],
  ): Promise<Envelope<{ pool_item_id: number; assigned: number }>>;
  distribute_niche(
    niche: string,
    maxPerAccount?: number | null,
  ): Promise<Envelope<DistributeNicheResult>>;
  distribute_niche_explicit(
    niche: string,
    targets: Record<number, number>,
  ): Promise<Envelope<DistributeNicheResult>>;
  dashboard_publish_jobs(): Promise<Envelope<DashboardPublishQueue>>;
  dashboard_schedule_coverage(): Promise<Envelope<ScheduleCoverage>>;
  cloud_publisher_health(): Promise<Envelope<CloudPublisherHealth>>;
  dashboard_cloud_account_settings(accountId: number): Promise<Envelope<CloudAccountSettings | null>>;
  dashboard_update_cloud_account_settings(
    accountId: number,
    dailyLimit: number,
    minGapMinutes: number,
    enabled: boolean,
  ): Promise<Envelope<CloudAccountSettings>>;
  dashboard_force_publish_cloud_job(jobId: number): Promise<Envelope<{ id: string; forced: boolean }>>;
  dashboard_account_stats(activeAccountId: number): Promise<Envelope<DashboardAccountStats>>;
  dashboard_mark_ready(jobIds: number[]): Promise<Envelope<{ updated: number }>>;
  dashboard_open_output(jobId: number): Promise<Envelope<{ opened: string }>>;
  dashboard_account_readiness(): Promise<Envelope<AccountReadiness>>;
  dashboard_start_live_health_check(): Promise<Envelope<{ job_id: string }>>;
  dashboard_relogin(accountId: number): Promise<Envelope<{ profile: string }>>;
  list_sources(accountId: number): Promise<Envelope<SourceProfile[]>>;
  add_source(accountId: number, sourceUrl: string): Promise<Envelope<SourceProfile>>;
  set_source_enabled(sourceId: number, enabled: boolean): Promise<Envelope<SourceProfile>>;
  remove_source(sourceId: number): Promise<Envelope<{ removed_source_id: number }>>;
  list_candidates(accountId: number, state: string): Promise<Envelope<ScrapeCandidate[]>>;
  set_candidate_state(
    candidateId: number,
    state: string,
  ): Promise<Envelope<{ candidate_id: number; state: string }>>;
  accept_candidate(
    candidateId: number,
  ): Promise<Envelope<{ candidate_id: number; state: string; pool_item_id: number; niche: string }>>;
  reject_candidate(
    candidateId: number,
    reason: string,
  ): Promise<Envelope<{ candidate_id: number; state: string; removed_pool_items: number }>>;
  candidate_preview(
    candidateId: number,
  ): Promise<Envelope<{ preview_url: string | null; thumbnail_url: string | null }>>;
  apify_usage(): Promise<Envelope<ApifyUsage>>;
  start_source_scrape(
    sourceId: number,
    maxItems: number,
  ): Promise<Envelope<{ job_id: string }>>;
  start_candidate_download(candidateId: number): Promise<Envelope<{ job_id: string }>>;
}

declare global {
  interface Window {
    pywebview?: { api: PywebviewApi };
  }
}

function hasBridge(): boolean {
  return typeof window !== "undefined" && window.pywebview?.api !== undefined;
}

let readyPromise: Promise<boolean> | null = null;

/**
 * Resolve once the pywebview Python API is injected (it arrives asynchronously
 * and fires a `pywebviewready` event after the page loads). Resolves `true` when
 * the bridge is present, or `false` after `timeoutMs` — i.e. plain-browser dev,
 * where the in-memory mock is used instead. Cached so every caller shares one wait.
 */
export function whenBridgeReady(timeoutMs = 4000): Promise<boolean> {
  if (readyPromise) return readyPromise;
  readyPromise = new Promise<boolean>((resolve) => {
    if (hasBridge()) {
      resolve(true);
      return;
    }
    let settled = false;
    const finish = (value: boolean) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    // pywebview fires this once the API binding is ready.
    window.addEventListener("pywebviewready", () => finish(true), { once: true });
    // Fallback poll in case the event fired before this listener attached.
    const start = Date.now();
    const timer = window.setInterval(() => {
      if (hasBridge()) {
        window.clearInterval(timer);
        finish(true);
      } else if (Date.now() - start > timeoutMs) {
        window.clearInterval(timer);
        finish(false);
      }
    }, 50);
  });
  return readyPromise;
}

async function unwrap<T>(promise: Promise<Envelope<T>>): Promise<T> {
  const result = await promise;
  if (!result.ok) {
    throw new Error(result.error);
  }
  return result.data;
}

export const bridge = {
  available: hasBridge,

  listItems(): Promise<ItemSummary[]> {
    if (!hasBridge()) return mock.listItems();
    return unwrap(window.pywebview!.api.list_items());
  },

  getContext(itemId?: number | null): Promise<ProcessingContext> {
    if (!hasBridge()) return mock.getContext();
    return unwrap(window.pywebview!.api.get_context(itemId ?? null));
  },

  // Fire-and-forget: warm upcoming clips' originals so opening them is instant.
  prefetchOriginals(itemIds: number[]): Promise<{ job_id: string | null }> {
    if (!hasBridge()) return Promise.resolve({ job_id: null });
    return unwrap(window.pywebview!.api.prefetch_originals(itemIds));
  },

  listPublishJobs(itemId: number): Promise<PublishJob[]> {
    if (!hasBridge()) return mock.listPublishJobs();
    return unwrap(window.pywebview!.api.list_publish_jobs(itemId));
  },

  queueForPublish(itemId: number, scheduledAt?: string | null): Promise<QueueResult> {
    if (!hasBridge()) return mock.queueForPublish();
    return unwrap(window.pywebview!.api.queue_for_publish(itemId, scheduledAt ?? null));
  },

  startQueueForPublish(itemId: number, scheduledAt?: string | null): Promise<{ job_id: string }> {
    if (!hasBridge()) return Promise.resolve({ job_id: "mock-queue-publish" });
    return unwrap(window.pywebview!.api.start_queue_for_publish(itemId, scheduledAt ?? null));
  },

  autoScheduleForPublish(itemId: number): Promise<QueueResult> {
    if (!hasBridge()) return mock.autoScheduleForPublish();
    return unwrap(window.pywebview!.api.auto_schedule_for_publish(itemId));
  },

  startAutoScheduleForPublish(itemId: number): Promise<{ job_id: string }> {
    if (!hasBridge()) return Promise.resolve({ job_id: "mock-auto-schedule" });
    return unwrap(window.pywebview!.api.start_auto_schedule_for_publish(itemId));
  },

  syncCloudPublishJobs(): Promise<{ synced: boolean; updated: number }> {
    if (!hasBridge()) return Promise.resolve({ synced: false, updated: 0 });
    return unwrap(window.pywebview!.api.sync_cloud_publish_jobs());
  },

  startPublishNow(
    itemId: number,
    allowRecent = false,
    forceLocal = false,
  ): Promise<{ job_id: string }> {
    if (!hasBridge()) return Promise.resolve({ job_id: "mock-publish" });
    return unwrap(window.pywebview!.api.start_publish_now(itemId, allowRecent, forceLocal));
  },

  publishDueCount(): Promise<{ due: number }> {
    if (!hasBridge()) return Promise.resolve({ due: 0 });
    return unwrap(window.pywebview!.api.publish_due_count());
  },

  itemPublishRecency(itemId: number): Promise<PublishRecency> {
    if (!hasBridge()) return Promise.resolve({ on_cooldown: false });
    return unwrap(window.pywebview!.api.item_publish_recency(itemId));
  },

  duePublishRecency(): Promise<DueRecencyWarning[]> {
    if (!hasBridge()) return Promise.resolve([]);
    return unwrap(window.pywebview!.api.due_publish_recency());
  },

  startPublishDue(allowRecent = false): Promise<{ job_id: string }> {
    if (!hasBridge()) return Promise.resolve({ job_id: "mock-publish-due" });
    return unwrap(window.pywebview!.api.start_publish_due(allowRecent));
  },

  drainPublishEvents(): Promise<PublishEvent[]> {
    if (!hasBridge()) return Promise.resolve([]);
    return unwrap(window.pywebview!.api.drain_publish_events());
  },

  getAutoPublish(): Promise<{ enabled: boolean }> {
    if (!hasBridge()) return Promise.resolve({ enabled: false });
    return unwrap(window.pywebview!.api.get_auto_publish());
  },

  setAutoPublish(enabled: boolean): Promise<{ enabled: boolean }> {
    if (!hasBridge()) return Promise.resolve({ enabled });
    return unwrap(window.pywebview!.api.set_auto_publish(enabled));
  },

  getLatestRevision(itemId: number): Promise<DraftRevision | null> {
    if (!hasBridge()) return mock.getLatest();
    return unwrap(window.pywebview!.api.get_latest_revision(itemId));
  },

  applyRevision(
    itemId: number,
    optionNumber: number,
    revisionId?: number | null,
  ): Promise<ApplyResult> {
    if (!hasBridge()) return mock.apply(optionNumber);
    return unwrap(
      window.pywebview!.api.apply_revision(itemId, optionNumber, revisionId ?? null),
    );
  },

  reviseOption(
    itemId: number,
    optionNumber: number,
    payload: Record<string, unknown>,
  ): Promise<DraftRevision> {
    if (!hasBridge()) return mock.getLatest() as Promise<DraftRevision>;
    return unwrap(window.pywebview!.api.revise_option(itemId, optionNumber, payload));
  },

  saveRevision(
    itemId: number,
    payload: Record<string, unknown>,
  ): Promise<DraftRevision> {
    if (!hasBridge()) return mock.getLatest() as Promise<DraftRevision>;
    return unwrap(window.pywebview!.api.save_revision(itemId, payload));
  },

  setActiveItem(itemId: number): Promise<unknown> {
    if (!hasBridge()) return Promise.resolve(null);
    return unwrap(window.pywebview!.api.set_active_item(itemId));
  },

  canGenerate(): Promise<boolean> {
    if (!hasBridge()) return Promise.resolve(true);
    return unwrap(window.pywebview!.api.can_generate()).then((d) => d.can_generate);
  },

  buildChatPrompt(
    itemId: number,
    payload: Record<string, unknown>,
  ): Promise<{ prompt: string }> {
    if (!hasBridge()) return Promise.resolve({ prompt: "Browser preview prompt" });
    return unwrap(window.pywebview!.api.build_chat_prompt(itemId, payload));
  },

  importPastedDraft(itemId: number, text: string): Promise<DraftRevision> {
    if (!hasBridge()) return mock.getLatest() as Promise<DraftRevision>;
    return unwrap(window.pywebview!.api.import_pasted_draft(itemId, text));
  },

  setProcessingStatus(itemId: number, status: string): Promise<SetProcessingStatusResult> {
    if (!hasBridge()) {
      return Promise.resolve({
        item_id: itemId,
        repost_job_id: itemId + 1000,
        status: status as SetProcessingStatusResult["status"],
        created: true,
      });
    }
    return unwrap(window.pywebview!.api.set_processing_status(itemId, status));
  },

  buildAccountBatchChatPrompt(
    accountId: number,
    itemIds: number[],
    payload: Record<string, unknown>,
  ): Promise<{ prompt: string }> {
    if (!hasBridge())
      return Promise.resolve({
        prompt: itemIds
          .map((itemId, index) => `===== REEL ${index + 1} | item ${itemId} | Mock =====`)
          .join("\n\n"),
      });
    return unwrap(
      window.pywebview!.api.build_account_batch_chat_prompt(accountId, itemIds, payload),
    );
  },

  importAccountBatchDraft(text: string, itemIds: number[]): Promise<BatchDraftImportResult> {
    if (!hasBridge())
      return Promise.resolve({ imported: itemIds, failed: [], unmatched: text ? [] : itemIds });
    return unwrap(window.pywebview!.api.import_account_batch_draft(text, itemIds));
  },

  prepareAccountBatchFrames(itemIds: number[]): Promise<BatchFramesResult> {
    if (!hasBridge())
      return Promise.resolve({
        folder: "C:/mock/batch-frames",
        frames: itemIds.map((itemId, index) => ({
          item_id: itemId,
          path: `C:/mock/batch-frames/reel_${index + 1}_item${itemId}.jpg`,
        })),
      });
    return unwrap(window.pywebview!.api.prepare_account_batch_frames(itemIds));
  },

  openBatchFramesFolder(folder: string): Promise<{ folder: string }> {
    if (!hasBridge()) return Promise.resolve({ folder });
    return unwrap(window.pywebview!.api.open_batch_frames_folder(folder));
  },

  getWorkflowSettings(itemId: number): Promise<WorkflowSettings> {
    if (!hasBridge()) return mock.getWorkflowSettings();
    return unwrap(window.pywebview!.api.get_workflow_settings(itemId));
  },

  saveWorkflowSettings(itemId: number, payload: Record<string, unknown>): Promise<WorkflowSettings> {
    if (!hasBridge()) return mock.getWorkflowSettings();
    return unwrap(window.pywebview!.api.save_workflow_settings(itemId, payload));
  },

  saveFinalDraft(itemId: number, title: string, caption: string) {
    if (!hasBridge()) return Promise.resolve({ title_draft: title, caption_draft: caption });
    return unwrap(window.pywebview!.api.save_final_draft(itemId, title, caption));
  },

  openItemFolder(itemId: number) {
    if (!hasBridge()) return Promise.resolve({ folder: "" });
    return unwrap(window.pywebview!.api.open_item_folder(itemId));
  },

  getCropOverride(itemId: number): Promise<CropRect | null> {
    if (!hasBridge()) return Promise.resolve(null);
    return unwrap(window.pywebview!.api.get_crop_override(itemId));
  },

  getCropPreview(itemId: number): Promise<{ item_id: number; preview_url: string }> {
    if (!hasBridge()) return Promise.resolve({ item_id: itemId, preview_url: "" });
    return unwrap(window.pywebview!.api.get_crop_preview(itemId));
  },

  saveCropOverride(
    itemId: number,
    rect: CropRect,
  ): Promise<{ item_id: number; crop_override: CropRect }> {
    if (!hasBridge()) return Promise.resolve({ item_id: itemId, crop_override: rect });
    return unwrap(window.pywebview!.api.save_crop_override(itemId, rect));
  },

  clearCropOverride(itemId: number): Promise<{ item_id: number; crop_override: null }> {
    if (!hasBridge()) return Promise.resolve({ item_id: itemId, crop_override: null });
    return unwrap(window.pywebview!.api.clear_crop_override(itemId));
  },

  startGeneration(
    itemId: number,
    payload: Record<string, unknown> = {},
  ): Promise<{ job_id: string }> {
    if (!hasBridge()) return mock.startGeneration();
    return unwrap(window.pywebview!.api.start_generation(itemId, payload));
  },

  startExport(itemId: number): Promise<{ job_id: string }> {
    if (!hasBridge()) return mock.startExport();
    return unwrap(window.pywebview!.api.start_export(itemId));
  },

  startItemDownload(itemId: number): Promise<{ job_id: string }> {
    if (!hasBridge()) return Promise.resolve({ job_id: "mock-job" });
    return unwrap(window.pywebview!.api.start_item_download(itemId));
  },

  getJob(jobId: string): Promise<JobSnapshot> {
    if (!hasBridge()) return mock.getJob();
    return unwrap(window.pywebview!.api.get_job(jobId));
  },

  listAccounts(): Promise<AccountSummary[]> {
    if (!hasBridge()) return mock.listAccounts();
    return unwrap(window.pywebview!.api.list_accounts());
  },

  getAccount(accountId: number): Promise<AccountDetail> {
    if (!hasBridge()) return mock.getAccount();
    return unwrap(window.pywebview!.api.get_account(accountId));
  },

  createAccount(payload: Record<string, unknown>): Promise<AccountDetail> {
    if (!hasBridge()) return mock.getAccount();
    return unwrap(window.pywebview!.api.create_account(payload));
  },

  updateAccount(accountId: number, payload: Record<string, unknown>): Promise<AccountDetail> {
    if (!hasBridge()) return mock.getAccount();
    return unwrap(window.pywebview!.api.update_account(accountId, payload));
  },

  deleteAccount(accountId: number): Promise<DeleteAccountResult> {
    if (!hasBridge())
      return Promise.resolve({
        deleted_account_id: accountId,
        unassigned_download_items: 0,
        removed_upload_jobs: 0,
        removed_assignments: 0,
      });
    return unwrap(window.pywebview!.api.delete_account(accountId));
  },

  getActiveAccount(): Promise<{ active_account_id: number | null }> {
    if (!hasBridge()) return Promise.resolve({ active_account_id: 1 });
    return unwrap(window.pywebview!.api.get_active_account());
  },

  setActiveAccount(accountId: number | null): Promise<{ active_account_id: number | null }> {
    if (!hasBridge()) return Promise.resolve({ active_account_id: accountId });
    return unwrap(window.pywebview!.api.set_active_account(accountId));
  },

  listLibraryItems(accountId?: number | null): Promise<LibraryItem[]> {
    if (!hasBridge()) return mock.listLibraryItems();
    return unwrap(window.pywebview!.api.list_library_items(accountId ?? null));
  },

  assignAccount(itemId: number, accountId: number | null) {
    if (!hasBridge())
      return Promise.resolve({ item_id: itemId, account_id: accountId, account_name: null });
    return unwrap(window.pywebview!.api.assign_account(itemId, accountId));
  },

  removeLibraryItem(itemId: number) {
    if (!hasBridge()) return Promise.resolve({ removed_item_id: itemId, deleted_revisions: 0 });
    return unwrap(window.pywebview!.api.remove_library_item(itemId));
  },

  removeItemFromPool(
    itemId: number,
    reason = "manual removal",
  ): Promise<{ item_id: number; removed_pool_items: number }> {
    if (!hasBridge()) return Promise.resolve({ item_id: itemId, removed_pool_items: 0 });
    return unwrap(window.pywebview!.api.remove_item_from_pool(itemId, reason));
  },

  rejectItem(
    itemId: number,
    reason = "low_quality",
  ): Promise<{
    item_id: number;
    rejected_candidates: number;
    removed_pool_items: number;
    review_state: string;
  }> {
    if (!hasBridge())
      return Promise.resolve({
        item_id: itemId,
        rejected_candidates: 0,
        removed_pool_items: 0,
        review_state: "rejected",
      });
    return unwrap(window.pywebview!.api.reject_item(itemId, reason));
  },

  rejectItemGlobally(
    itemId: number,
    reason = "globally rejected",
  ): Promise<{
    item_id: number;
    removed_pool_items: number;
    dropped_assignments: number;
    review_state: string;
    blocked: boolean;
  }> {
    if (!hasBridge())
      return Promise.resolve({
        item_id: itemId,
        removed_pool_items: 0,
        dropped_assignments: 0,
        review_state: "blocked",
        blocked: true,
      });
    return unwrap(window.pywebview!.api.reject_item_globally(itemId, reason));
  },

  listPublishQueue(accountId?: number | null): Promise<PublishQueueJob[]> {
    if (!hasBridge()) return mock.listPublishQueue();
    return unwrap(window.pywebview!.api.list_publish_queue(accountId ?? null));
  },

  markJobPosted(jobId: number, payload: Record<string, unknown>): Promise<PublishQueueJob> {
    if (!hasBridge()) return mock.publishQueueJob();
    return unwrap(window.pywebview!.api.mark_job_posted(jobId, payload));
  },

  updateJobMetrics(jobId: number, payload: Record<string, unknown>): Promise<PublishQueueJob> {
    if (!hasBridge()) return mock.publishQueueJob();
    return unwrap(window.pywebview!.api.update_job_metrics(jobId, payload));
  },

  rescheduleJob(jobId: number, scheduledAt: string): Promise<PublishQueueJob> {
    if (!hasBridge()) return mock.publishQueueJob();
    return unwrap(window.pywebview!.api.reschedule_job(jobId, scheduledAt));
  },

  unscheduleJob(jobId: number): Promise<PublishQueueJob> {
    if (!hasBridge()) return mock.publishQueueJob();
    return unwrap(window.pywebview!.api.unschedule_job(jobId));
  },

  removePublishJob(jobId: number) {
    if (!hasBridge()) return Promise.resolve({ removed_job_id: jobId });
    return unwrap(window.pywebview!.api.remove_publish_job(jobId));
  },

  poolingOverview(): Promise<PoolingOverview> {
    if (!hasBridge()) return mock.poolingOverview();
    return unwrap(window.pywebview!.api.pooling_overview());
  },

  listPoolItems(niche: string): Promise<PoolClip[]> {
    if (!hasBridge()) return mock.listPoolItems();
    return unwrap(window.pywebview!.api.list_pool_items(niche));
  },

  listPoolSources(niche: string): Promise<PoolSource[]> {
    if (!hasBridge()) return Promise.resolve([]);
    return unwrap(window.pywebview!.api.list_pool_sources(niche));
  },

  listPoolSourceClips(
    niche: string,
    sourceLabel: string,
    includeRemoved = false,
  ): Promise<PoolSourceClip[]> {
    if (!hasBridge()) return Promise.resolve([]);
    return unwrap(
      window.pywebview!.api.list_pool_source_clips(niche, sourceLabel, includeRemoved),
    );
  },

  poolReviewQueue(niche: string, sourceLabel?: string | null): Promise<PoolReviewItem[]> {
    if (!hasBridge()) return mock.poolReviewQueue(niche, sourceLabel);
    return unwrap(window.pywebview!.api.pool_review_queue(niche, sourceLabel ?? null));
  },

  poolItemPreview(poolItemId: number): Promise<PoolItemPreview> {
    if (!hasBridge()) return mock.poolItemPreview(poolItemId);
    return unwrap(window.pywebview!.api.pool_item_preview(poolItemId));
  },

  startPoolItemPreviewDownload(poolItemId: number): Promise<{ job_id: string }> {
    if (!hasBridge()) return Promise.resolve({ job_id: "mock-pool-preview-dl" });
    return unwrap(window.pywebview!.api.start_pool_item_preview_download(poolItemId));
  },

  approvePoolItems(ids: number[]): Promise<{ approved: number }> {
    if (!hasBridge()) return Promise.resolve({ approved: ids.length });
    return unwrap(window.pywebview!.api.approve_pool_items(ids));
  },

  rejectPoolItems(ids: number[], reason: string): Promise<{ rejected: number }> {
    if (!hasBridge()) return Promise.resolve({ rejected: ids.length });
    return unwrap(window.pywebview!.api.reject_pool_items(ids, reason));
  },

  removePoolItem(
    poolItemId: number,
    reason = "manual removal",
  ): Promise<{ pool_item_id: number; acceptance_status: string }> {
    if (!hasBridge())
      return Promise.resolve({ pool_item_id: poolItemId, acceptance_status: "removed" });
    return unwrap(window.pywebview!.api.remove_pool_item(poolItemId, reason));
  },

  restorePoolItem(
    poolItemId: number,
  ): Promise<{ pool_item_id: number; acceptance_status: string }> {
    if (!hasBridge())
      return Promise.resolve({ pool_item_id: poolItemId, acceptance_status: "accepted" });
    return unwrap(window.pywebview!.api.restore_pool_item(poolItemId));
  },

  poolNicheAccounts(niche: string): Promise<NicheAccount[]> {
    if (!hasBridge()) return Promise.resolve([]);
    return unwrap(window.pywebview!.api.pool_niche_accounts(niche));
  },

  distributePoolItem(
    poolItemId: number,
    accountIds: number[],
  ): Promise<{ pool_item_id: number; assigned: number }> {
    if (!hasBridge())
      return Promise.resolve({ pool_item_id: poolItemId, assigned: accountIds.length });
    return unwrap(window.pywebview!.api.distribute_pool_item(poolItemId, accountIds));
  },

  distributeNiche(niche: string, maxPerAccount?: number | null): Promise<DistributeNicheResult> {
    if (!hasBridge())
      return Promise.resolve({ niche, assigned: 0, pinned: 0, download_failures: 0, max_per_account: maxPerAccount ?? 28, accounts: [], reason: "no_accounts" as const });
    return unwrap(window.pywebview!.api.distribute_niche(niche, maxPerAccount ?? null));
  },

  distributeNicheExplicit(niche: string, targets: Record<number, number>): Promise<DistributeNicheResult> {
    if (!hasBridge()) return Promise.resolve({ niche, assigned: 0, pinned: 0, download_failures: 0, max_per_account: null, accounts: [] });
    return unwrap(window.pywebview!.api.distribute_niche_explicit(niche, targets));
  },

  dashboardPublishJobs(): Promise<DashboardPublishQueue> {
    if (!hasBridge())
      return Promise.resolve({ jobs: [], due_count: 0, draft: 0, ready: 0, scheduled: 0, failed: 0, unscheduled_exports: [] });
    return unwrap(window.pywebview!.api.dashboard_publish_jobs());
  },

  dashboardScheduleCoverage(): Promise<ScheduleCoverage> {
    if (!hasBridge())
      return Promise.resolve({
        horizon_days: 2,
        accounts: [
          {
            account_id: 1,
            account_name: "Mock Movie Account",
            timezone: "Asia/Jakarta",
            daily_target: 2,
            auto_schedule_on_export: true,
            filled: 2,
            total: 4,
            days: [
              {
                date: new Date().toISOString().slice(0, 10),
                is_today: true,
                filled: 2,
                total: 2,
                slots: [
                  {
                    slot: "09:00",
                    slot_at: new Date().toISOString(),
                    state: "posted",
                    job_id: 1,
                    item_id: 101,
                    job_title: "Mock posted reel",
                    scheduled_at: new Date().toISOString(),
                    note: null,
                    timing: "on_time",
                  },
                  {
                    slot: "18:00",
                    slot_at: new Date().toISOString(),
                    state: "cloud",
                    job_id: 2,
                    item_id: 102,
                    job_title: "Mock cloud reel",
                    scheduled_at: new Date(Date.now() + 3_600_000).toISOString(),
                    note: "same-account cooldown until " + new Date(Date.now() + 3_600_000).toISOString(),
                    timing: "late",
                  },
                ],
              },
              {
                date: new Date(Date.now() + 86_400_000).toISOString().slice(0, 10),
                is_today: false,
                filled: 0,
                total: 2,
                slots: [
                  {
                    slot: "09:00",
                    slot_at: new Date(Date.now() + 86_400_000).toISOString(),
                    state: "open",
                    job_id: null,
                    item_id: null,
                    job_title: null,
                    scheduled_at: null,
                    note: null,
                    timing: null,
                  },
                  {
                    slot: "18:00",
                    slot_at: new Date(Date.now() + 86_400_000).toISOString(),
                    state: "open",
                    job_id: null,
                    item_id: null,
                    job_title: null,
                    scheduled_at: null,
                    note: null,
                    timing: null,
                  },
                ],
              },
            ],
          },
        ],
      });
    return unwrap(window.pywebview!.api.dashboard_schedule_coverage());
  },

  cloudPublisherHealth(): Promise<CloudPublisherHealth> {
    if (!hasBridge())
      return Promise.resolve({
        publish_mode: "live",
        stored_bytes: 5_766_268,
        max_stored_bytes: 8_000_000_000,
        remaining_bytes: 7_994_233_732,
        usage_percent: 0.07,
        active_jobs: 1,
        max_active_jobs: 150,
        active_usage_percent: 0.67,
        active_jobs_by_status: { scheduled: 1 },
        oldest_active_created_at: new Date(Date.now() - 20 * 60_000).toISOString(),
        oldest_active_age_minutes: 20,
        max_upload_bytes: 95_000_000,
        stale_jobs: {
          awaiting_upload_over_minutes: 120,
          awaiting_upload: 0,
          processing_over_minutes: 120,
          processing: 0,
          processing_age_unknown: 0,
          scheduled_past_due: 0,
          oldest_scheduled_at: null,
        },
      });
    return unwrap(window.pywebview!.api.cloud_publisher_health());
  },

  dashboardCloudAccountSettings(accountId: number): Promise<CloudAccountSettings | null> {
    if (!hasBridge()) return Promise.resolve(null);
    return unwrap(window.pywebview!.api.dashboard_cloud_account_settings(accountId));
  },

  dashboardUpdateCloudAccountSettings(
    accountId: number,
    dailyLimit: number,
    minGapMinutes: number,
    enabled: boolean,
  ): Promise<CloudAccountSettings> {
    if (!hasBridge())
      return Promise.resolve({
        account_key: "mock",
        instagram_user_id: "0",
        token_secret_name: "IG_TOKEN_MOCK",
        enabled,
        daily_limit: dailyLimit,
        min_gap_minutes: minGapMinutes,
      });
    return unwrap(
      window.pywebview!.api.dashboard_update_cloud_account_settings(
        accountId,
        dailyLimit,
        minGapMinutes,
        enabled,
      ),
    );
  },

  dashboardForcePublishCloudJob(jobId: number): Promise<{ id: string; forced: boolean }> {
    if (!hasBridge()) return Promise.resolve({ id: "mock", forced: true });
    return unwrap(window.pywebview!.api.dashboard_force_publish_cloud_job(jobId));
  },

  dashboardAccountStats(activeAccountId: number): Promise<DashboardAccountStats> {
    if (!hasBridge()) return Promise.resolve({ niche: "history", accounts: [] });
    return unwrap(window.pywebview!.api.dashboard_account_stats(activeAccountId));
  },

  dashboardMarkReady(jobIds: number[]): Promise<{ updated: number }> {
    if (!hasBridge()) return Promise.resolve({ updated: jobIds.length });
    return unwrap(window.pywebview!.api.dashboard_mark_ready(jobIds));
  },

  dashboardOpenOutput(jobId: number): Promise<{ opened: string }> {
    if (!hasBridge()) return Promise.resolve({ opened: String(jobId) });
    return unwrap(window.pywebview!.api.dashboard_open_output(jobId));
  },

  dashboardAccountReadiness(): Promise<AccountReadiness> {
    if (!hasBridge()) return Promise.resolve({ rows: [], totals: { account_count: 0, total_due_now: 0, total_scheduled: 0, blocked_accounts: 0, next_post_at: null } });
    return unwrap(window.pywebview!.api.dashboard_account_readiness());
  },

  dashboardStartLiveHealthCheck(): Promise<{ job_id: string }> {
    if (!hasBridge()) return Promise.resolve({ job_id: "mock-health" });
    return unwrap(window.pywebview!.api.dashboard_start_live_health_check());
  },

  dashboardRelogin(accountId: number): Promise<{ profile: string }> {
    if (!hasBridge()) return Promise.resolve({ profile: String(accountId) });
    return unwrap(window.pywebview!.api.dashboard_relogin(accountId));
  },

  listSources(accountId: number): Promise<SourceProfile[]> {
    if (!hasBridge()) return mock.listSources();
    return unwrap(window.pywebview!.api.list_sources(accountId));
  },

  addSource(accountId: number, sourceUrl: string): Promise<SourceProfile> {
    if (!hasBridge()) return mock.source();
    return unwrap(window.pywebview!.api.add_source(accountId, sourceUrl));
  },

  setSourceEnabled(sourceId: number, enabled: boolean): Promise<SourceProfile> {
    if (!hasBridge()) return mock.source();
    return unwrap(window.pywebview!.api.set_source_enabled(sourceId, enabled));
  },

  removeSource(sourceId: number): Promise<{ removed_source_id: number }> {
    if (!hasBridge()) return Promise.resolve({ removed_source_id: sourceId });
    return unwrap(window.pywebview!.api.remove_source(sourceId));
  },

  listCandidates(accountId: number, state: string): Promise<ScrapeCandidate[]> {
    if (!hasBridge()) return mock.listCandidates();
    return unwrap(window.pywebview!.api.list_candidates(accountId, state));
  },

  setCandidateState(
    candidateId: number,
    state: string,
  ): Promise<{ candidate_id: number; state: string }> {
    if (!hasBridge()) return Promise.resolve({ candidate_id: candidateId, state });
    return unwrap(window.pywebview!.api.set_candidate_state(candidateId, state));
  },

  acceptCandidate(
    candidateId: number,
  ): Promise<{ candidate_id: number; state: string; pool_item_id: number; niche: string }> {
    if (!hasBridge())
      return Promise.resolve({
        candidate_id: candidateId,
        state: "pooled",
        pool_item_id: candidateId,
        niche: "history",
      });
    return unwrap(window.pywebview!.api.accept_candidate(candidateId));
  },

  rejectCandidate(
    candidateId: number,
    reason: string,
  ): Promise<{ candidate_id: number; state: string; removed_pool_items: number }> {
    if (!hasBridge())
      return Promise.resolve({
        candidate_id: candidateId,
        state: `rejected_${reason}`,
        removed_pool_items: 0,
      });
    return unwrap(window.pywebview!.api.reject_candidate(candidateId, reason));
  },

  apifyUsage(): Promise<ApifyUsage> {
    if (!hasBridge())
      return Promise.resolve({
        month: "",
        used: 0,
        free_cap: 1850,
        remaining: 1850,
        over_free_tier: false,
        warn: false,
      });
    return unwrap(window.pywebview!.api.apify_usage());
  },

  startSourceScrape(sourceId: number, maxItems = 30): Promise<{ job_id: string }> {
    if (!hasBridge()) return Promise.resolve({ job_id: "mock-scrape" });
    return unwrap(window.pywebview!.api.start_source_scrape(sourceId, maxItems));
  },

  startCandidateDownload(candidateId: number): Promise<{ job_id: string }> {
    if (!hasBridge()) return Promise.resolve({ job_id: "mock-candidate-dl" });
    return unwrap(window.pywebview!.api.start_candidate_download(candidateId));
  },
};

// --- browser-only mock ---------------------------------------------------- //

const mockRevision: DraftRevision = {
  id: 1,
  download_item_id: 1,
  revision_number: 1,
  source: "codex",
  created_at: new Date().toISOString(),
  summary: "A famous one-take movie moment.",
  title_options: [
    "The take that fooled everyone",
    "One shot — no second chances",
    "He nailed it first try",
  ],
  caption_options: [
    "A single take changed the scene.",
    "No retakes. Just nerve.",
    "First try, on camera.",
  ],
  option_notes: ["Curiosity hook", "Tension hook", "Plain flex"],
  option_tiers: ["green", "yellow", "yellow"],
  recommended_title_index: 2,
  recommended_caption_index: 2,
  recommendation_reason: "Punchy and curiosity-driven.",
  title_style_preset: null,
  caption_style_preset: null,
  provider_label: "Codex (mock)",
  generation_meta: null,
  vision_payload: null,
  applied_at: null,
  applied_title_index: null,
  applied_caption_index: null,
};

const mock = {
  async getContext(): Promise<ProcessingContext> {
    return {
      item: {
        id: 1,
        source_url: "https://instagram.com/reel/mock",
        title: "Mock clip (browser dev — no pywebview)",
        source_description: null,
        file_path: "C:/clips/mock.mp4",
        processed_path: null,
        preview_url: null,
        original_preview_url: null,
        exported_preview_url: null,
        status: "completed",
        review_state: "new",
        transcript_text: "",
        transcript_truncated: false,
        title_draft: null,
        caption_draft: null,
        title_style_preset: null,
        caption_style_preset: null,
      },
      account: {
        id: 1,
        name: "Mock Movie Account",
        platform: "instagram",
        niche_label: "movie",
        niche: "movie",
        writing_tone: "cinematic",
        target_audience: null,
        hook_style: null,
        banned_phrases: null,
        title_style_notes: null,
        caption_style_notes: null,
        auto_schedule_on_export: false,
      },
      latest_revision: mockRevision,
      revision_count: 1,
    };
  },
  async getLatest(): Promise<DraftRevision | null> {
    return mockRevision;
  },
  async apply(optionNumber: number): Promise<ApplyResult> {
    return {
      item_id: 1,
      revision_id: 1,
      revision_number: 1,
      applied_option: optionNumber,
      title_draft: mockRevision.title_options[optionNumber - 1] ?? "",
      caption_draft: mockRevision.caption_options[optionNumber - 1] ?? "",
    };
  },
  async startGeneration(): Promise<{ job_id: string }> {
    return { job_id: "mock-job" };
  },
  async startExport(): Promise<{ job_id: string }> {
    return { job_id: "mock-export" };
  },
  async listItems() {
    return [
      {
        id: 1,
        title: "Mock clip (browser dev)",
        source_url: "https://instagram.com/reel/mock",
        account_id: 1,
        status: "completed",
        has_processed: false,
        has_draft: true,
      },
    ];
  },
  async listPublishJobs() {
    return [];
  },
  async queueForPublish() {
    return { job_id: 1, status: "draft", scheduled_at: null, created: true };
  },
  async autoScheduleForPublish() {
    return {
      job_id: 1,
      status: "scheduled",
      scheduled_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
      created: true,
    };
  },
  async getJob(jobId?: string): Promise<JobSnapshot> {
    if (jobId === "mock-queue-publish" || jobId === "mock-auto-schedule") {
      return {
        id: jobId,
        status: "succeeded",
        progress: 1,
        message: "Scheduled",
        result: {
          job_id: 1,
          status: "cloud",
          scheduled_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
          created: false,
        },
        error: null,
      };
    }
    return {
      id: "mock-job",
      status: "succeeded",
      progress: 1,
      message: "Done",
      result: mockRevision,
      error: null,
    };
  },
  async listAccounts(): Promise<AccountSummary[]> {
    return [
      {
        id: 1,
        name: "Mock Movie Account",
        platform: "instagram",
        niche_label: "movie",
        niche: "movie",
        instagram_handle: "mockmovies",
      },
    ];
  },
  publishQueueJob(): Promise<PublishQueueJob> {
    return Promise.resolve({
      id: 1,
      account_id: 1,
      account_name: "Mock Movie Account",
      download_item_id: 1,
      title: "Mock reel",
      status: "draft",
      scheduled_at: null,
      posted_at: null,
      posted_url: null,
      posted_views: null,
      posted_likes: null,
      posted_comments: null,
      posted_shares: null,
      content_type: null,
      processed_path: "C:/processed/mock.mp4",
    });
  },
  async listPublishQueue(): Promise<PublishQueueJob[]> {
    return [await mock.publishQueueJob()];
  },
  async poolingOverview(): Promise<PoolingOverview> {
    return {
      niches: [
        {
          niche: "history",
          pooled: 3,
          assigned: 2,
          unused: 1,
          rejected: 0,
          pending: 1,
          assignments_by_account: [
            { account_id: 1, account_name: "Past Moments Daily", count: 2 },
          ],
        },
        {
          niche: "movie",
          pooled: 0,
          assigned: 0,
          unused: 0,
          rejected: 0,
          pending: 0,
          assignments_by_account: [],
        },
      ],
    };
  },
  async listPoolItems(): Promise<PoolClip[]> {
    return [
      {
        pool_item_id: 1,
        clip_label: "abc123",
        source_label: "thehistologian",
        accepted_at: new Date().toISOString(),
        distributed_to: ["Past Moments Daily"],
        is_distributed: true,
      },
    ];
  },
  async poolReviewQueue(niche: string, sourceLabel?: string | null): Promise<PoolReviewItem[]> {
    const rows: PoolReviewItem[] = [
      {
        pool_item_id: 101,
        niche,
        clip_label: "Mock pending reel",
        source_label: "thehistologian",
        created_at: new Date().toISOString(),
        thumbnail_url: null,
        source_url: "https://www.instagram.com/reel/mock",
        preview_url: null,
        fit_score: 0.057,
        source_er: 0.045,
        topic_tier: "S",
        suggested_action: "accept",
        view_count: 56789,
        like_count: 1234,
        comment_count: 42,
        duration_seconds: 18,
        description: "Browser-dev mock pending pool clip.",
        channel_name: "thehistologian",
        published_at: new Date(Date.now() - 3 * 86400 * 1000).toISOString(),
      },
    ];
    return sourceLabel ? rows.filter((row) => row.source_label === sourceLabel) : rows;
  },
  async poolItemPreview(poolItemId: number): Promise<PoolItemPreview> {
    return {
      pool_item_id: poolItemId,
      preview_url: null,
      thumbnail_url: null,
      source_url: "https://www.instagram.com/reel/mock",
    };
  },
  async source(): Promise<SourceProfile> {
    return {
      id: 1,
      label: "@thehistologian",
      source_url: "https://www.instagram.com/thehistologian",
      source_type: "instagram_profile",
      platform: "instagram",
      enabled: true,
      priority: 100,
      last_scraped_at: null,
      last_run_status: null,
      last_error_summary: null,
    };
  },
  async listSources(): Promise<SourceProfile[]> {
    return [await mock.source()];
  },
  async listCandidates(): Promise<ScrapeCandidate[]> {
    return [
      {
        id: 1,
        title: "Mock candidate clip",
        source_url: "https://instagram.com/reel/mock",
        channel_name: "thehistologian",
        state: "candidate",
        like_count: 1234,
        view_count: 56789,
        comment_count: 42,
        duration_seconds: 18,
        description: "Mock candidate description for browser dev.",
        published_at: new Date().toISOString(),
        created_at: new Date().toISOString(),
        thumbnail_url: null,
      },
    ];
  },
  async listLibraryItems(): Promise<LibraryItem[]> {
    return [
      {
        id: 1,
        account_seq: 1,
        title: "Mock clip (browser dev)",
        source_url: "https://instagram.com/reel/mock",
        status: "draft",
        raw_status: "completed",
        reopened: false,
        review_state: "new",
        file_path: "C:/clips/mock.mp4",
        has_file: true,
        has_processed: false,
        has_draft: true,
        account_id: 1,
        account_name: "Mock Movie Account",
        created_at: new Date().toISOString(),
        is_new: true,
      },
    ];
  },
  async getAccount(): Promise<AccountDetail> {
    return {
      id: 1,
      name: "Mock Movie Account",
      platform: "instagram",
      niche_label: "movie",
      niche: "movie",
      instagram_handle: "mockmovies",
      login_identifier: null,
      instagram_profile: null,
      credential_blob: null,
      writing_tone: "cinematic",
      target_audience: null,
      hook_style: null,
      banned_phrases: null,
      title_style_notes: null,
      caption_style_notes: null,
      upload_timezone: "Asia/Jakarta",
      upload_default_privacy: "private",
      upload_schedule_slots: null,
      daily_posts_target: null,
      distribute_daily_target: null,
      auto_schedule_on_export: false,
      download_item_count: 0,
      upload_job_count: 0,
    };
  },
  async getWorkflowSettings(): Promise<WorkflowSettings> {
    return {
      clip_premise: "",
      caption_style: "contextual_info",
      title_style: "",
      title_length: "long",
      template: "gaming_meme_black",
      title_draft: "",
      caption_draft: "",
      caption_style_options: [{ value: "contextual_info", label: "Context / info" }],
      title_style_options: [{ value: "", label: "Auto" }],
      title_length_options: [
        { value: "short", label: "Short (5-9 words)" },
        { value: "medium", label: "Medium (10-16 words)" },
        { value: "long", label: "Long (15-28 words)" },
        { value: "auto", label: "Auto mix" },
      ],
      template_options: [{ value: "gaming_meme_black", label: "Gaming Meme Black" }],
    };
  },
};
