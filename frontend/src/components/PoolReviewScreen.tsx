import { useCallback, useEffect, useState } from "react";

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

export function PoolReviewScreen() {
  const [niche, setNiche] = useState("history");
  const [sourceFilter, setSourceFilter] = useState("");
  const [items, setItems] = useState<PoolReviewItem[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [rejectReasons, setRejectReasons] = useState<Record<number, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [thumbErrors, setThumbErrors] = useState<Record<number, boolean>>({});

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

  const toggle = (poolItemId: number) => {
    setSelected((current) =>
      current.includes(poolItemId)
        ? current.filter((id) => id !== poolItemId)
        : [...current, poolItemId],
    );
  };

  const approve = async (ids: number[]) => {
    try {
      const result = await bridge.approvePoolItems(ids);
      setSelected((current) => current.filter((id) => !ids.includes(id)));
      setMessage(`${result.approved} approved.`);
      await load();
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : String(err));
    }
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

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Review</h1>
          <p className="text-sm text-muted-foreground">
            {loading ? "Loading clips…" : `${items.length} pending clip(s)`}
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
          <Button size="sm" disabled={!selected.length} onClick={() => void approve(selected)}>
            Approve selected
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

        {items.map((item) => (
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
                <span className="rounded-full bg-emerald-500/15 px-2 py-1 font-medium text-emerald-500">
                  Tier {item.topic_tier}
                </span>
                <span className="uppercase text-muted-foreground">{item.suggested_action}</span>
              </div>
              <div className="text-muted-foreground">
                ER {(item.source_er * 100).toFixed(1)}% · {item.fit_score.toFixed(3)}
              </div>
            </div>
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
          </div>
        ))}

        {!loading && !items.length && (
          <p className="border-t border-border px-3 py-8 text-center text-sm text-muted-foreground">
            No pending clips.
          </p>
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
