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
  ApplyResult,
  DraftRevision,
  JobSnapshot,
  ProcessingContext,
} from "@/types";

type Envelope<T> = { ok: true; data: T } | { ok: false; error: string };

interface PywebviewApi {
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
  start_generation(
    itemId: number,
    payload: Record<string, unknown>,
  ): Promise<Envelope<{ job_id: string }>>;
  start_export(itemId: number): Promise<Envelope<{ job_id: string }>>;
  get_job(jobId: string): Promise<Envelope<JobSnapshot>>;
}

declare global {
  interface Window {
    pywebview?: { api: PywebviewApi };
  }
}

function hasBridge(): boolean {
  return typeof window !== "undefined" && window.pywebview?.api !== undefined;
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

  getContext(itemId?: number | null): Promise<ProcessingContext> {
    if (!hasBridge()) return mock.getContext();
    return unwrap(window.pywebview!.api.get_context(itemId ?? null));
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
};
