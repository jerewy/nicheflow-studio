import { Fragment, useCallback, useEffect, useRef, useState } from "react";

import {
  CandidateCard,
  type RenderState,
  type Trim,
} from "@/components/clip-studio/CandidateCard";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { bridge } from "@/lib/bridge";
import type {
  AccountSummary,
  ClipPreview,
  ClipPreviewBatch,
  ClipSourceClip,
  ClipSourceHistory,
  ClipStage,
  ClipTitleOption,
  ClipTitleSuggestions,
  RegisteredClip,
} from "@/types";

// The preview batch downloads the source once (about 90s on a 90-minute video,
// instant when cached) and then cuts every candidate locally, so it needs the
// long leash.
const PREVIEW_TIMEOUT_MS = 900000;

// One templated render: a crop pass plus the title/header burn-in over a clip of
// twenty-odd seconds. Far shorter than a preview batch, but not instant.
const RENDER_TIMEOUT_MS = 300000;

// One provider round trip. Measured 2-13s against Groq on real clip context.
const TITLE_TIMEOUT_MS = 120000;

/** Identity of a rendered clip. The account belongs in here as much as the trim
 *  and the title: the post header, avatar, name and verified seal are all read
 *  from it, so switching accounts changes the picture without touching either of
 *  the other two. Leaving it out left the grid showing eight clips branded for
 *  the previous account under a badge reading "as it will publish". */
function renderKey(trim: Trim, hook: string, accountId: number | null): string {
  return `${trim.start.toFixed(2)}|${trim.end.toFixed(2)}|${hook.trim()}|${accountId ?? "none"}`;
}

// Which account clips were last sent to. Remembered rather than defaulted from
// the header's active-niche account on purpose: campaign clipping runs on
// dedicated clip accounts, so inheriting the active niche is how a campaign clip
// ends up on a history account (see App.tsx — Clip Studio is the one screen not
// gated on the active account).
const SETTING_CLIP_ACCOUNT = "clipStudio.accountId";

async function waitForJob(
  jobId: string,
  timeoutMs: number,
  onProgress?: (value: number, message: string) => void,
): Promise<unknown> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const snapshot = await bridge.getJob(jobId);
    onProgress?.(snapshot.progress, snapshot.message);
    if (snapshot.status === "succeeded") return snapshot.result;
    if (snapshot.status === "failed") throw new Error(snapshot.error ?? "The job failed.");
    if (Date.now() > deadline) throw new Error("The job timed out.");
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

