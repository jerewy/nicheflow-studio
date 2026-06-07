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
  processed_path: string | null;
}

export interface QueueResult {
  job_id: number;
  status: string;
  scheduled_at: string | null;
  created: boolean;
}

export interface WorkflowOption {
  value: string;
  label: string;
}

export interface WorkflowSettings {
  clip_premise: string;
  caption_style: string;
  title_style: string;
  template: string;
  title_draft: string;
  caption_draft: string;
  caption_style_options: WorkflowOption[];
  title_style_options: WorkflowOption[];
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
  download_item_count: number;
  upload_job_count: number;
}

export interface DeleteAccountResult {
  deleted_account_id: number;
  unassigned_download_items: number;
  removed_upload_jobs: number;
}

export interface LibraryItem {
  id: number;
  title: string | null;
  source_url: string;
  status: string;
  file_path: string | null;
  has_file: boolean;
  has_processed: boolean;
  has_draft: boolean;
  account_id: number | null;
  account_name: string | null;
  created_at: string | null;
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
  assignments_by_account: PoolAssignmentCount[];
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
