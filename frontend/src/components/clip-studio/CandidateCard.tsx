import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { ClipPreview, ClipTitleOption } from "@/types";

/** In/out points for one candidate, in absolute source seconds. */
export interface Trim {
  start: number;
  end: number;
}

/** Where a candidate is in the background render queue. */
export type RenderState = "idle" | "queued" | "rendering" | "done" | "failed";

interface CandidateCardProps {
  preview: ClipPreview;
  /** The raw cut — always available, cheap, and what shows the pacing. */
  rawUrl?: string;
  /** The same span through the account's real template, once it has rendered. */
  renderedUrl?: string;
  renderState: RenderState;
  /** True when the trim moved after the render, so what is on screen is stale. */
  renderStale: boolean;
  showRendered: boolean;
  onToggleView: () => void;
  onRender: () => void;
  selected: boolean;
  onSelect: () => void;
  trim: Trim;
  onTrimChange: (trim: Trim) => void;
  /** This candidate's own on-screen title. Each clip needs a different one, and
   *  the render is worthless for judging without it. */
  title: string;
  onTitleChange: (title: string) => void;
  /** Options written for this clip, each with its grounding verdict. */
  titleOptions: ClipTitleOption[];
  /** True when no provider answered and the options are local-fallback text. */
  titlesFromFallback: boolean;
  titlesPending: boolean;
  onSuggestTitles: () => void;
  /** Copy a prompt for Claude/ChatGPT, and read the reply back. */
  onCopyTitlePrompt: () => void;
  onPasteTitles: () => void;
  /** Transient confirmation so a copy or paste is never a silent no-op. */
  flash: "copied" | "imported" | null;
  /** Push the out-point past the reveal this candidate was measured to have. */
  onExtendEnd: () => void;
  /** Library item this candidate already became, if it has been sent. */
  sentItemId: number | null;
}

