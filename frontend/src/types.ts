// Mirrors nicheflow_studio.services.draft_revisions DTOs and active_context().

export interface DraftRevision {
  id: number;
  download_item_id: number;
  revision_number: number;
  source: string;
  created_at: string | null;
  summary: string | null;
  title_options: string[];
  caption_options: string[];
  option_notes: string[];
  option_tiers: string[];
  recommended_title_index: number | null;
  recommended_caption_index: number | null;
  recommendation_reason: string | null;
  title_style_preset: string | null;
  caption_style_preset: string | null;
  provider_label: string | null;
  generation_meta: unknown;
  vision_payload: unknown;
  applied_at: string | null;
  applied_title_index: number | null;
  applied_caption_index: number | null;
}

export interface BatchFrame {
  item_id: number;
  path: string;
}

export interface BatchFramesResult {
  folder: string;
  /** Only the reels that still need an attached still; vision-backed reels are omitted. */
  frames: BatchFrame[];
  /** Reels whose visual evidence JSON covers them, so no image is attached. */
  described?: number[];
  skipped?: { item_id: number; reason: string }[];
}

export interface BatchDraftImportResult {
  imported: number[];
  failed: { item_id: number; error: string }[];
  /** Reels rejected after the batch was prepared: the reply still carried a
   *  block for them, but no draft was saved. Distinct from `unmatched`. */
  skipped: { item_id: number; reason: string }[];
  unmatched: number[];
}

export interface BatchCandidateItem {
  id: number;
  /** Per-account "#N" shown in Processing; also what the batch prompt/paste-router keys on. */
  account_seq: number | null;
  title: string | null;
  source_url: string | null;
  /** Virtual-host URL for the source clip, or null until media mapping is ready. */
  preview_url: string | null;
  has_draft: boolean;
  has_vision: boolean;
}

/** One account's draftless reels, offered for a cross-account batch. */
export interface BatchCandidateGroup {
  account_id: number;
  account_name: string;
  niche: string | null;
  auto_schedules: boolean;
  items: BatchCandidateItem[];
  /** Total eligible reels for this account, before the per-account limit. */
  available: number;
  /** Draftable reels with no local footage yet, so not offered. May be stalled. */
  pending_media: number;
}

/** What a "Fetch footage" retry recovered across the stuck distributed reels. */
export interface FetchMissingMediaResult {
  stuck_rows: number;
  /** Rows fixed by re-linking footage a sibling account had already pulled. */
  linked_from_disk: number;
  fetched: number;
  retired_gone: number;
  /** Rows with no shortcode or registered asset — open them in Processing. */
  unresolved: number;
  failures: { shortcode: string; error: string }[];
}

/** What "Finish batch" would do to one reel, before it runs. */
export interface FinishBatchPlanEntry {
  item_id: number;
  /** Per-account "#N", so results can name a reel the way Processing does. */
  account_seq: number | null;
  title: string | null;
  account_id: number | null;
  account_name: string | null;
  auto_schedules: boolean;
  /** True when this account's post is handed to the Cloudflare Worker rather
   *  than published from this machine. */
  publishes_via_cloud: boolean;
  ready?: boolean;
  reason?: string;
  option?: number;
  revision_id?: number;
}

/** What queue_for_publish reported for one auto-scheduled reel. */
export interface FinishBatchSchedule {
  job_id?: number;
  /** "cloud" once handed off to the Worker, "scheduled" while local-only. */
  status?: string;
  /**
   * "deferred" when the Worker upload was handed to a background thread instead
   * of awaited (batch exports pipeline it against the next render). The reel is
   * cloud-bound even though `status` still reads "scheduled".
   */
  cloud_handoff?: string;
  scheduled_at?: string | null;
  message?: string;
}

export interface FinishBatchResult {
  applied: { item_id: number; option: number }[];
  exported: { item_id: number; processed_path: string | null }[];
  scheduled: { item_id: number; schedule: FinishBatchSchedule }[];
  failed: { item_id: number; stage: string; error: string }[];
  skipped: { item_id: number; reason: string }[];
  // Reels whose Worker upload was still running on a background thread when the
  // batch job finished. They read "Scheduled" in the library until it lands.
  pending_cloud?: number;
}


