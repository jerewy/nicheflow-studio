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
  frames: BatchFrame[];
}

export interface BatchDraftImportResult {
  imported: number[];
  failed: { item_id: number; error: string }[];
  unmatched: number[];
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

export type JobStatus = "pending" | "running" | "succeeded" | "failed";

export interface JobSnapshot {
  id: string;
  status: JobStatus;
  progress: number;
  message: string;
  result: unknown;
  error: string | null;
}

export interface ExportResult {
  item_id: number;
  processed_path: string;
  warning?: string;
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
  template: string;
  title_draft: string;
  caption_draft: string;
  caption_style_options: WorkflowOption[];
  title_style_options: WorkflowOption[];
  title_length_options: WorkflowOption[];
  template_options: WorkflowOption[];
}

export interface AccountSummary {
  id: number;
  name: string;
  platform: string;
  niche_label: string | null;
  niche: string | null;
  instagram_handle: string | null;
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
  view_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  duration_seconds: number | null;
  description: string | null;
  channel_name: string | null;
  published_at: string | null;
}

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
  timing: "on_time" | "late" | null;
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
