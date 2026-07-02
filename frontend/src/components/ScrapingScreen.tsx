import { useCallback, useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { bridge } from "@/lib/bridge";
import type { ApifyUsage, ScrapeCandidate, ScrapeToPoolResult, SourceProfile } from "@/types";

const SCRAPE_TIMEOUT_MS = 600000;

// Poll a background scrape job until it finishes; resolve with its result or
// throw its error message. Reports progress on each poll.
async function waitForJob(
  jobId: string,
  onProgress?: (value: number, message: string) => void,
): Promise<unknown> {
  const deadline = Date.now() + SCRAPE_TIMEOUT_MS;
  for (;;) {
    const snapshot = await bridge.getJob(jobId);
    onProgress?.(snapshot.progress, snapshot.message);
    if (snapshot.status === "succeeded") return snapshot.result;
    if (snapshot.status === "failed") throw new Error(snapshot.error ?? "The scrape failed.");
    if (Date.now() > deadline) throw new Error("The scrape timed out.");
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

interface ScrapingScreenProps {
  activeAccountId: number;
  activeAccountName: string | null;
}

const CANDIDATE_FILTERS = [
  { value: "all", label: "All" },
  { value: "candidate", label: "Ready to review" },
  { value: "ignored", label: "Ignored" },
  { value: "queued", label: "Queued" },
  { value: "downloaded", label: "Downloaded" },
  { value: "pooled", label: "In pool" },
];

// Reject reasons mirror db.pools.REJECT_REASONS. Rejecting also pulls any pooled
// copy of the clip out of distribution (reversible from the desktop app).
const REJECT_REASON_OPTIONS = [
  { value: "low_quality", label: "Low quality" },
  { value: "duplicate", label: "Duplicate (dedup missed)" },
  { value: "wrong_niche", label: "Wrong niche" },
  { value: "ad_campaign", label: "Ad / promo" },
];

function shortDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString();
}

// Columns the candidate table can sort by (Review is an action column, not sortable).
type SortKey = "state" | "title" | "channel_name" | "like_count" | "published_at";

// Comparable value for a candidate under a given sort key. Text sorts
// case-insensitively; missing numbers/dates sink to -Infinity so they land at
// the bottom of a "highest first" sort.
function sortValue(c: ScrapeCandidate, key: SortKey): string | number {
  switch (key) {
    case "title":
      return (c.title ?? c.source_url ?? "").toLowerCase();
    case "channel_name":
      return (c.channel_name ?? "").toLowerCase();
    case "state":
      return c.state ?? "";
    case "like_count":
      return c.like_count ?? Number.NEGATIVE_INFINITY;
    case "published_at":
      return c.published_at ? new Date(c.published_at).getTime() : Number.NEGATIVE_INFINITY;
  }
}

export function ScrapingScreen({ activeAccountId, activeAccountName }: ScrapingScreenProps) {
  const [sources, setSources] = useState<SourceProfile[]>([]);
  const [candidates, setCandidates] = useState<ScrapeCandidate[]>([]);
  // Scraping auto-pools every clip it pulls, so the meaningful review surface is
  // the pool: act on pooled clips (Add to Processing / Reject) rather than an
  // explicit accept step.
  const [filter, setFilter] = useState("pooled");
  const [newUrl, setNewUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Candidate whose detail panel is open, and the reason chosen for a reject.
  const [selected, setSelected] = useState<ScrapeCandidate | null>(null);
  const [thumbnailError, setThumbnailError] = useState(false);
  const [rejectReason, setRejectReason] = useState("low_quality");
  // Apify free-tier usage + per-source scrape progress.
  const [usage, setUsage] = useState<ApifyUsage | null>(null);
  const [scrapingSourceId, setScrapingSourceId] = useState<number | null>(null);
  const [scrapeProgress, setScrapeProgress] = useState<{ value: number; message: string } | null>(
    null,
  );
  // How many recent posts a "Scrape -> pool" pulls. Default 30; raise it to
  // backfill a source's whole history in one run (first scrape of a source has
  // no "newer than" cutoff, so it pulls the most recent N).
  const [scrapeCount, setScrapeCount] = useState(30);
  // Set of candidate ids currently being downloaded — lets multiple downloads
  // run concurrently without locking the whole table.
  const [addingIds, setAddingIds] = useState<Set<number>>(new Set());
  // Per-candidate download progress while "Add to Processing" fetches the clip.
  const [addProgress, setAddProgress] = useState<
    Record<number, { value: number; message: string }>
  >({});
  // Client-side table sort. null key keeps the backend order (newest first).
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((dir) => (dir === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(key);
    // Numbers/dates are most useful highest-first; text reads best A→Z.
    setSortDir(key === "like_count" || key === "published_at" ? "desc" : "asc");
  };

  const sortedCandidates = useMemo(() => {
    if (!sortKey) return candidates;
    const factor = sortDir === "asc" ? 1 : -1;
    return [...candidates].sort((a, b) => {
      const va = sortValue(a, sortKey);
      const vb = sortValue(b, sortKey);
      if (va < vb) return -factor;
      if (va > vb) return factor;
      return 0;
    });
  }, [candidates, sortKey, sortDir]);

  const sortIndicator = (key: SortKey) =>
    sortKey === key ? (sortDir === "asc" ? " ▲" : " ▼") : "";

  const loadSources = useCallback(async () => {
    try {
      setSources(await bridge.listSources(activeAccountId));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [activeAccountId]);

  const loadCandidates = useCallback(async () => {
    try {
      setCandidates(await bridge.listCandidates(activeAccountId, filter));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [activeAccountId, filter]);

  const loadUsage = useCallback(async () => {
    try {
      setUsage(await bridge.apifyUsage());
    } catch {
      // Non-fatal: the free-tier reminder just won't show.
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadSources();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadSources]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadCandidates();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadCandidates]);
  useEffect(() => {
    const timer = window.setTimeout(loadUsage, 0);
    return () => window.clearTimeout(timer);
  }, [loadUsage]);

  const run = async (
    fn: () => Promise<unknown>,
    note: string,
    reload: () => Promise<void>,
  ): Promise<boolean> => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await fn();
      await reload();
      setMessage(note);
      return true;
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
      return false;
    } finally {
      setBusy(false);
    }
  };

  // A review action from the detail panel: run it, reload, and close the panel
  // only on success so a failure keeps the panel (and its error) visible.
  const reviewAction = (fn: () => Promise<unknown>, note: string) => {
    void run(fn, note, loadCandidates).then((ok) => {
      if (ok) setSelected(null);
    });
  };

  // Download the candidate's clip (or reuse it if on disk) and add it to
  // Processing as a new item. Runs as a background job and can be slow.
  // Does NOT set the global busy flag so multiple downloads can run in parallel.
  const addToProcessing = async (candidate: ScrapeCandidate) => {
    setAddingIds((prev) => new Set([...prev, candidate.id]));
    setError(null);
    setMessage(null);
    try {
      const { job_id } = await bridge.startCandidateDownload(candidate.id);
      const result = (await waitForJob(job_id, (value, message) =>
        setAddProgress((prev) => ({ ...prev, [candidate.id]: { value, message } })),
      )) as {
        item_id: number;
        reused: boolean;
        downloaded: boolean;
        message: string;
      };
      setMessage(`${result.message} Now in Processing (item #${result.item_id}).`);
      setSelected(null);
      await loadCandidates();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAddingIds((prev) => {
        const next = new Set(prev);
        next.delete(candidate.id);
        return next;
      });
      setAddProgress((prev) => {
        const next = { ...prev };
        delete next[candidate.id];
        return next;
      });
    }
  };

  // Scrape one source via Apify into its account's niche pool (deduped), with a
  // live progress bar. Refuses to silently exceed the free tier.
  const scrapeSource = async (src: SourceProfile): Promise<void> => {
    if (
      usage?.over_free_tier &&
      !window.confirm(
        "You're over the Apify free tier this month — scraping may incur charges. Continue?",
      )
    )
      return;
    setBusy(true);
    setError(null);
    setMessage(null);
    setScrapingSourceId(src.id);
    setScrapeProgress({ value: 0, message: "Starting…" });
    try {
      const { job_id } = await bridge.startSourceScrape(src.id, Math.max(1, scrapeCount));
      const result = (await waitForJob(job_id, (value, message) =>
        setScrapeProgress({ value, message }),
      )) as ScrapeToPoolResult;
      setMessage(
        `Scraped ${src.label}: ${result.added} added, ${result.duplicates} duplicate(s) of ${result.scraped} result(s).`,
      );
      await loadUsage();
      await loadSources();
      await loadCandidates();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      setScrapingSourceId(null);
      setScrapeProgress(null);
    }
  };

  const addSource = () => {
    if (!newUrl.trim()) return;
    void (async () => {
      setBusy(true);
      setError(null);
      setMessage(null);
      try {
        const created = await bridge.addSource(activeAccountId, newUrl.trim());
        setNewUrl("");
        await loadSources();
        setMessage("Source added.");
        // Adding a source also pulls it into the pool — but never blow past the
        // free tier without a heads-up (scrapeSource confirms when over).
        if (!usage?.over_free_tier) {
          await scrapeSource(created);
        }
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Scraping</h1>
        <p className="text-sm text-muted-foreground">
          Source intake for {activeAccountName ?? "this account"}. Distributed clips are reviewed
          once in Processing; this screen keeps source and Apify intake history.
          source pulls its recent posts into this account's niche pool via Apify (deduplicated).
        </p>
      </div>

      {usage && (
        <div
          className={`rounded-md border px-3 py-2 text-sm ${
            usage.over_free_tier
              ? "border-destructive/50 bg-destructive/10 text-destructive"
              : usage.warn
                ? "border-amber-500/50 bg-amber-500/10 text-amber-600 dark:text-amber-400"
                : "border-border text-muted-foreground"
          }`}
        >
          Apify free tier — <span className="font-medium">{usage.used.toLocaleString()}</span> /{" "}
          {usage.free_cap.toLocaleString()} results used this month ({usage.remaining.toLocaleString()}{" "}
          left).
          {usage.over_free_tier
            ? " Over the free tier — further scrapes may be billed."
            : usage.warn
              ? " Approaching the cap."
              : ""}
        </div>
      )}

      {error && <p className="text-sm text-destructive">{error}</p>}
      {message && <p className="text-sm text-emerald-600">{message}</p>}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Source profiles</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Input
              className="max-w-md"
              placeholder="https://www.instagram.com/<handle>/ or /explore/tags/<tag>"
              value={newUrl}
              onChange={(e) => setNewUrl(e.target.value)}
            />
            <Button onClick={addSource} disabled={busy || !newUrl.trim()}>
              Add source
            </Button>
            <label className="ml-auto flex items-center gap-2 text-sm text-muted-foreground">
              Posts per scrape
              <Input
                type="number"
                min={1}
                className="w-24"
                value={scrapeCount}
                onChange={(e) => setScrapeCount(Math.max(1, Number(e.target.value) || 1))}
              />
            </label>
          </div>
          {sources.length === 0 ? (
            <p className="text-sm text-muted-foreground">No sources yet.</p>
          ) : (
            <ul className="space-y-1">
              {sources.map((src) => (
                <li
                  key={src.id}
                  className="flex flex-wrap items-center gap-2 border-b border-border py-2 text-sm last:border-0"
                >
                  <span className="font-medium">{src.label}</span>
                  <Badge variant="outline">{src.source_type.replace("instagram_", "")}</Badge>
                  {!src.enabled && <Badge variant="secondary">disabled</Badge>}
                  {src.last_run_status && (
                    <span className="text-xs text-muted-foreground">{src.last_run_status}</span>
                  )}
                  <span className="grow" />
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={busy}
                    onClick={() => scrapeSource(src)}
                  >
                    {scrapingSourceId === src.id ? "Scraping…" : "Scrape → pool"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy}
                    onClick={() =>
                      run(
                        () => bridge.setSourceEnabled(src.id, !src.enabled),
                        src.enabled ? "Source disabled." : "Source enabled.",
                        loadSources,
                      )
                    }
                  >
                    {src.enabled ? "Disable" : "Enable"}
                  </Button>
                  <Button
                    size="sm"
                    variant="destructive"
                    disabled={busy}
                    onClick={() => {
                      if (!window.confirm(`Remove source ${src.label}?`)) return;
                      void run(
                        () => bridge.removeSource(src.id),
                        "Source removed.",
                        loadSources,
                      );
                    }}
                  >
                    Remove
                  </Button>
                </li>
              ))}
            </ul>
          )}
          {scrapeProgress && (
            <div className="space-y-1">
              <div className="h-2 w-full overflow-hidden rounded-full bg-secondary">
                <div
                  className="h-full bg-primary transition-[width] duration-300"
                  style={{ width: `${Math.round(scrapeProgress.value * 100)}%` }}
                />
              </div>
              <p className="text-xs text-muted-foreground">{scrapeProgress.message}</p>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <CardTitle className="text-base">Intake history</CardTitle>
          <select
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          >
            {CANDIDATE_FILTERS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
        </CardHeader>
        <CardContent>
          {candidates.length === 0 ? (
            <p className="text-sm text-muted-foreground">No candidates for this filter.</p>
          ) : (
            <div className="max-h-[460px] overflow-auto rounded-md border border-border">
              <table className="w-full text-left text-sm">
                <thead className="sticky top-0 z-10 bg-muted text-xs text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 font-medium">
                      <button
                        type="button"
                        className="flex items-center gap-1 hover:text-foreground"
                        onClick={() => toggleSort("state")}
                      >
                        State{sortIndicator("state")}
                      </button>
                    </th>
                    <th className="px-3 py-2 font-medium">
                      <button
                        type="button"
                        className="flex items-center gap-1 hover:text-foreground"
                        onClick={() => toggleSort("title")}
                      >
                        Title{sortIndicator("title")}
                      </button>
                    </th>
                    <th className="px-3 py-2 font-medium">
                      <button
                        type="button"
                        className="flex items-center gap-1 hover:text-foreground"
                        onClick={() => toggleSort("channel_name")}
                      >
                        Source{sortIndicator("channel_name")}
                      </button>
                    </th>
                    <th className="px-3 py-2 text-right font-medium">
                      <button
                        type="button"
                        className="flex w-full items-center justify-end gap-1 hover:text-foreground"
                        onClick={() => toggleSort("like_count")}
                      >
                        Likes{sortIndicator("like_count")}
                      </button>
                    </th>
                    <th className="px-3 py-2 font-medium">
                      <button
                        type="button"
                        className="flex items-center gap-1 hover:text-foreground"
                        onClick={() => toggleSort("published_at")}
                      >
                        Published{sortIndicator("published_at")}
                      </button>
                    </th>
                    <th className="px-3 py-2 font-medium">Review</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedCandidates.map((c) => (
                    <tr
                      key={c.id}
                      onClick={() => { setSelected(c); setThumbnailError(false); }}
                      className="cursor-pointer border-t border-border hover:bg-accent"
                    >
                      <td className="px-3 py-2">
                        <Badge variant={c.state === "ignored" ? "secondary" : "outline"}>
                          {c.state}
                        </Badge>
                      </td>
                      <td className="max-w-xs truncate px-3 py-2" title={c.title ?? c.source_url}>
                        {c.title ?? c.source_url}
                      </td>
                      <td className="px-3 py-2 text-muted-foreground">{c.channel_name ?? "—"}</td>
                      <td className="px-3 py-2 text-right">{c.like_count?.toLocaleString() ?? "—"}</td>
                      <td className="px-3 py-2 text-xs text-muted-foreground">
                        {shortDate(c.published_at)}
                      </td>
                      <td className="px-3 py-2" onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center gap-2">
                          {["candidate", "pooled", "queued"].includes(c.state) && (
                            <Button
                              size="sm"
                              disabled={addingIds.has(c.id)}
                              title={addProgress[c.id]?.message}
                              onClick={() => addToProcessing(c)}
                            >
                              {addingIds.has(c.id)
                                ? addProgress[c.id]
                                  ? `${Math.round(addProgress[c.id].value * 100)}% — ${addProgress[c.id].message || "Downloading…"}`
                                  : "Adding…"
                                : "Add to Processing"}
                            </Button>
                          )}
                          {c.state === "ignored" ? (
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busy || addingIds.has(c.id)}
                              onClick={() =>
                                run(
                                  () => bridge.setCandidateState(c.id, "candidate"),
                                  "Candidate restored.",
                                  loadCandidates,
                                )
                              }
                            >
                              Restore
                            </Button>
                          ) : (
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={busy || addingIds.has(c.id)}
                              onClick={() =>
                                run(
                                  () => bridge.setCandidateState(c.id, "ignored"),
                                  "Candidate ignored.",
                                  loadCandidates,
                                )
                              }
                            >
                              Ignore
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="mt-2 text-xs text-muted-foreground">
            Scraping pools clips automatically. Distributed clips no longer appear here — they go
            straight to the Processing screen as pending-review items (review and reject them
            there). Distribute pooled clips from Pool &amp; Distribute.
          </p>
        </CardContent>
      </Card>

      {selected && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setSelected(null)}
        >
          <div
            className="max-h-[85vh] w-full max-w-2xl overflow-auto rounded-xl border border-border bg-card p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-xs uppercase tracking-wider text-muted-foreground">
                  Candidate #{selected.id}
                </p>
                <h2 className="mt-1 text-lg font-semibold">{selected.title ?? "(untitled)"}</h2>
              </div>
              <Button size="sm" variant="ghost" onClick={() => setSelected(null)}>
                Close
              </Button>
            </div>

            <div className="mt-4 grid gap-4 sm:grid-cols-[160px_1fr]">
              <div className="aspect-[9/16] overflow-hidden rounded-lg border border-border bg-black">
                {selected.thumbnail_url && !thumbnailError ? (
                  <img
                    src={selected.thumbnail_url}
                    alt=""
                    className="h-full w-full object-cover"
                    onError={() => setThumbnailError(true)}
                  />
                ) : (
                  <div className="flex h-full flex-col items-center justify-center gap-1 px-2 text-center text-xs text-muted-foreground">
                    {thumbnailError ? (
                      <>
                        <span>Preview unavailable</span>
                        <span className="opacity-60">(CDN link expired)</span>
                      </>
                    ) : "No thumbnail"}
                  </div>
                )}
              </div>
              <div className="space-y-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={selected.state === "ignored" ? "secondary" : "outline"}>
                    {selected.state}
                  </Badge>
                  <span className="text-muted-foreground">{selected.channel_name ?? "—"}</span>
                </div>
                <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
                  <div>
                    <dt className="text-muted-foreground">Likes</dt>
                    <dd>{selected.like_count?.toLocaleString() ?? "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Views</dt>
                    <dd>{selected.view_count?.toLocaleString() ?? "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Comments</dt>
                    <dd>{selected.comment_count?.toLocaleString() ?? "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Duration</dt>
                    <dd>{selected.duration_seconds ? `${selected.duration_seconds}s` : "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Published</dt>
                    <dd>{shortDate(selected.published_at)}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Added</dt>
                    <dd>{shortDate(selected.created_at)}</dd>
                  </div>
                </dl>
                <a
                  href={selected.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="block truncate text-xs text-sky-500 hover:underline"
                  title={selected.source_url}
                >
                  {selected.source_url}
                </a>
              </div>
            </div>

            {selected.description && (
              <p className="mt-4 max-h-40 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-muted/30 p-3 text-sm text-muted-foreground">
                {selected.description}
              </p>
            )}

            <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-border pt-4">
              {selected.state === "ignored" ? (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() =>
                    reviewAction(
                      () => bridge.setCandidateState(selected.id, "candidate"),
                      "Candidate restored.",
                    )
                  }
                >
                  Restore
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() =>
                    reviewAction(
                      () => bridge.setCandidateState(selected.id, "ignored"),
                      "Candidate ignored.",
                    )
                  }
                >
                  Ignore
                </Button>
              )}
              <Button size="sm" disabled={addingIds.has(selected.id)} onClick={() => addToProcessing(selected)}>
                {addingIds.has(selected.id)
                  ? addProgress[selected.id]
                    ? `${Math.round(addProgress[selected.id].value * 100)}% — ${addProgress[selected.id].message || "Downloading…"}`
                    : "Adding…"
                  : "Add to Processing"}
              </Button>
              <div className="grow" />
              <select
                className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
              >
                {REJECT_REASON_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
              <Button
                size="sm"
                variant="destructive"
                disabled={busy}
                onClick={() =>
                  reviewAction(
                    () => bridge.rejectCandidate(selected.id, rejectReason),
                    "Rejected — removed from the pool too.",
                  )
                }
              >
                Reject / remove from pool
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