export interface ProcessingItem {
  id: number;
  source_url: string;
  title: string | null;
  source_description: string | null;
  file_path: string | null;
  processed_path: string | null;
  preview_url: string | null;
  original_preview_url: string | null;
  exported_preview_url: string | null;
  status: string;
  review_state: string;
  transcript_text: string;
  transcript_truncated: boolean;
  title_draft: string | null;
  caption_draft: string | null;
  title_style_preset: string | null;
  caption_style_preset: string | null;
}

export interface AccountVoice {
  id: number;
  name: string;
  platform: string;
  niche_label: string | null;
  niche: string | null;
  writing_tone: string | null;
  target_audience: string | null;
  hook_style: string | null;
  banned_phrases: string | null;
  title_style_notes: string | null;
  caption_style_notes: string | null;
  auto_schedule_on_export: boolean;
}

export interface ProcessingContext {
  item: ProcessingItem;
  account: AccountVoice | null;
  latest_revision: DraftRevision | null;
  revision_count: number;
}

export interface ApplyResult {
  item_id: number;
  revision_id: number;
  revision_number: number;
  applied_option: number;
  title_draft: string;
  caption_draft: string;
}

export type JobStatus = "pending" | "running" | "succeeded" | "failed" | "canceled";

export interface JobSnapshot {
  id: string;
  status: JobStatus;
  progress: number;
  message: string;
  result: unknown;
  error: string | null;
  // True once cancellation was requested for this job (cooperative cancel).
  cancel_requested?: boolean;
}

export interface ExportResult {
  item_id: number;
  processed_path: string;
  warning?: string;
  // Best-effort watermark cover (docs/SOURCING_POOLING_PLAN.md): a foreign
  // @handle found on the rendered reel is covered with the account's own handle.
  // Never fails the export; when nothing is covered these report why.
  watermark_replaced?: boolean;
  watermark_detected_text?: string | null;
  watermark_skipped_reason?: string | null;
  scheduled_publish?: QueueResult & {
    schedule_path?: string;
    message?: string;
  };
}

export interface ItemSummary {
  id: number;
  title: string | null;
  source_url: string;
  account_id: number | null;
  status: string;
  has_processed: boolean;
  has_draft: boolean;
}

export interface PublishJob {
  id: number;
  status: string;
  title: string | null;
  scheduled_at: string | null;
  posted_at: string | null;
  posted_url: string | null;
  error_message: string | null;
  processed_path: string | null;
}

export interface QueueResult {
  job_id: number;
  status: string;
  scheduled_at: string | null;
  created: boolean;
}

// Recent-post recency warning shown before a manual publish: the account posted
// within the 4h same-account window. `on_cooldown` false means safe to post.
export interface PublishRecency {
  on_cooldown: boolean;
  // True when a live post is already running for this account (vs. a recent
  // already-completed post). The UI warns immediately instead of opening the post.
  in_progress?: boolean;
  account_id?: number;
  account_name?: string | null;
  last_posted_at?: string | null;
  minutes_since?: number;
  recommended_next_at?: string | null;
}

// A completed background post (auto-publish loop) the UI hasn't shown yet.
export interface PublishEvent {
  id: number;
  at?: string | null;
  status: string;
  job_id: number;
  item_id?: number | null;
  account_id?: number | null;
  account_name?: string | null;
  posted_url?: string | null;
}

export interface DueRecencyWarning {
  account_id: number;
  account_name: string | null;
  last_posted_at: string;
  minutes_since: number;
  recommended_next_at: string;
}

export interface WorkflowOption {
  value: string;
  label: string;
}

export interface WorkflowSettings {
  clip_premise: string;
  caption_style: string;
  title_style: string;
  title_length: string;
  // "shared" = one caption for all three titles (cheaper); "per_option" = one
  // caption per title. Steers the generated prompt only; the importer accepts
  // either shape regardless.
  caption_mode: string;
  template: string;
  title_draft: string;
  caption_draft: string;
  caption_style_options: WorkflowOption[];
  title_style_options: WorkflowOption[];
  title_length_options: WorkflowOption[];
  caption_mode_options: WorkflowOption[];
  template_options: WorkflowOption[];
}

export type AccountOperationalStatus = "active" | "resting" | "flagged";

export interface AccountSummary {
  id: number;
  name: string;
  platform: string;
  niche_label: string | null;
  niche: string | null;
  instagram_handle: string | null;
  operational_status: AccountOperationalStatus;
}

