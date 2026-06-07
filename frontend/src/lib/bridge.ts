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
  ApplyResult,
  DeleteAccountResult,
  DraftRevision,
  DashboardPublishQueue,
  ItemSummary,
  JobSnapshot,
  LibraryItem,
  PoolClip,
  PoolSource,
  PoolSourceClip,
  PoolingOverview,
  ProcessingContext,
  PublishJob,
  PublishQueueJob,
  AccountReadiness,
  QueueResult,
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
  start_generation(
    itemId: number,
    payload: Record<string, unknown>,
  ): Promise<Envelope<{ job_id: string }>>;
  start_export(itemId: number): Promise<Envelope<{ job_id: string }>>;
  get_job(jobId: string): Promise<Envelope<JobSnapshot>>;
  list_publish_jobs(itemId: number): Promise<Envelope<PublishJob[]>>;
  queue_for_publish(
    itemId: number,
    scheduledAt?: string | null,
  ): Promise<Envelope<QueueResult>>;
  auto_schedule_for_publish(itemId: number): Promise<Envelope<QueueResult>>;
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
  list_publish_queue(accountId?: number | null): Promise<Envelope<PublishQueueJob[]>>;
  mark_job_posted(
    jobId: number,
    payload: Record<string, unknown>,
  ): Promise<Envelope<PublishQueueJob>>;
  reschedule_job(jobId: number, scheduledAt: string): Promise<Envelope<PublishQueueJob>>;
  unschedule_job(jobId: number): Promise<Envelope<PublishQueueJob>>;
  remove_publish_job(jobId: number): Promise<Envelope<{ removed_job_id: number }>>;
  pooling_overview(): Promise<Envelope<PoolingOverview>>;
  list_pool_items(niche: string): Promise<Envelope<PoolClip[]>>;
  list_pool_sources(niche: string): Promise<Envelope<PoolSource[]>>;
  list_pool_source_clips(niche: string, sourceLabel: string): Promise<Envelope<PoolSourceClip[]>>;
  dashboard_publish_jobs(): Promise<Envelope<DashboardPublishQueue>>;
  dashboard_mark_ready(jobIds: number[]): Promise<Envelope<{ updated: number }>>;
  dashboard_open_output(jobId: number): Promise<Envelope<{ opened: string }>>;
  dashboard_account_readiness(): Promise<Envelope<AccountReadiness>>;
  dashboard_start_live_health_check(): Promise<Envelope<{ job_id: string }>>;
  dashboard_relogin(accountId: number): Promise<Envelope<{ profile: string }>>;
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

  listPublishJobs(itemId: number): Promise<PublishJob[]> {
    if (!hasBridge()) return mock.listPublishJobs();
    return unwrap(window.pywebview!.api.list_publish_jobs(itemId));
  },

  queueForPublish(itemId: number, scheduledAt?: string | null): Promise<QueueResult> {
    if (!hasBridge()) return mock.queueForPublish();
    return unwrap(window.pywebview!.api.queue_for_publish(itemId, scheduledAt ?? null));
  },

  autoScheduleForPublish(itemId: number): Promise<QueueResult> {
    if (!hasBridge()) return mock.autoScheduleForPublish();
    return unwrap(window.pywebview!.api.auto_schedule_for_publish(itemId));
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

  listPublishQueue(accountId?: number | null): Promise<PublishQueueJob[]> {
    if (!hasBridge()) return mock.listPublishQueue();
    return unwrap(window.pywebview!.api.list_publish_queue(accountId ?? null));
  },

  markJobPosted(jobId: number, payload: Record<string, unknown>): Promise<PublishQueueJob> {
    if (!hasBridge()) return mock.publishQueueJob();
    return unwrap(window.pywebview!.api.mark_job_posted(jobId, payload));
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

  listPoolSourceClips(niche: string, sourceLabel: string): Promise<PoolSourceClip[]> {
    if (!hasBridge()) return Promise.resolve([]);
    return unwrap(window.pywebview!.api.list_pool_source_clips(niche, sourceLabel));
  },

  dashboardPublishJobs(): Promise<DashboardPublishQueue> {
    if (!hasBridge()) return Promise.resolve({ jobs: [], due_count: 0, draft: 0, ready: 0, scheduled: 0 });
    return unwrap(window.pywebview!.api.dashboard_publish_jobs());
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
  async getJob(): Promise<JobSnapshot> {
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
  async listLibraryItems(): Promise<LibraryItem[]> {
    return [
      {
        id: 1,
        title: "Mock clip (browser dev)",
        source_url: "https://instagram.com/reel/mock",
        status: "draft",
        raw_status: "completed",
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
      download_item_count: 0,
      upload_job_count: 0,
    };
  },
  async getWorkflowSettings(): Promise<WorkflowSettings> {
    return {
      clip_premise: "",
      caption_style: "contextual_info",
      title_style: "",
      template: "gaming_meme_black",
      title_draft: "",
      caption_draft: "",
      caption_style_options: [{ value: "contextual_info", label: "Context / info" }],
      title_style_options: [{ value: "", label: "Auto" }],
      template_options: [{ value: "gaming_meme_black", label: "Gaming Meme Black" }],
    };
  },
};
