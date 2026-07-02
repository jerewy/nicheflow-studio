import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { bridge } from "@/lib/bridge";
import type { PoolReviewItem } from "@/types";

const NICHES = [
  { value: "history", label: "History" },
  { value: "movie", label: "Movie" },
];

// Downloads run via yt-dlp and can take a while on a long reel.
const PREVIEW_TIMEOUT_MS = 120000;

// Poll a background download job until it finishes; resolve when done or throw
// its error. Reports progress messages on each poll.
async function waitForJob(
  jobId: string,
  onProgress?: (value: number, message: string) => void,
): Promise<void> {
  const deadline = Date.now() + PREVIEW_TIMEOUT_MS;
  for (;;) {
    const snapshot = await bridge.getJob(jobId);
    onProgress?.(snapshot.progress, snapshot.message);
    if (snapshot.status === "succeeded") return;
    if (snapshot.status === "failed") throw new Error(snapshot.error ?? "The download failed.");
    if (Date.now() > deadline) throw new Error("The download timed out.");
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

function formatNumber(value: number | null): string {
  return value === null ? "-" : value.toLocaleString();
}

function formatDate(value: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleDateString();
}

function formatDuration(value: number | null): string {
  if (value === null) return "-";
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  const seconds = value % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function metricLine(item: PoolReviewItem): string {
  return [
    `${formatNumber(item.view_count)} views`,
    `${formatNumber(item.like_count)} likes`,
    `${formatNumber(item.comment_count)} comments`,
    formatDuration(item.duration_seconds),
    formatDate(item.published_at),
  ].join(" / ");
}

const GRID_COLS = "grid-cols-[2.5rem_6rem_minmax(0,1fr)_9rem_10rem_18rem]";

// Render the queue in batches and grow it on scroll. The backend returns every
// pending row in one query, but rendering hundreds at once (each firing a
// thumbnail request) is slow and makes the page huge — so the list is windowed.
const ROW_BATCH = 12;

// Per-row download/approve progress. Presence in the map means the row is busy.
interface ApproveProgress {
  value: number;
  message: string;
}

// Dependency-free spinner; inherits the surrounding text color via border-current.
function Spinner() {
  return (
    <span
      className="inline-block h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent"
      aria-hidden
    />
  );
}

export function PoolReviewScreen() {
  const [niche, setNiche] = useState("history");
  const [sourceFilter, setSourceFilter] = useState("");
  const [items, setItems] = useState<PoolReviewItem[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [rejectReasons, setRejectReasons] = useState<Record<number, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [thumbErrors, setThumbErrors] = useState<Record<number, boolean>>({});
  // Rows currently downloading-then-approving, keyed by pool_item_id.
  const [approving, setApproving] = useState<Record<number, ApproveProgress>>({});
  const [approveErrors, setApproveErrors] = useState<Record<number, string>>({});
  // Infinite-scroll window: how many rows are rendered, grown by ROW_BATCH as the
  // bottom sentinel scrolls into view.
  const [visibleCount, setVisibleCount] = useState(ROW_BATCH);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  // Preview modal state.
  const [previewItem, setPreviewItem] = useState<PoolReviewItem | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewMessage, setPreviewMessage] = useState("");
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewThumbError, setPreviewThumbError] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await bridge.poolReviewQueue(niche, sourceFilter.trim() || null));
      setVisibleCount(ROW_BATCH);
      setThumbErrors({});
      setMessage(null);
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [niche, sourceFilter]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  // Grow the rendered window when the bottom sentinel nears the viewport.
  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          setVisibleCount((count) => Math.min(count + ROW_BATCH, items.length));
        }
      },
      { rootMargin: "300px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [items.length, visibleCount]);

  const toggle = (poolItemId: number) => {
    setSelected((current) =>
      current.includes(poolItemId)
        ? current.filter((id) => id !== poolItemId)
        : [...current, poolItemId],
    );
  };

  // Approving downloads the clip's footage first (so it's on disk and reused at
  // distribution), then flips the pool item to accepted. Downloads serialize on
  // the backend, so we process ids sequentially and surface per-row progress.
  const approve = async (ids: number[]) => {
    if (!ids.length) return;
    setApproveErrors((current) => {
      const next = { ...current };
      for (const id of ids) delete next[id];
      return next;
    });
    setApproving((current) => {
      const next = { ...current };
      for (const id of ids) next[id] = { value: 0, message: "Queued…" };
      return next;
    });

    let approved = 0;
    const failed: number[] = [];
    for (const id of ids) {
      try {
        setApproving((current) => ({ ...current, [id]: { value: 0, message: "Starting…" } }));
        const { job_id } = await bridge.startPoolItemPreviewDownload(id);
        await waitForJob(job_id, (value, message) =>
          setApproving((current) => ({
            ...current,
            [id]: { value, message: message || "Downloading…" },
          })),
        );
        await bridge.approvePoolItems([id]);
        approved += 1;
      } catch (err: unknown) {
        failed.push(id);
        setApproveErrors((current) => ({
          ...current,
          [id]: err instanceof Error ? err.message : String(err),
        }));
      } finally {
        setApproving((current) => {
          const next = { ...current };
          delete next[id];
          return next;
        });
      }
    }

    setSelected((current) => current.filter((id) => !ids.includes(id)));
    await load();
    // load() clears the status line, so set the result message after it.
    setMessage(
      failed.length ? `${approved} approved, ${failed.length} failed.` : `${approved} approved.`,
    );
  };

  const reject = async (item: PoolReviewItem) => {
    const reason = rejectReasons[item.pool_item_id]?.trim() || "rejected in review";
    try {
      const result = await bridge.rejectPoolItems([item.pool_item_id], reason);
      setSelected((current) => current.filter((id) => id !== item.pool_item_id));
      setMessage(`${result.rejected} rejected.`);
      await load();
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    }
  };

  const openPreview = (item: PoolReviewItem) => {
    setPreviewItem(item);
    setPreviewUrl(item.preview_url);
    setPreviewBusy(false);
    setPreviewMessage("");
    setPreviewError(null);
    setPreviewThumbError(false);
  };

  const closePreview = () => setPreviewItem(null);

  // Download the footage on demand, then play it. Pending clips have no local
  // file yet, so this fetches it (review-only — it does not approve the clip).
  const loadVideo = async () => {
    if (!previewItem) return;
    const id = previewItem.pool_item_id;
    setPreviewBusy(true);
    setPreviewError(null);
    setPreviewMessage("Downloading clip…");
    try {
      const { job_id } = await bridge.startPoolItemPreviewDownload(id);
      await waitForJob(job_id, (_value, msg) => setPreviewMessage(msg || "Downloading…"));
      const preview = await bridge.poolItemPreview(id);
      if (!preview.preview_url) {
        setPreviewError("Downloaded, but no playable file was produced.");
        return;
      }
      setPreviewUrl(preview.preview_url);
      // Cache the URL on the row so reopening this clip plays instantly.
      setItems((current) =>
        current.map((row) =>
          row.pool_item_id === id ? { ...row, preview_url: preview.preview_url } : row,
        ),
      );
    } catch (err: unknown) {
      setPreviewError(err instanceof Error ? err.message : String(err));
    } finally {
      setPreviewBusy(false);
      setPreviewMessage("");
    }
  };

  const bulkApproving = Object.keys(approving).length > 0;

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Review</h1>
          <p className="text-sm text-muted-foreground">
            {loading ? "Loading clips…" : `${items.length} pending clip(s)`}
          </p>
          <p className="mt-0.5 text-xs text-muted-foreground/80">
            Ranked by fit score (tier weight × engagement rate × recency). The Tier badge is topic
            only — it doesn&apos;t set the order, so a high-engagement Tier A can outrank a Tier S.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
            value={niche}
            onChange={(event) => {
              setNiche(event.target.value);
              setSelected([]);
            }}
          >
            {NICHES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <input
            className="h-9 w-56 rounded-md border border-input bg-transparent px-3 text-sm"
            placeholder="Source"
            value={sourceFilter}
            onChange={(event) => setSourceFilter(event.target.value)}
          />
          <Button size="sm" variant="outline" disabled={loading} onClick={() => void load()}>
            {loading ? "Refreshing..." : "Refresh"}
          </Button>
          <Button
            size="sm"
            disabled={!selected.length || bulkApproving}
            onClick={() => void approve(selected)}
          >
            {bulkApproving ? "Approving…" : "Approve selected"}
          </Button>
        </div>
      </div>

      {message && <p className="text-sm text-muted-foreground">{message}</p>}

      <div className="overflow-hidden rounded-lg border border-border">
        <div
          className={`grid ${GRID_COLS} gap-0 bg-muted/40 px-3 py-2 text-xs font-medium uppercase text-muted-foreground`}
        >
          <span />
          <span>Preview</span>
          <span>Clip</span>
          <span>Source</span>
          <span>Score</span>
          <span>Action</span>
        </div>

        {loading && !items.length &&
          Array.from({ length: 6 }).map((_unused, index) => (
            <div
              key={`skeleton-${index}`}
              className={`grid ${GRID_COLS} items-center gap-0 border-t border-border px-3 py-3`}
            >
              <span />
              <div className="h-24 w-16 animate-pulse rounded-md bg-muted" />
              <div className="space-y-2 pr-4">
                <div className="h-4 w-3/4 animate-pulse rounded bg-muted" />
                <div className="h-3 w-1/2 animate-pulse rounded bg-muted" />
              </div>
              <div className="h-3 w-20 animate-pulse rounded bg-muted" />
              <div className="h-6 w-16 animate-pulse rounded bg-muted" />
              <div className="h-8 w-full animate-pulse rounded bg-muted" />
            </div>
          ))}

        {items.slice(0, visibleCount).map((item) => (
          <div
            key={item.pool_item_id}
            className={`grid ${GRID_COLS} items-center gap-0 border-t border-border px-3 py-3`}
          >
            <input
              type="checkbox"
              checked={selected.includes(item.pool_item_id)}
              onChange={() => toggle(item.pool_item_id)}
            />
            <button
              type="button"
              onClick={() => openPreview(item)}
              title="Preview clip"
              className="group relative h-24 w-16 overflow-hidden rounded-md border border-border bg-black"
            >
              {item.thumbnail_url && !thumbErrors[item.pool_item_id] ? (
                <img
                  src={item.thumbnail_url}
                  alt=""
                  loading="lazy"
                  className="h-full w-full object-cover"
                  onError={() =>
                    setThumbErrors((current) => ({ ...current, [item.pool_item_id]: true }))
                  }
                />
              ) : (
                <div className="flex h-full items-center justify-center px-1 text-center text-[10px] leading-tight text-muted-foreground">
                  {thumbErrors[item.pool_item_id] ? "Preview expired" : "No thumbnail"}
                </div>
              )}
              <span className="absolute inset-0 flex items-center justify-center text-lg text-white opacity-0 transition group-hover:bg-black/45 group-hover:opacity-100">
                ▶
              </span>
            </button>
            <div className="min-w-0 space-y-1 pr-4">
              <p className="truncate font-medium" title={item.clip_label}>
                {item.clip_label}
              </p>
              <p className="text-xs text-muted-foreground">{metricLine(item)}</p>
              {item.description && (
                <p className="max-h-10 overflow-hidden text-xs text-muted-foreground">
                  {item.description}
                </p>
              )}
            </div>
            <div className="truncate text-sm text-muted-foreground" title={item.source_label}>
              {item.source_label}
            </div>
            <div className="space-y-1 text-xs">
              <div className="flex items-center gap-1.5">
                <span
                  className="rounded-full bg-emerald-500/15 px-2 py-1 font-medium text-emerald-500"
                  title="Topic tier from keyword match (subject only). Does not set the row order."
                >
                  Tier {item.topic_tier}
                </span>
                <span
                  className="uppercase text-muted-foreground"
                  title="Advisory only. A clip over 35s is only flagged REJECT when its engagement rate is also weak; long clips that engage well stay accept."
                >
                  {item.suggested_action}
                </span>
              </div>
              <div
                className="text-muted-foreground"
                title="ER = engagement rate. Fit score = tier weight × ER × recency, and sets the sort order."
              >
                ER {(item.source_er * 100).toFixed(1)}% · {item.fit_score.toFixed(3)}
              </div>
            </div>
            {approving[item.pool_item_id] ? (
              <div className="space-y-1.5">
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Spinner />
                  <span className="truncate">
                    {approving[item.pool_item_id].message} ·{" "}
                    {Math.round(approving[item.pool_item_id].value * 100)}%
                  </span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                  <div
                    className="h-full rounded-full bg-emerald-500 transition-all duration-300"
                    style={{
                      width: `${Math.max(Math.round(approving[item.pool_item_id].value * 100), 8)}%`,
                    }}
                  />
                </div>
              </div>
            ) : (
              <div className="space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <Button size="sm" onClick={() => void approve([item.pool_item_id])}>
                    Approve
                  </Button>
                  <input
                    className="h-8 w-32 rounded-md border border-input bg-transparent px-2 text-xs"
                    placeholder="Reason"
                    value={rejectReasons[item.pool_item_id] ?? ""}
                    onChange={(event) =>
                      setRejectReasons((current) => ({
                        ...current,
                        [item.pool_item_id]: event.target.value,
                      }))
                    }
                  />
                  <Button size="sm" variant="destructive" onClick={() => void reject(item)}>
                    Reject
                  </Button>
                </div>
                {approveErrors[item.pool_item_id] && (
                  <p className="text-xs text-destructive">{approveErrors[item.pool_item_id]}</p>
                )}
              </div>
            )}
          </div>
        ))}

        {!loading && !items.length && (
          <p className="border-t border-border px-3 py-8 text-center text-sm text-muted-foreground">
            No pending clips.
          </p>
        )}

        {!loading && visibleCount < items.length && (
          <div
            ref={sentinelRef}
            className="flex items-center justify-center gap-2 border-t border-border px-3 py-4 text-xs text-muted-foreground"
          >
            <Spinner />
            <span>
              Showing {visibleCount} of {items.length}…
            </span>
          </div>
        )}
      </div>

      {previewItem && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={closePreview}
        >
          <div
            className="max-h-[90vh] w-full max-w-3xl overflow-auto rounded-xl border border-border bg-card p-5 shadow-2xl"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-wider text-muted-foreground">
                  {previewItem.source_label} · Tier {previewItem.topic_tier}
                </p>
                <h2 className="mt-1 truncate text-lg font-semibold" title={previewItem.clip_label}>
                  {previewItem.clip_label}
                </h2>
              </div>
              <Button size="sm" variant="ghost" onClick={closePreview}>
                Close
              </Button>
            </div>

            <div className="mt-4 grid gap-4 sm:grid-cols-[260px_1fr]">
              <div className="flex aspect-[9/16] items-center justify-center overflow-hidden rounded-lg border border-border bg-black">
                {previewUrl ? (
                  <video
                    key={previewUrl}
                    src={previewUrl}
                    controls
                    autoPlay
                    className="h-full w-full object-contain"
                  />
                ) : previewItem.thumbnail_url && !previewThumbError ? (
                  <img
                    src={previewItem.thumbnail_url}
                    alt=""
                    className="h-full w-full object-cover"
                    onError={() => setPreviewThumbError(true)}
                  />
                ) : (
                  <div className="flex h-full flex-col items-center justify-center gap-1 px-3 text-center text-xs text-muted-foreground">
                    {previewThumbError ? (
                      <>
                        <span>Preview unavailable</span>
                        <span className="opacity-60">(CDN link expired)</span>
                      </>
                    ) : (
                      <span>No preview yet</span>
                    )}
                  </div>
                )}
              </div>

              <div className="space-y-3 text-sm">
                <p className="text-xs text-muted-foreground">{metricLine(previewItem)}</p>
                {previewItem.description && (
                  <p className="max-h-40 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-muted/30 p-3 text-xs text-muted-foreground">
                    {previewItem.description}
                  </p>
                )}

                {!previewUrl && (
                  <Button size="sm" disabled={previewBusy} onClick={() => void loadVideo()}>
                    {previewBusy ? previewMessage || "Loading…" : "Load video"}
                  </Button>
                )}
                {!previewUrl && !previewBusy && (
                  <p className="text-xs text-muted-foreground">
                    The clip isn&apos;t downloaded yet. Loading fetches the footage so you can watch
                    it here — it does not approve the clip.
                  </p>
                )}
                {previewError && <p className="text-xs text-destructive">{previewError}</p>}

                {previewItem.source_url && (
                  <a
                    href={previewItem.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="block truncate text-xs text-sky-500 hover:underline"
                    title={previewItem.source_url}
                  >
                    Open on Instagram
                  </a>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