export interface AccountDetail extends AccountSummary {
  login_identifier: string | null;
  instagram_profile: string | null;
  credential_blob: string | null;
  writing_tone: string | null;
  target_audience: string | null;
  hook_style: string | null;
  banned_phrases: string | null;
  title_style_notes: string | null;
  caption_style_notes: string | null;
  upload_timezone: string | null;
  upload_default_privacy: string | null;
  upload_schedule_slots: string | null;
  daily_posts_target: number | null;
  distribute_daily_target: number | null;
  auto_schedule_on_export: boolean;
  download_item_count: number;
  upload_job_count: number;
}

export interface DeleteAccountResult {
  deleted_account_id: number;
  unassigned_download_items: number;
  removed_upload_jobs: number;
  removed_assignments: number;
}

export interface SourceProfile {
  id: number;
  label: string;
  source_url: string;
  source_type: string;
  platform: string;
  enabled: boolean;
  priority: number;
  last_scraped_at: string | null;
  last_run_status: string | null;
  last_error_summary: string | null;
}

export interface ScrapeCandidate {
  id: number;
  title: string | null;
  source_url: string;
  channel_name: string | null;
  state: string;
  like_count: number | null;
  view_count: number | null;
  comment_count: number | null;
  duration_seconds: number | null;
  description: string | null;
  published_at: string | null;
  created_at: string | null;
  thumbnail_url: string | null;
}

// Normalized keep-region for a per-item manual export crop (fractions of the
// source width/height, in [0,1]).
export interface CropRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface ApifyUsage {
  month: string;
  used: number;
  free_cap: number;
  remaining: number;
  over_free_tier: boolean;
  warn: boolean;
}

export interface ScrapeToPoolResult {
  source_id: number;
  niche: string;
  scraped: number;
  added: number;
  duplicates: number;
  no_account: number;
  apify_usage: ApifyUsage;
}

export interface LibraryItem {
  id: number;
  // Per-account running number shown as "#N" in Processing (oldest = 1). Stable
  // per item and == the account's clip count for the newest item. Differs from
  // `id`, which is the global primary key. Null only if it could not be ranked.
  account_seq: number | null;
  title: string | null;
  source_url: string;
  status: string; // derived workflow status: new | draft | exported | posted | skipped
  raw_status: string;
  // True for a posted item that was manually reopened (a newer draft repost exists).
  // The status dropdown offers "Posted" only for these, to undo the reopen.
  reopened: boolean;
  review_state: string | null;
  file_path: string | null;
  has_file: boolean;
  has_processed: boolean;
  has_draft: boolean;
  account_id: number | null;
  account_name: string | null;
  created_at: string | null;
  is_new: boolean;
}

export interface PoolAssignmentCount {
  account_id: number;
  account_name: string;
  count: number;
}

export interface NichePool {
  niche: string;
  pooled: number;
  assigned: number;
  unused: number;
  rejected: number;
  pending: number;
  assignments_by_account: PoolAssignmentCount[];
}

export interface SetProcessingStatusResult {
  item_id: number;
  repost_job_id: number | null;
  status: "pending_review" | "draft" | "exported";
  created: boolean;
}

export interface PoolingOverview {
  niches: NichePool[];
}

export interface PoolClip {
  pool_item_id: number;
  clip_label: string;
  source_label: string;
  accepted_at: string | null;
  distributed_to: string[];
  is_distributed: boolean;
}

export interface PoolSource {
  source_label: string;
  clip_count: number;
  newest_post_at: string | null;
}

export interface NicheAccount {
  id: number;
  name: string;
  operational_status: AccountOperationalStatus;
}

export interface PoolSourceClip {
  pool_item_id: number;
  shortcode: string | null;
  source_url: string | null;
  caption: string | null;
  like_count: number | null;
  published_at: string | null;
  download_status: string;
  acceptance_status: string;
  preview_url: string | null;
  distributed_to: string[];
  // Engagement score (log-damped likes + recency) the pool list is ranked by.
  score: number;
}

