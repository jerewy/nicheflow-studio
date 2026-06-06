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