function mmss(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

const NUDGES = [-2, -0.5, 0.5, 2];

// Grounding verdict from draft_guard, checked against the clip's own words.
const TIER_DOT: Record<string, string> = {
  green: "bg-emerald-400",
  yellow: "bg-amber-400",
  red: "bg-red-500",
};

export function CandidateCard({
  preview,
  rawUrl,
  renderedUrl,
  renderState,
  renderStale,
  showRendered,
  onToggleView,
  onRender,
  selected,
  onSelect,
  trim,
  onTrimChange,
  title,
  onTitleChange,
  titleOptions,
  titlesFromFallback,
  titlesPending,
  onSuggestTitles,
  onCopyTitlePrompt,
  onPasteTitles,
  flash,
  onExtendEnd,
  sentItemId,
}: CandidateCardProps) {
  // The preview file starts at the moment's original in-point and runs past its
  // out-point by the look-ahead, so that span is exactly the footage on hand.
  // The handles stop there rather than offering a trim nothing can play back.
  const floor = preview.start;
  const ceiling = preview.start + preview.duration + (preview.lookahead_seconds ?? 0);
  const span = Math.max(ceiling - floor, 0.001);
  const duration = trim.end - trim.start;

  const clamp = (value: number) => Math.min(ceiling, Math.max(floor, value));
  const setStart = (value: number) =>
    onTrimChange({ ...trim, start: Math.min(clamp(value), trim.end - 0.5) });
  const setEnd = (value: number) =>
    onTrimChange({ ...trim, end: Math.max(clamp(value), trim.start + 0.5) });

  const showingFinished = Boolean(showRendered && renderedUrl);
  const activeUrl = showingFinished ? renderedUrl : rawUrl;
  const pct = (value: number) => ((value - floor) / span) * 100;

  return (
    <div
      className={
        selected
          ? "flex flex-col gap-2 rounded-md border border-sky-400 bg-sky-500/10 p-2"
          : sentItemId !== null
            ? "flex flex-col gap-2 rounded-md border border-emerald-500/40 p-2 hover:border-emerald-400"
            : "flex flex-col gap-2 rounded-md border border-white/10 p-2 hover:border-white/30"
      }
    >
      <div className="relative">
        <button type="button" onClick={onSelect} className="block w-full text-left">
          {activeUrl ? (
            <video
              src={activeUrl}
              controls
              preload="metadata"
              // The finished cut is 1080x1920, so it gets a portrait box rather
              // than the raw cut's 16:9 one — a vertical clip letterboxed inside
              // a landscape frame is exactly what this screen exists to avoid.
              className={
                showingFinished
                  ? "mx-auto aspect-[9/16] max-h-[30rem] rounded bg-black"
                  : "aspect-video w-full rounded bg-black"
              }
              onClick={(event) => event.stopPropagation()}
            />
          ) : (
            <div className="aspect-video w-full animate-pulse rounded bg-white/5" />
          )}
        </button>
        <span
          className={
            showingFinished
              ? "pointer-events-none absolute left-2 top-2 rounded bg-emerald-500/80 px-1.5 py-0.5 text-[10px] font-semibold text-black"
              : "pointer-events-none absolute left-2 top-2 rounded bg-black/70 px-1.5 py-0.5 text-[10px] font-semibold text-white/80"
          }
        >
          {showingFinished ? "FINAL · as it will publish" : "RAW cut"}
        </span>
      </div>

      {/* Per candidate, not one hook for the batch: each clip promises something
          different, and a render with no title is the thing this screen was
          added to stop you judging from. */}
      <input
        value={title}
        onChange={(event) => onTitleChange(event.target.value)}
        placeholder="On-screen title for this clip…"
        className="w-full rounded border border-white/15 bg-transparent px-2 py-1 text-xs"
      />
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="ghost"
          onClick={onSuggestTitles}
          disabled={titlesPending}
          className="h-6 px-2 text-[10px]"
        >
          {titlesPending ? "Writing titles…" : "Suggest titles"}
        </Button>
        {/* The escape hatch when the local model's titles are not usable: same
            prompt, written by whichever chat model you prefer, parsed back
            through the same headers and checked by the same guard. */}
        <Button
          size="sm"
          variant="ghost"
          onClick={onCopyTitlePrompt}
          className="h-6 px-2 text-[10px]"
          title="Copy a prompt for Claude Code / Claude chat / ChatGPT"
        >
          {flash === "copied" ? "✓ Copied" : "Copy prompt"}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={onPasteTitles}
          className="h-6 px-2 text-[10px]"
          title="Read the reply back off the clipboard"
        >
          {flash === "imported" ? "✓ Imported" : "Paste reply"}
        </Button>
        {flash ? (
          <span className="text-[10px] text-emerald-300">
            {flash === "copied"
              ? "prompt on the clipboard"
              : `${titleOptions.length} titles in`}
          </span>
        ) : null}
        {!title.trim() && !titleOptions.length ? (
          <span className="text-[10px] text-amber-300/80">
            No title yet — the finished render will have an empty title band.
          </span>
        ) : null}
      </div>
      {/* A fallback result is not a weak title, it is raw transcript text — say
          so rather than letting it pass as a suggestion worth judging. */}
      {titlesFromFallback ? (
        <div className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-[10px] text-amber-100">
          The title model was unreachable, so these came from the local fallback —
          they are transcript fragments, not written titles. Check GROQ_MODEL in
          .env, or use Copy prompt instead.
        </div>
      ) : null}
      {/* Written from this clip's own words, in the destination account's own
          register. Click to use — the first is the generator's pick. */}
      {titleOptions.length ? (
        <div className="flex flex-col gap-1">
          {titleOptions.map((option, index) => (
            <button
              key={option.text}
              type="button"
              onClick={() => onTitleChange(option.text)}
              title={
                option.flagged.length
                  ? `Not backed by this clip: ${option.flagged.join(", ")}. The words are in the surrounding video, not the 15 seconds a viewer sees.`
                  : undefined
              }
              className={
                option.text === title
                  ? "flex items-start gap-2 rounded border border-sky-400 bg-sky-500/10 px-2 py-1 text-left text-[11px]"
                  : "flex items-start gap-2 rounded border border-white/10 px-2 py-1 text-left text-[11px] hover:border-white/30"
              }
            >
              {/* Grounding verdict, checked against the clip alone. Red means the
                  title promises something these seconds do not deliver. */}
              <span
                className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${TIER_DOT[option.tier] ?? TIER_DOT.yellow}`}
              />
              <span>
                {index === 0 ? <span className="mr-1 opacity-50">pick</span> : null}
                {option.text}
                {option.flagged.length ? (
                  <span className="ml-1 text-red-300">
                    · unsupported: {option.flagged.join(", ")}
                  </span>
                ) : null}
              </span>
            </button>
          ))}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-1">
        <Badge>{preview.score.toFixed(1)}</Badge>
        <span className="font-mono text-xs">
          {mmss(trim.start)}–{mmss(trim.end)}
        </span>
        <span className="text-xs opacity-70">{duration.toFixed(1)}s</span>
        {preview.visual_activity.looks_static ? (
          <span
            className="rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] text-amber-200"
            title={`${preview.visual_activity.kb_per_second} KB/s — one locked-off shot`}
          >
            static
          </span>
        ) : null}
        {preview.visual_payoff?.detected ? (
          <button
            type="button"
            onClick={onExtendEnd}
            className="rounded bg-emerald-500/20 px-1.5 py-0.5 text-[10px] text-emerald-200 hover:bg-emerald-500/40"
            title="The picture gets busier once the talking stops, which is usually the thing being discussed finally on screen. This keeps it."
          >
            reveal after · +{(preview.visual_payoff.extra_seconds ?? 0).toFixed(1)}s
          </button>
        ) : null}
        {sentItemId !== null ? (
          <span
            className="rounded bg-emerald-500/25 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-100"
            title="Sending this window to the same account again is refused; reject that item first, or nudge the trim."
          >
            sent · item #{sentItemId}
          </span>
        ) : null}
        {preview.opens_mid_thought ? (
          <span className="rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] text-amber-200">
            opens mid-thought
          </span>
        ) : null}
        {preview.ends_mid_thought ? (
          <span className="rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] text-amber-200">
            ends mid-thought
          </span>
        ) : null}
      </div>

      {/* Selected span against the footage actually on hand. The marker is the
          ranker's own out-point, so pulling right past it is visibly "keeping
          more than was proposed" rather than an unlabelled drag. */}
      <div className="relative h-1.5 w-full rounded bg-white/10">
        <div
          className="absolute h-full rounded bg-sky-400/70"
          style={{ left: `${pct(trim.start)}%`, width: `${pct(trim.end) - pct(trim.start)}%` }}
        />
        <div
          className="absolute h-full w-px bg-white/50"
          style={{ left: `${pct(preview.end)}%` }}
          title="Where the ranker proposed the clip should end"
        />
      </div>

      <label className="flex items-center gap-2 text-[10px] opacity-70">
        <span className="w-6">In</span>
        <input
          type="range"
          min={floor}
          max={ceiling}
          step={0.1}
          value={trim.start}
          onChange={(event) => setStart(Number(event.target.value))}
          className="h-1 flex-1 accent-sky-400"
        />
        {NUDGES.map((delta) => (
          <button
            key={delta}
            type="button"
            onClick={() => setStart(trim.start + delta)}
            className="rounded border border-white/15 px-1 hover:border-white/40"
          >
            {delta > 0 ? `+${delta}` : delta}
          </button>
        ))}
      </label>
      <label className="flex items-center gap-2 text-[10px] opacity-70">
        <span className="w-6">Out</span>
        <input
          type="range"
          min={floor}
          max={ceiling}
          step={0.1}
          value={trim.end}
          onChange={(event) => setEnd(Number(event.target.value))}
          className="h-1 flex-1 accent-sky-400"
        />
        {NUDGES.map((delta) => (
          <button
            key={delta}
            type="button"
            onClick={() => setEnd(trim.end + delta)}
            className="rounded border border-white/15 px-1 hover:border-white/40"
          >
            {delta > 0 ? `+${delta}` : delta}
          </button>
        ))}
      </label>

      <div className="flex flex-wrap items-center gap-2 text-[10px]">
        {renderState === "done" && renderedUrl ? (
          <Button size="sm" variant="ghost" onClick={onToggleView} className="h-6 px-2 text-[10px]">
            {showRendered ? "Show raw cut" : "Show finished"}
          </Button>
        ) : null}
        {renderState === "rendering" ? (
          <span className="text-sky-300">Rendering the finished version…</span>
        ) : null}
        {renderState === "queued" ? <span className="opacity-50">Queued to render</span> : null}
        {renderState === "failed" ? <span className="text-red-300">Render failed</span> : null}
        {renderState === "idle" || renderStale || renderState === "failed" ? (
          <Button size="sm" variant="ghost" onClick={onRender} className="h-6 px-2 text-[10px]">
            {renderState === "failed"
              ? "Try again"
              : renderStale
                ? "Re-render"
                : "Render finished"}
          </Button>
        ) : null}
        {/* Covers both causes: the batch renders before the hook is written, so
            typing a title invalidates every card, not just a trimmed one. */}
        {renderStale && renderState === "done" ? (
          <span className="text-amber-300">
            Trim, title or account changed — showing the old cut
          </span>
        ) : null}
      </div>

      <div className="text-[11px] opacity-75">{preview.reasons.join(" | ")}</div>
      <div className="line-clamp-3 text-[11px] opacity-55">{preview.context}</div>
    </div>
  );
}