export interface PoolReviewItem {
  pool_item_id: number;
  niche: string;
  clip_label: string;
  source_label: string;
  created_at: string | null;
  thumbnail_url: string | null;
  // Original reel URL (to open externally) and a playable in-app URL when the
  // footage has been downloaded; null until the clip is fetched for review.
  source_url: string | null;
  preview_url: string | null;
  fit_score: number;
  source_er: number;
  topic_tier: "S" | "A" | "B" | "C" | "D";
  suggested_action: "accept" | "review" | "reject";
  rights_confidence: RightsConfidence | null;
  view_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  duration_seconds: number | null;
  description: string | null;
  channel_name: string | null;
  published_at: string | null;
}

// Rights-risk label a reviewer sets on a pool item (docs/SOURCING_POOLING_PLAN.md
// §2.2). Drives the rights-risk badge in the Review tab.
export type RightsConfidence =
  | "archival"
  | "meme"
  | "tv_moment"
  | "broadcast_sport"
  | "news_broadcast"
  | "unknown";

export interface PoolItemPreview {
  pool_item_id: number;
  preview_url: string | null;
  thumbnail_url: string | null;
  source_url: string | null;
}

export interface DistributeNicheResult {
  niche: string;
  assigned: number;
  pinned: number;
  download_failures: number;
  max_per_account: number | null;
  accounts: {
    account_id: number;
    account_name: string;
    count: number;
    pinned: number;
    target: number;
  }[];
  /** Only present when assigned === 0. Explains why nothing was distributed. */
  reason?: "no_accounts" | "no_ready_accounts" | "all_at_cap" | "pool_empty" | "download_failed";
}

export interface DashboardPublishJob {
  id: number;
  account_name: string;
  video: string;
  title: string | null;
  status: string;
  is_due: boolean;
  scheduled_at: string | null;
  error_message: string | null;
  profile: string | null;
  output_name: string;
  processed_path: string;
}

/** Exported item that never made it into the publish queue (no UploadJob). */
export interface UnscheduledExport {
  item_id: number;
  account_id: number | null;
  account_name: string;
  title: string;
  output_name: string;
  exported_at: string | null;
  reason: string;
  can_schedule: boolean;
}

export interface DashboardPublishQueue {
  jobs: DashboardPublishJob[];
  due_count: number;
  draft: number;
  ready: number;
  scheduled: number;
  failed: number;
  unscheduled_exports: UnscheduledExport[];
}

export interface ScheduleCoverageSlot {
  slot: string;
  slot_at: string;
  state: string;
  job_id: number | null;
  // Processing/library item id (DownloadItem.id) for deep-linking to re-edit.
  item_id: number | null;
  job_title: string | null;
  scheduled_at: string | null;
  // Reason the Worker hasn't posted a 'cloud' job yet (e.g. same-account
  // cooldown), or null when there's nothing to report.
  note: string | null;
  // Raw Worker job status/error, synced on every poll (services/publishing.
  // sync_cloud_jobs) -- lets a 'cloud' slot show a short gate reason distinct
  // from `note` (same underlying value, kept for backward compatibility).
  cloud_status: string | null;
  cloud_error: string | null;
  timing: "on_time" | "late" | null;
}

export interface CloudAccountSettings {
  account_key: string;
  instagram_user_id: string;
  token_secret_name: string;
  enabled: boolean;
  daily_limit: number;
  min_gap_minutes: number;
}

export interface ScheduleCoverageAccount {
  account_id: number;
  account_name: string;
  timezone: string;
  daily_target: number;
  auto_schedule_on_export: boolean;
  filled: number;
  total: number;
  days: {
    date: string;
    is_today: boolean;
    filled: number;
    total: number;
    slots: ScheduleCoverageSlot[];
  }[];
}

export interface ScheduleCoverage {
  horizon_days: number;
  accounts: ScheduleCoverageAccount[];
}

export interface CloudPublisherHealth {
  publish_mode: "disabled" | "validate" | "live" | null;
  stored_bytes: number;
  max_stored_bytes: number;
  remaining_bytes: number;
  usage_percent: number;
  active_jobs: number;
  max_active_jobs: number;
  active_usage_percent: number;
  active_jobs_by_status: Record<string, number>;
  oldest_active_created_at: string | null;
  oldest_active_age_minutes: number | null;
  max_upload_bytes: number;
  stale_jobs: {
    awaiting_upload_over_minutes: number;
    awaiting_upload: number;
    processing_over_minutes: number;
    processing: number;
    processing_age_unknown: number;
    scheduled_past_due: number;
    oldest_scheduled_at: string | null;
  };
}