function mmss(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function shortDate(iso: string | null): string {
  if (!iso) return "—";
  const value = new Date(iso);
  return Number.isNaN(value.getTime())
    ? "—"
    : value.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}

function runtime(seconds: number | null): string {
  if (!seconds || seconds <= 0) return "—";
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

/** A source's display name: its resolved title, else the tail of the URL/path. */
function sourceLabel(source: ClipSourceHistory): string {
  if (source.title) return source.title;
  const trimmed = source.source_ref.replace(/\/+$/, "");
  return trimmed.split(/[/\\]/).pop() || source.source_ref;
}

const STAGE_STYLES: Record<ClipStage, string> = {
  library: "bg-white/10 text-white/70",
  exported: "bg-sky-500/20 text-sky-200",
  scheduled: "bg-amber-500/20 text-amber-200",
  posted: "bg-emerald-500/20 text-emerald-200",
};

const STAGE_LABELS: Record<ClipStage, string> = {
  library: "In library",
  exported: "Exported",
  scheduled: "Scheduled",
  posted: "Posted",
};

/** The clips one source produced, and how far each of them got. */
function SourceClipList({ clips }: { clips: ClipSourceClip[] | undefined }) {
  if (clips === undefined) {
    return <div className="px-3 py-2 text-xs opacity-60">Loading clips…</div>;
  }
  if (!clips.length) {
    return (
      <div className="px-3 py-2 text-xs opacity-60">
        Nothing was cut from this source — the previews were watched and passed on.
      </div>
    );
  }
  return (
    <table className="w-full text-xs">
      <thead className="text-[10px] uppercase tracking-wide opacity-50">
        <tr>
          <th className="px-3 py-1 text-left font-medium">Clip</th>
          <th className="px-3 py-1 text-left font-medium">Account</th>
          <th className="px-3 py-1 text-left font-medium">Stage</th>
          <th className="px-3 py-1 text-left font-medium">When</th>
        </tr>
      </thead>
      <tbody>
        {clips.map((clip) => (
          <tr key={clip.item_id} className="border-t border-white/5">
            <td className="max-w-[22rem] truncate px-3 py-1.5" title={clip.title ?? undefined}>
              #{clip.item_id} {clip.title ?? <span className="opacity-50">untitled</span>}
            </td>
            <td className="px-3 py-1.5 opacity-70">{clip.account_name ?? "—"}</td>
            <td className="px-3 py-1.5">
              <span className={`rounded px-1.5 py-0.5 text-[10px] ${STAGE_STYLES[clip.stage]}`}>
                {STAGE_LABELS[clip.stage]}
              </span>
            </td>
            <td className="whitespace-nowrap px-3 py-1.5 opacity-70">
              {clip.stage === "posted"
                ? shortDate(clip.posted_at)
                : clip.stage === "scheduled"
                  ? shortDate(clip.scheduled_at)
                  : shortDate(clip.created_at)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function ClipStudioScreen({ active = true }: { active?: boolean }) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Short confirmations for actions with no visible result of their own —
  // copying a prompt to the clipboard mainly.
  const [notice, setNotice] = useState<string | null>(null);

  const [batch, setBatch] = useState<ClipPreviewBatch | null>(null);
  // Preview files live under data/, so each needs a servable URL for <video>.
  const [previewUrls, setPreviewUrls] = useState<Record<number, string>>({});
  const [selected, setSelected] = useState<ClipPreview | null>(null);
  // Hand-typed in/out, and the only trim path for a source with no speech to
  // rank: no transcript means no candidates, so there is no card to carry one.
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  // Written outside this app and pasted in. Deliberately not generated here:
  // a campaign caption carries required mentions and disclosure rules that
  // change per campaign, and guessing at them is worse than leaving it blank.
  const [caption, setCaption] = useState("");

  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [storedAccountId, setAccountId] = useState<number | null>(null);
  // A remembered account that has since been deleted must not stay selected: the
  // select renders blank while still submitting the dead id. Derived rather than
  // corrected in an effect, so no render ever sees the stale value.
  const accountId =
    storedAccountId !== null &&
    accounts.length > 0 &&
    !accounts.some((account) => account.id === storedAccountId)
      ? null
      : storedAccountId;
  // Gates the save-back so the initial null never overwrites what was restored.
  const [accountRestored, setAccountRestored] = useState(false);
  const [handedOff, setHandedOff] = useState<RegisteredClip | null>(null);
  // Which candidates already went to the library, and as what. A batch offers
  // eight off one source and only the last confirmation used to be visible, so
  // there was nothing on the grid to say clip 3 had already been sent.
  const [sentItems, setSentItems] = useState<Record<number, number>>({});
  const [manualPath, setManualPath] = useState("");
  // Defaults from the batch: a source the viewer cannot follow by ear needs the
  // words on screen, and noticing that should not be the operator's job.
  const [burnCaptions, setBurnCaptions] = useState(false);

  // Per-candidate trim, and the finished renders that stream in behind the raw
  // grid. Keyed by preview index throughout.
  const [trims, setTrims] = useState<Record<number, Trim>>({});
  // Each candidate's own on-screen title. A batch shares a source, not a hook —
  // eight clips promise eight different things.
  const [titles, setTitles] = useState<Record<number, string>>({});
  const [titleOptions, setTitleOptions] = useState<Record<number, ClipTitleOption[]>>({});
  const [titlesPending, setTitlesPending] = useState<Record<number, boolean>>({});
  // Which cards' options came from the local fallback rather than a real model.
  const [titleFallback, setTitleFallback] = useState<Record<number, boolean>>({});
  // Transient per-card confirmation for actions whose result is not otherwise
  // visible on the card itself (a copy leaves no trace; a paste can return
  // titles that look like the ones already there).
  const [cardFlash, setCardFlash] = useState<Record<number, "copied" | "imported" | null>>(
    {},
  );
  const [renderedUrls, setRenderedUrls] = useState<Record<number, string>>({});
  const [renderStates, setRenderStates] = useState<Record<number, RenderState>>({});
  // What each render was made from — trim plus title. A later nudge, or typing
  // the hook, marks it stale rather than leaving a finished clip on screen that
  // is no longer the cut that would publish.
  const [renderedKeys, setRenderedKeys] = useState<Record<number, string>>({});
  const [showRendered, setShowRendered] = useState<Record<number, boolean>>({});
  // Bumped on every new batch; an in-flight queue checks it and stops rather
  // than filling the previous batch's cards with renders nobody asked for.
  const renderRunRef = useRef(0);
  // The render queue outlives the render that started it, so it reads these
  // through refs — a captured value would be minutes stale by the last clip.
  const batchSourceRef = useRef<string | null>(null);
  const transcriptRef = useRef<string | null>(null);
  const burnCaptionsRef = useRef(false);
  const accountIdRef = useRef<number | null>(null);
  // Read per iteration, not snapshotted: the queue is serial at ~21s a clip, so
  // titles typed while it runs still reach the clips it has not started yet.
  const titlesRef = useRef<Record<number, string>>({});

  // History: every source this studio has been through, and what came out.
  const [sources, setSources] = useState<ClipSourceHistory[]>([]);
  const [openSourceRef, setOpenSourceRef] = useState<string | null>(null);
  const [clipsBySource, setClipsBySource] = useState<Record<string, ClipSourceClip[]>>({});

  // Mirrored into refs after commit, not during render: the render queue runs
  // for minutes and must read what is current when each clip starts, not what
  // was captured when it was kicked off.
  useEffect(() => {
    batchSourceRef.current = batch?.source?.video_path ?? null;
    transcriptRef.current = batch?.transcript_path ?? null;
    burnCaptionsRef.current = burnCaptions;
    accountIdRef.current = accountId;
    titlesRef.current = titles;
  }, [batch, burnCaptions, accountId, titles]);

  const refreshHistory = useCallback(() => {
    bridge.listClipSources().then(setSources).catch(() => setSources([]));
  }, []);

  useEffect(() => {
    if (!active) return;
    bridge.listAccounts().then(setAccounts).catch(() => setAccounts([]));
    refreshHistory();
  }, [active, refreshHistory]);

  // Restore the last clip account once, before anything is allowed to save.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const stored = await bridge.getUiSettings([SETTING_CLIP_ACCOUNT]);
        const remembered = stored[SETTING_CLIP_ACCOUNT];
        if (!cancelled && typeof remembered === "number") setAccountId(remembered);
      } catch {
        // A preference that will not load must not block the screen.
      } finally {
        if (!cancelled) setAccountRestored(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!accountRestored || accountId === null) return;
    void bridge.setUiSetting(SETTING_CLIP_ACCOUNT, accountId);
  }, [accountId, accountRestored]);


  const onToggleSource = (sourceRef: string) => {
    if (openSourceRef === sourceRef) {
      setOpenSourceRef(null);
      return;
    }
    setOpenSourceRef(sourceRef);
    // Always refetch: a clip listed as "exported" an hour ago may be scheduled now.
    bridge
      .listClipSourceClips(sourceRef)
      .then((clips) => setClipsBySource((current) => ({ ...current, [sourceRef]: clips })))
      .catch(() => setClipsBySource((current) => ({ ...current, [sourceRef]: [] })));
  };

  const run = useCallback(
    async (label: string, task: () => Promise<void>) => {
      setBusy(label);
      setError(null);
      setNotice(null);
      try {
        await task();
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  /** Everything a render needs that is not the trim. Passed rather than read
   *  from state: a queue started in `loadBatch` runs before React has committed
   *  the new batch, so reading it back would render the *previous* source. */
  const renderCandidate = useCallback(
    async (
      preview: ClipPreview,
      trim: Trim,
      accId: number,
      runToken: number,
      context: { videoPath: string; transcriptPath: string | null; hook: string; burn: boolean },
    ) => {
      setRenderStates((current) => ({ ...current, [preview.index]: "rendering" }));
      try {
        const { job_id } = await bridge.startClipRender({
          video_path: context.videoPath,
          start: trim.start,
          end: trim.end,
          title: context.hook,
          account_id: accId,
          transcript_path: context.transcriptPath,
          burn_captions: context.burn,
        });
        const output = (await waitForJob(job_id, RENDER_TIMEOUT_MS)) as string;
        if (renderRunRef.current !== runToken) return;
        const media = await bridge.clipMediaUrl(String(output));
        setRenderedUrls((current) => ({ ...current, [preview.index]: media.url }));
        setRenderedKeys((current) => ({
          ...current,
          [preview.index]: renderKey(trim, context.hook, accId),
        }));
        setRenderStates((current) => ({ ...current, [preview.index]: "done" }));
        // Finished output is the point of rendering it, so show it once it lands.
        setShowRendered((current) => ({ ...current, [preview.index]: true }));
      } catch {
        if (renderRunRef.current !== runToken) return;
        setRenderStates((current) => ({ ...current, [preview.index]: "failed" }));
      }
    },
    [],
  );

  /** Walk the batch one at a time. Serial on purpose: eight parallel ffmpeg
   *  passes would fight for the same cores and finish no sooner, while making
   *  the first result — the one being waited on — arrive eight times later. */
  const renderQueue = useCallback(
    async (
      previews: ClipPreview[],
      initial: Record<number, Trim>,
      accId: number,
      context: { videoPath: string; transcriptPath: string | null; hook: string; burn: boolean },
    ) => {
      const runToken = ++renderRunRef.current;
      setRenderStates(Object.fromEntries(previews.map((p) => [p.index, "queued" as RenderState])));
      for (const preview of previews) {
        if (renderRunRef.current !== runToken) return;
        await renderCandidate(preview, initial[preview.index], accId, runToken, {
          ...context,
          // Whatever title this card has by the time its turn arrives.
          hook: titlesRef.current[preview.index] ?? "",
        });
      }
    },
    [renderCandidate],
  );

  // Both intake paths produce the same batch; only the job that fills it differs.
  const loadBatch = async (startJob: () => Promise<{ job_id: string }>) => {
    // Any queue still filling the old grid must stop before the new one loads.
    renderRunRef.current += 1;
    setBatch(null);
    setPreviewUrls({});
    setSelected(null);
    setHandedOff(null);
    setTrims({});
    setTitles({});
    setTitleOptions({});
    setTitlesPending({});
    setTitleFallback({});
    setRenderedUrls({});
    setRenderStates({});
    setRenderedKeys({});
    setShowRendered({});
    setSentItems({});
    const { job_id } = await startJob();
    const result = (await waitForJob(job_id, PREVIEW_TIMEOUT_MS, (_v, message) =>
      setBusy(message || "Cutting previews…"),
    )) as ClipPreviewBatch;
    setBatch(result);
    setBurnCaptions(Boolean(result.captions_recommended));
    const resolved = await Promise.all(
      result.previews.map(async (preview) => {
        const media = await bridge.clipMediaUrl(preview.video_path);
        return [preview.index, media.url] as const;
      }),
    );
    setPreviewUrls(Object.fromEntries(resolved));
    const initialTrims = Object.fromEntries(
      result.previews.map((preview) => [preview.index, { start: preview.start, end: preview.end }]),
    );
    setTrims(initialTrims);
    // The source was just written to history, so the table below is now stale.
    refreshHistory();
    // The raw grid is already usable; the finished versions fill in behind it.
    // Needs an account: the post-header templates take the avatar, name and seal
    // from it, so rendering without one produces a clip in nobody's styling.
    if (accountIdRef.current !== null && result.previews.length && result.source) {
      void renderQueue(result.previews, initialTrims, accountIdRef.current, {
        videoPath: result.source.video_path,
        transcriptPath: result.transcript_path,
        // Always empty here by design: the queue reads each card's own title
        // when that card's turn arrives, so this value never reaches a render.
        hook: "",
        burn: Boolean(result.captions_recommended),
      });
    }
  };

  const onFindPreviews = () =>
    run("Reading transcript…", () => loadBatch(() => bridge.startClipPreviews(url.trim(), 8)));

  const onFindLocalPreviews = () =>
    run("Transcribing the file — this takes a few minutes…", async () => {
      if (!manualPath.trim()) throw new Error("Choose a video file first.");
      await loadBatch(() => bridge.startLocalClipPreviews(manualPath.trim(), 8));
    });

  // Reopening is cheap by design: the source and its transcript are cached in
  // the workspace, so this re-cuts the previews without downloading anything.
  const onReopenSource = (source: ClipSourceHistory) => {
    if (source.kind === "url") {
      setUrl(source.source_ref);
      return run("Reopening the source…", () =>
        loadBatch(() => bridge.startClipPreviews(source.source_ref, 8)),
      );
    }
    const path = source.source_ref.replace(/^file:\/\//, "");
    setManualPath(path);
    return run("Reopening the file…", () =>
      loadBatch(() => bridge.startLocalClipPreviews(path, 8)),
    );
  };

  const onForgetSource = (source: ClipSourceHistory) =>
    run("Removing from history…", async () => {
      await bridge.forgetClipSource(source.source_ref, true);
      setOpenSourceRef(null);
      refreshHistory();
    });

  /** Card trim is the source of truth; step 3 mirrors whichever card is picked. */
  /** Push the out-point past the point where the picture gets busier. The
   *  ranker stops on the last spoken word, which cuts away the thing being
   *  talked about right as it finally appears. */
  const onExtendEnd = (preview: ClipPreview) => {
    const extra = preview.visual_payoff?.extra_seconds ?? 0;
    if (extra <= 0) return;
    const trim = trims[preview.index] ?? { start: preview.start, end: preview.end };
    setTrims((current) => ({
      ...current,
      [preview.index]: { ...trim, end: trim.end + extra },
    }));
  };

  const onTrimChange = (preview: ClipPreview, next: Trim) => {
    setTrims((current) => ({ ...current, [preview.index]: next }));
    if (selected?.index === preview.index) {
      setStart(next.start.toFixed(1));
      setEnd(next.end.toFixed(1));
    }
  };

  /** Write title options for one candidate from the words spoken inside it. */
  const onSuggestTitles = (preview: ClipPreview) => {
    if (accountId === null) {
      setError("Pick the account this clip is for first — the title takes its register from it.");
      return;
    }
    setTitlesPending((current) => ({ ...current, [preview.index]: true }));
    setError(null);
    void (async () => {
      try {
        const { job_id } = await bridge.startClipTitleSuggestions({
          account_id: accountId,
          transcript_text: preview.context,
          video_path: preview.video_path,
          // The source's own title names the subject a 15s window rarely does.
          source_title: batch?.title ?? batch?.source?.title ?? null,
          // Lets the backend read what was said either side of this window.
          transcript_path: batch?.transcript_path ?? null,
          start: trims[preview.index]?.start ?? preview.start,
          end: trims[preview.index]?.end ?? preview.end,
        });
        const result = (await waitForJob(job_id, TITLE_TIMEOUT_MS)) as ClipTitleSuggestions;
        applyTitleResult(preview, result);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setTitlesPending((current) => ({ ...current, [preview.index]: false }));
      }
    })();
  };

  /** Shared by both title routes: order the guard's pick first and store. */
  const applyTitleResult = (preview: ClipPreview, result: ClipTitleSuggestions) => {
    const options: ClipTitleOption[] = result.titles.map((text, index) => ({
      text,
      tier: result.tiers?.[index] ?? "yellow",
      flagged: result.flagged_terms?.[String(index)] ?? [],
    }));
    const pick = result.recommended_index ?? 0;
    if (pick > 0 && pick < options.length) options.unshift(...options.splice(pick, 1));
    setTitleOptions((current) => ({ ...current, [preview.index]: options }));
    setTitleFallback((current) => ({
      ...current,
      [preview.index]: Boolean(result.used_fallback),
    }));
  };

  const clipTitlePayload = (preview: ClipPreview) => ({
    account_id: accountId,
    transcript_text: preview.context,
    video_path: preview.video_path,
    source_title: batch?.title ?? batch?.source?.title ?? null,
    transcript_path: batch?.transcript_path ?? null,
    range_label: preview.range_label,
    start: trims[preview.index]?.start ?? preview.start,
    end: trims[preview.index]?.end ?? preview.end,
  });

  /** Per-card confirmation that fades, so a copy/paste is not a silent no-op. */
  const flagCard = (index: number, state: "copied" | "imported") => {
    setCardFlash((current) => ({ ...current, [index]: state }));
    window.setTimeout(
      () => setCardFlash((current) => ({ ...current, [index]: null })),
      4000,
    );
  };

  const onCopyTitlePrompt = (preview: ClipPreview) =>
    run("Building the prompt…", async () => {
      if (accountId === null) throw new Error("Pick the account this clip is for first.");
      const { copied } = await bridge.buildClipTitlePrompt(clipTitlePayload(preview));
      if (!copied) throw new Error("Prompt built, but the clipboard was unavailable.");
      flagCard(preview.index, "copied");
      setNotice("Prompt copied. Paste it into Claude or ChatGPT, then click Paste reply.");
    });

  const onPasteTitles = (preview: ClipPreview) =>
    run("Reading the reply…", async () => {
      const result = await bridge.importClipTitles(clipTitlePayload(preview));
      applyTitleResult(preview, result);
      flagCard(preview.index, "imported");
      setNotice(
        result.recommendation_shifted
          ? `Imported ${result.titles.length} titles. The pasted pick was flagged, so the recommendation moved.`
          : `Imported ${result.titles.length} titles.`,
      );
    });

  const batchPayload = () => ({
    account_id: accountId,
    source_title: batch?.title ?? batch?.source?.title ?? null,
    transcript_path: batch?.transcript_path ?? null,
    clips: (batch?.previews ?? []).map((preview) => ({
      transcript_text: preview.context,
      range_label: preview.range_label,
      start: trims[preview.index]?.start ?? preview.start,
      end: trims[preview.index]?.end ?? preview.end,
    })),
  });

  const onCopyBatchPrompt = () =>
    run("Building one prompt for every clip…", async () => {
      if (accountId === null) throw new Error("Pick the account these clips are for first.");
      if (!batch?.previews.length) throw new Error("Find previews first.");
      const { copied, clip_count } = await bridge.buildClipBatchTitlePrompt(batchPayload());
      if (!copied) throw new Error("Prompt built, but the clipboard was unavailable.");
      setNotice(
        `Prompt for all ${clip_count} clips copied. Paste it into Claude or ChatGPT, ` +
          "then click Paste batch reply.",
      );
    });

  const onPasteBatchTitles = () =>
    run("Routing the reply back to each clip…", async () => {
      if (!batch?.previews.length) throw new Error("Find previews first.");
      const result = await bridge.importClipBatchTitles(batchPayload());
      // Keyed by the candidate's own index, so a reply that reorders or omits
      // clips still lands each block on the right card.
      for (const [key, suggestion] of Object.entries(result.results)) {
        const preview = batch.previews[Number(key)];
        if (!preview) continue;
        applyTitleResult(preview, suggestion);
        flagCard(preview.index, "imported");
      }
      setNotice(
        result.missing.length
          ? `Imported titles for ${result.imported} clips. No block found for clip ${result.missing.join(", ")} — ask for those again, or do them one at a time.`
          : `Imported titles for all ${result.imported} clips.`,
      );
    });

  /** The card's title is what the render burns in, and what is sent onward. */
  const onTitleChange = (preview: ClipPreview, next: string) => {
    setTitles((current) => ({ ...current, [preview.index]: next }));
  };

  const onRenderCandidate = (preview: ClipPreview) => {
    if (accountId === null) {
      setError("Pick the account this clip is for first.");
      return;
    }
    if (!batch?.source) return;
    const trim = trims[preview.index] ?? { start: preview.start, end: preview.end };
    // Reuses the current run token, so a one-off re-render neither cancels the
    // batch queue nor survives a new batch replacing the grid under it.
    void renderCandidate(preview, trim, accountId, renderRunRef.current, {
      videoPath: batch.source.video_path,
      transcriptPath: batch.transcript_path,
      hook: titles[preview.index] ?? "",
      burn: burnCaptions,
    });
  };

  const onSelectPreview = (preview: ClipPreview) => {
    setSelected(preview);
    setHandedOff(null);
  };

  // What actually gets cut: the picked card's own in/out points, or the
  // hand-typed ones when there was nothing to rank and so no card to pick.
  const sendTrim = selected
    ? (trims[selected.index] ?? { start: selected.start, end: selected.end })
    : { start: Number(start), end: Number(end) };
  const sendTitle = selected ? (titles[selected.index] ?? "") : "";
  const duration = sendTrim.end - sendTrim.start;

  const onSendToProcessing = () =>
    run("Cutting and adding to the library…", async () => {
      if (!batch?.source) throw new Error("Find previews first.");
      if (accountId === null) throw new Error("Pick the account this clip is for.");
      if (!(duration > 0)) throw new Error("Pick a candidate, or type the in/out points.");
      // The whole source is on disk, so these are plain source timestamps — no
      // section-relative offset to translate.
      const clip = await bridge.sendClipToProcessing({
        video_path: batch.source.video_path,
        start: sendTrim.start,
        end: sendTrim.end,
        account_id: accountId,
        title: sendTitle || null,
        source_url: url.trim(),
        transcript_context: selected?.context ?? null,
        caption_draft: caption || null,
        transcript_path: batch.transcript_path,
        burn_captions: burnCaptions,
        // Ties the clip back to the source it was cut from, which is what makes
        // the history drill-in possible.
        clip_source_ref: batch.source_ref ?? null,
      });
      setHandedOff(clip);
      if (selected) {
        setSentItems((current) => ({ ...current, [selected.index]: clip.item_id }));
      }
      refreshHistory();
    });

  const onPickManualFile = () =>
    run("Waiting for the file dialog…", async () => {
      const picked = await bridge.pickVideoFile();
      if (picked.path) setManualPath(picked.path);
    });

  const onImportManualFile = () =>
    run("Adding to the library…", async () => {
      if (!manualPath.trim()) throw new Error("Choose a video file first.");
      if (accountId === null) throw new Error("Pick the account this clip is for.");
      const clip = await bridge.importLocalClip({
        video_path: manualPath.trim(),
        account_id: accountId,
        title: sendTitle || null,
        source_url: url.trim() || null,
        caption_draft: caption || null,
      });
      setHandedOff(clip);
    });

  return (
    <div className="flex flex-col gap-4 p-4">
      {error ? (
        <div className="rounded-md border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-200">
          {error}
        </div>
      ) : null}
      {busy ? (
        <div className="rounded-md border border-sky-500/40 bg-sky-500/10 p-3 text-sm text-sky-100">
          {busy}
        </div>
      ) : null}
      {notice && !busy ? (
        <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-100">
          {notice}
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>1 · Source</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs uppercase tracking-wide opacity-50">Clip goes to</span>
            <select
              value={accountId ?? ""}
              onChange={(event) =>
                setAccountId(event.target.value ? Number(event.target.value) : null)
              }
              className="rounded-md border border-white/15 bg-transparent px-2 py-1 text-sm"
            >
              <option value="">Pick an account…</option>
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name}
                </option>
              ))}
            </select>
            <span className="text-xs opacity-50">
              Separate from the active niche up top — campaign clips go to their own clip
              accounts. Remembered between visits.
            </span>
          </div>

          <div className="flex gap-2">
            <Input
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="Source URL (YouTube)"
            />
            <Button onClick={onFindPreviews} disabled={!url.trim() || busy !== null}>
              Find &amp; cut previews
            </Button>
          </div>
          <p className="text-xs opacity-70">
            Downloads the source once (about 90s, instant if you have used it before), then cuts
            eight candidates to watch. Roughly two minutes of watching instead of the whole video.
          </p>

          <details className="rounded-md border border-white/10 p-2">
            <summary className="cursor-pointer text-sm opacity-80">
              Already have the file? Import it instead
            </summary>
            <div className="mt-3 flex flex-col gap-2">
              <p className="text-xs opacity-70">
                For clips you downloaded by hand (a campaign clip pack, say). MP4, MOV, MKV and
                WEBM all work; anything the review player cannot play is converted on the way in.
                “Add to library” takes the whole file as one clip. “Find &amp; cut previews” treats
                it like a source and ranks it — that one transcribes the audio locally, so give it
                a few minutes on a long file.
              </p>
              <div className="flex gap-2">
                <Input
                  value={manualPath}
                  onChange={(event) => setManualPath(event.target.value)}
                  placeholder="Path to the video file"
                />
                <Button onClick={onPickManualFile} disabled={busy !== null} variant="secondary">
                  Browse…
                </Button>
                <Button
                  onClick={onFindLocalPreviews}
                  disabled={!manualPath.trim() || busy !== null}
                  variant="secondary"
                >
                  Find &amp; cut previews
                </Button>
                <Button
                  onClick={onImportManualFile}
                  disabled={!manualPath.trim() || accountId === null || busy !== null}
                >
                  Add to library
                </Button>
              </div>
            </div>
          </details>
          {batch?.source ? (
            <p className="text-xs opacity-60">
              Source {batch.source.width}x{batch.source.height}
              {batch.source.from_cache ? " · reused the cached download" : " · downloaded now"}
            </p>
          ) : null}
          {batch?.transcribed_locally ? (
            <p className="text-xs opacity-60">
              No captions on this source, so the audio was transcribed here instead.
            </p>
          ) : null}
          {batch && !batch.transcript_available ? (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-100">
              {batch.transcript_error
                ? `Nothing could be transcribed from this source (${batch.transcript_error}).`
                : "No speech was found on this source, so nothing can be ranked."}{" "}
              Type the in/out points by hand in step 3.
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Sources you have mined</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          <p className="text-xs opacity-70">
            Every video this studio has been through, newest first. Reopening one is instant —
            the download and transcript are still cached — so check here before pasting a URL
            you may already have worked. Click a row to see the clips it produced.
          </p>
          {/* Kept visible when empty: a table that only appears once it has rows
              is indistinguishable from one that was never built. */}
          {!sources.length ? (
            <div className="rounded-md border border-dashed border-white/15 p-4 text-xs opacity-60">
              Nothing recorded yet. The next source you run “Find &amp; cut previews” on lands
              here — sources analysed before this list existed are not backfilled.
            </div>
          ) : (
            <div className="overflow-hidden rounded-md border border-white/10">
              <table className="w-full text-xs">
                <thead className="bg-white/[0.03] text-[10px] uppercase tracking-wide opacity-50">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">Source</th>
                    <th className="px-3 py-2 text-left font-medium">Length</th>
                    <th className="px-3 py-2 text-left font-medium">Previews</th>
                    <th className="px-3 py-2 text-left font-medium">Clips</th>
                    <th className="px-3 py-2 text-left font-medium">Last opened</th>
                    <th className="px-3 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {sources.map((source) => (
                    <Fragment key={source.source_ref}>
                      <tr
                        className={
                          openSourceRef === source.source_ref
                            ? "cursor-pointer border-t border-white/10 bg-sky-500/10"
                            : "cursor-pointer border-t border-white/10 hover:bg-white/[0.04]"
                        }
                        onClick={() => onToggleSource(source.source_ref)}
                      >
                        <td className="max-w-[26rem] px-3 py-2">
                          <div className="truncate font-medium" title={source.source_ref}>
                            {sourceLabel(source)}
                          </div>
                          <div className="truncate font-mono text-[10px] opacity-45">
                            {source.kind === "file" ? "local file" : source.source_ref}
                          </div>
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 opacity-70">
                          {runtime(source.duration_seconds)}
                        </td>
                        <td className="px-3 py-2 opacity-70">{source.preview_count}</td>
                        <td className="px-3 py-2">
                          {source.clip_count ? (
                            <Badge>{source.clip_count}</Badge>
                          ) : (
                            <span className="opacity-45">none</span>
                          )}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 opacity-70">
                          {shortDate(source.last_analyzed_at)}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-right">
                          <Button
                            size="sm"
                            variant="secondary"
                            disabled={busy !== null}
                            onClick={(event) => {
                              event.stopPropagation();
                              void onReopenSource(source);
                            }}
                          >
                            Reopen
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={busy !== null}
                            title="Remove from this list and delete the cached download"
                            onClick={(event) => {
                              event.stopPropagation();
                              void onForgetSource(source);
                            }}
                          >
                            Forget
                          </Button>
                        </td>
                      </tr>
                      {openSourceRef === source.source_ref ? (
                        <tr className="border-t border-white/5 bg-black/20">
                          <td colSpan={6} className="p-0">
                            <SourceClipList clips={clipsBySource[source.source_ref]} />
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Always rendered, even with no batch loaded, so the numbered workflow
          never reads 1 → 3 with a gap where the picking step should be. */}
      <Card>
        <CardHeader>
          <CardTitle>2 · Watch and pick</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p className="text-xs opacity-70">
            A shortlist, not a verdict. The score reads the transcript only, so it cannot tell a
            payoff from a talking head — that is what the “static” flag and your eyes are for.
            {batch?.previews.length
              ? ` Each player runs ${batch.previews[0].lookahead_seconds ?? 4}s past where the clip would actually stop, so you can see whether the shot after it is worth keeping.`
              : ""}
          </p>
          {/* Lives here rather than with the send step because it changes what
              the cards render: a burn decided afterwards would leave eight
              finished previews that no longer match what publishes. */}
          <label className="flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              checked={burnCaptions}
              onChange={(event) => setBurnCaptions(event.target.checked)}
              disabled={!batch?.transcript_available}
              className="mt-0.5"
            />
            <span className="opacity-80">
              Burn the transcript into the picture.
              {batch?.captions_recommended
                ? ` This source is in ${batch.source_language ?? "another language"}, so a viewer cannot follow it without them.`
                : " Off by default — most pooled clips already carry the creator's own text."}
            </span>
          </label>
          {!batch?.previews.length ? (
            <div className="rounded-md border border-dashed border-white/15 p-4 text-xs opacity-60">
              Nothing ranked yet. Run “Find &amp; cut previews” above and the candidates land
              here, each with its own in/out points ready to nudge.
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {batch.previews.map((preview) => {
                const trim = trims[preview.index] ?? {
                  start: preview.start,
                  end: preview.end,
                };
                return (
                  <CandidateCard
                    key={preview.index}
                    preview={preview}
                    rawUrl={previewUrls[preview.index]}
                    renderedUrl={renderedUrls[preview.index]}
                    renderState={renderStates[preview.index] ?? "idle"}
                    renderStale={
                      renderedKeys[preview.index] !== undefined &&
                      renderedKeys[preview.index] !==
                        renderKey(trim, titles[preview.index] ?? "", accountId)
                    }
                    showRendered={showRendered[preview.index] ?? false}
                    onToggleView={() =>
                      setShowRendered((current) => ({
                        ...current,
                        [preview.index]: !current[preview.index],
                      }))
                    }
                    onRender={() => onRenderCandidate(preview)}
                    selected={selected?.index === preview.index}
                    onSelect={() => onSelectPreview(preview)}
                    trim={trim}
                    onTrimChange={(next) => onTrimChange(preview, next)}
                    title={titles[preview.index] ?? ""}
                    onTitleChange={(next) => onTitleChange(preview, next)}
                    titleOptions={titleOptions[preview.index] ?? []}
                    titlesFromFallback={titleFallback[preview.index] ?? false}
                    titlesPending={titlesPending[preview.index] ?? false}
                    onSuggestTitles={() => onSuggestTitles(preview)}
                    onCopyTitlePrompt={() => void onCopyTitlePrompt(preview)}
                    onPasteTitles={() => void onPasteTitles(preview)}
                    flash={cardFlash[preview.index] ?? null}
                    onExtendEnd={() => onExtendEnd(preview)}
                    sentItemId={sentItems[preview.index] ?? null}
                  />
                );
              })}
            </div>
          )}
          {/* One prompt and one paste for the whole grid. Doing it per card
              means eight copies, eight model round trips and eight pastes for a
              normal batch, and the rules are identical for every clip in it. */}
          {batch?.previews.length ? (
            <div className="flex flex-wrap items-center gap-2 rounded-md border border-white/10 bg-white/[0.03] p-2">
              <span className="text-xs opacity-70">
                All {batch.previews.length} clips at once:
              </span>
              <Button
                size="sm"
                variant="secondary"
                onClick={onCopyBatchPrompt}
                disabled={busy !== null}
              >
                Copy prompt for all
              </Button>
              <Button
                size="sm"
                variant="secondary"
                onClick={onPasteBatchTitles}
                disabled={busy !== null}
              >
                Paste batch reply
              </Button>
              <span className="text-[11px] opacity-50">
                One prompt covering every candidate; the reply routes back by clip number.
              </span>
            </div>
          ) : null}
          {batch?.previews.length && accountId === null ? (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-xs text-amber-100">
              Pick the account up in step 1 and the finished versions will render — the title
              band, avatar and verified seal all come from it, so there is nothing to render
              into without one.
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>3 · Send to Processing</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p className="text-xs opacity-70">
            Cuts the picked candidate at its own in/out points and adds it to the library as a
            normal item. Title styling, crop and export happen in Processing, and the words spoken
            in the clip travel with it so the draft prompt is already grounded.
          </p>
          {/* The one trim path left for a source with nothing to rank: no
              transcript means no candidates, and no candidates means no card. */}
          {batch && !batch.previews.length ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs opacity-70">Nothing was ranked — cut it by hand:</span>
              <Input
                value={start}
                onChange={(event) => setStart(event.target.value)}
                placeholder="start (s)"
                className="w-32"
              />
              <Input
                value={end}
                onChange={(event) => setEnd(event.target.value)}
                placeholder="end (s)"
                className="w-32"
              />
            </div>
          ) : null}
          {selected ? (
            <p className="text-xs opacity-70">
              Sending {mmss(sendTrim.start)}–{mmss(sendTrim.end)} · {duration.toFixed(1)}s
              {sendTitle
                ? ` · “${sendTitle}”`
                : " · no title on this card yet, so the band would publish empty"}
            </p>
          ) : null}
          {/* Not generated on purpose. Campaign captions carry required mentions
              and disclosure rules that differ per campaign and change without
              notice, so this takes whatever you wrote instead of guessing. */}
          <Textarea
            value={caption}
            onChange={(event) => setCaption(event.target.value)}
            rows={4}
            placeholder="Caption (optional) — paste your own, mentions and campaign rules included"
          />
          <Button
            onClick={onSendToProcessing}
            disabled={!batch?.source || !(duration > 0) || accountId === null || busy !== null}
          >
            Send to Processing
          </Button>
          {handedOff ? (
            <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-100">
              <div className="font-semibold">
                Added to the library as item #{handedOff.item_id}
              </div>
              <div className="mt-1 text-xs opacity-80">
                Open the Processing tab to write the title, generate the caption, and export it.
                {handedOff.has_transcript_context
                  ? " The clip's spoken words came along for the draft prompt."
                  : " No transcript context on this one, so the draft prompt starts empty."}
              </div>
              <div className="mt-1 font-mono text-[11px] opacity-60">{handedOff.file_path}</div>
            </div>
          ) : null}
        </CardContent>
      </Card>

    </div>
  );
}