/** One Worker publish job (Cloudflare `publish_jobs` row), joined to the local
 * account name where the account is cloud-mapped and registered. */
export interface CloudWorkerJob {
  id: string;
  external_id: string;
  account_key: string;
  account_name: string | null;
  scheduled_at: string;
  status: string;
  attempts: number;
  meta_container_id: string | null;
  meta_media_id: string | null;
  error_message: string | null;
  published_at: string | null;
  // Local UploadJob.id parsed from `external_id` (`nf-<id>-...`), or null for
  // jobs that don't follow that convention.
  upload_job_id: number | null;
}

export interface CloudWorkerJobsResult {
  jobs: CloudWorkerJob[];
  publish_mode: "disabled" | "validate" | "live" | null;
}

export interface CloudWorkerAccount {
  account_key: string;
  instagram_user_id: string;
  token_secret_name: string;
  enabled: boolean;
  daily_limit: number;
  min_gap_minutes: number;
  created_at?: string;
  updated_at?: string;
}

export interface CloudWorkerAccountsResult {
  accounts: CloudWorkerAccount[];
}

export interface DashboardAccountStatsRow {
  account_id: number;
  account_name: string;
  today: number;
  daily_target: number;
  week: number;
  all_time: number;
  in_queue: number;
  scheduled: number;
  runway_days: number;
  runway_status: "green" | "amber" | "red";
  next_post_at: string | null;
}

export interface DashboardAccountStats {
  niche: string;
  accounts: DashboardAccountStatsRow[];
}

export interface ReadinessRow {
  account_id: number;
  account_name: string;
  profile: string | null;
  login_identifier: string | null;
  session_state: string;
  session_label: string;
  detail: string;
  due_now: number;
  scheduled: number;
  next_post_at: string | null;
  publishable: boolean;
}

export interface AccountReadiness {
  rows: ReadinessRow[];
  totals: {
    account_count: number;
    total_due_now: number;
    total_scheduled: number;
    blocked_accounts: number;
    next_post_at: string | null;
  };
}

export interface PublishQueueJob {
  id: number;
  account_id: number | null;
  account_name: string | null;
  download_item_id: number | null;
  title: string | null;
  status: string;
  scheduled_at: string | null;
  posted_at: string | null;
  posted_url: string | null;
  posted_views: number | null;
  posted_likes: number | null;
  posted_comments: number | null;
  posted_shares: number | null;
  content_type: string | null;
  processed_path: string | null;
}

// --- Clip Studio (campaign clip-and-repost workflow) --- //

export interface Campaign {
  slug: string;
  name: string;
  required_mention: string;
  hashtags: string[];
  min_clip_seconds: number;
  max_clips_per_day: number;
  country_tiers: number[];
  min_views_per_clip: number;
  min_views_for_payout: number;
  notes: string[];
}

export interface SourceMoment {
  start: number;
  end: number;
  duration: number;
  score: number;
  range_label: string;
  length_note: string;
  reasons: string[];
  context: string;
}

export interface ClipPlan {
  /** Null for a file taken off disk rather than fetched from a URL. */
  url: string | null;
  transcript_path: string | null;
  transcript_available: boolean;
  moments: SourceMoment[];
  /** The source had no captions, so its audio was transcribed here. */
  transcribed_locally?: boolean;
  /** Why transcription produced nothing (no speech, missing ffmpeg). */
  transcript_error?: string;
  /** The source's own title. Read from metadata, so it survives a cache hit —
   *  unlike the download result's, which is null on every run after the first. */
  title?: string | null;
  /** BCP-47 language YouTube reports for the source, e.g. "ko". */
  source_language?: string | null;
  /** Source is not in English, so a viewer cannot follow it without captions. */
  captions_recommended?: boolean;
}

/** How much the picture changes, derived from the preview's encoded bitrate. */
export interface VisualActivity {
  kb_per_second: number;
  /** A locked-off shot: great audio, nothing to look at. */
  looks_static: boolean;
}

/** One candidate cut from the source, ready to be watched and judged. */
export interface ClipPreview {
  index: number;
  video_path: string;
  start: number;
  end: number;
  duration: number;
  score: number;
  range_label: string;
  reasons: string[];
  visual_activity: VisualActivity;
  /** The exact words in this window — grounding for the hook and caption. */
  context: string;
  /** Seconds already shaved off the front because the window opened mid-thought. */
  opening_trimmed: number;
  /** Still starts mid-conversation — check the in-point before sending it over. */
  opens_mid_thought: boolean;
  /** Stops before the speaker finishes — the clip plays as if cut off. */
  ends_mid_thought?: boolean;
  /** Silence held after the last word so the clip lands. Part of `end`. */
  tail_hold?: number;
  /** Whether the picture gets busier after the talking stops (a cutaway). */
  visual_payoff?: VisualPayoff;
  /** Seconds the preview plays PAST `end`, so payoff can be seen and measured. */
  lookahead_seconds?: number;
}

/** Title options written for one candidate from its own spoken words. */
export interface ClipTitleSuggestions {
  titles: string[];
  /** Per title: "green" (no checkable claim), "yellow" (claims the clip backs
   *  up), "red" (a claim it does not). Checked against the clip alone. */
  tiers: string[];
  notes: string[];
  /** Index (as a string key) -> the words the clip does not support. */
  flagged_terms: Record<string, string[]>;
  recommended_index: number | null;
  /** True when the guard moved the pick off a flagged option. */
  recommendation_shifted: boolean;
  reason: string | null;
  summary: string;
  provider_label: string;
  used_fallback: boolean;
}

/** A batch reply routed back to each candidate by its clip number. */
export interface ClipBatchTitleImport {
  /** Keyed by the candidate's own 0-based index, as a string. */
  results: Record<string, ClipTitleSuggestions>;
  /** 1-based clip numbers the reply never mentioned. */
  missing: number[];
  imported: number;
}

/** One suggested title with its grounding verdict. */
export interface ClipTitleOption {
  text: string;
  tier: string;
  flagged: string[];
}

/** Measured picture activity after the out-point — the "they finally show it" case. */
export interface VisualPayoff {
  detected: boolean;
  body_kb_per_second?: number;
  tail_kb_per_second?: number;
  /** How much further the out-point should run to include the reveal. */
  extra_seconds?: number;
}

export interface ClipSourceInfo {
  video_path: string;
  title: string | null;
  width: number;
  height: number;
  duration_seconds: number | null;
  from_cache: boolean;
}

export interface ClipPreviewBatch extends ClipPlan {
  source: ClipSourceInfo | null;
  previews: ClipPreview[];
  /** History key for this source — travels onto every clip cut from it. */
  source_ref?: string;
}

/** One long video Clip Studio has been through, as the history table shows it. */
export interface ClipSourceHistory {
  source_ref: string;
  kind: "url" | "file";
  title: string | null;
  workspace_path: string | null;
  duration_seconds: number | null;
  transcript_available: boolean;
  preview_count: number;
  clip_count: number;
  created_at: string | null;
  last_analyzed_at: string | null;
}

/** How far one clip cut from a source has travelled. */
export type ClipStage = "library" | "exported" | "scheduled" | "posted";

export interface ClipSourceClip {
  item_id: number;
  title: string | null;
  created_at: string | null;
  file_path: string | null;
  processed_path: string | null;
  review_state: string;
  account_id: number | null;
  account_name: string | null;
  stage: ClipStage;
  job_id: number | null;
  job_status: string | null;
  scheduled_at: string | null;
  posted_at: string | null;
  posted_url: string | null;
}

export interface ClipSection {
  video_path: string;
  title: string | null;
  width: number;
  height: number;
  duration_seconds: number | null;
  section_start: number;
  section_end: number;
  // Where the chosen clip starts inside the downloaded section.
  clip_offset: number;
}

export interface CaptionCheck {
  caption: string;
  problems: string[];
}

/** A clip taken into the library — the row every downstream tool hangs off. */
export interface RegisteredClip {
  item_id: number;
  title: string | null;
  file_path: string;
  account_id: number;
  source_url: string;
  /** True when the clip carries its spoken words for the draft prompt. */
  has_transcript_context: boolean;
}

/** Result of putting a different reel into an already-scheduled slot. */
export interface SwapScheduledJobResult {
  slot_at: string | null;
  /** The reel that was sitting in the slot, now back in the unscheduled pool. */
  unscheduled: PublishQueueJob;
  /** The reel that now owns the slot. */
  scheduled: PublishQueueJob;
}

export interface SubmissionStatus {
  deadline: string;
  seconds_remaining: number;
  expired: boolean;
  view_ceiling: number;
}
