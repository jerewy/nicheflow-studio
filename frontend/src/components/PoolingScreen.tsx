import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DashboardTable } from "@/components/DashboardTable";
import { useRevisitRefresh } from "@/hooks/useKeepAlive";
import { formatDate } from "@/lib/format";
import { bridge } from "@/lib/bridge";
import type {
  ApifyUsage,
  NicheAccount,
  PoolSource,
  PoolSourceClip,
  PoolingOverview,
  ScrapeToPoolResult,
} from "@/types";

const SCRAPE_TIMEOUT_MS = 600000;

// Poll a background scrape job until it finishes; resolve with its result or
// throw its error message. Mirrors ScrapingScreen's poller.
async function waitForScrapeJob(
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

export function PoolingScreen({
  activeAccountId,
  active = true,
}: {
  activeAccountId: number;
  // False while the screen is kept alive but hidden behind another (sub-)tab.
  active?: boolean;
}) {
  const [overview, setOverview] = useState<PoolingOverview | null>(null);
  const [niche, setNiche] = useState("history");
  const [sources, setSources] = useState<PoolSource[]>([]);
  const [source, setSource] = useState<string | null>(null);
  const [clips, setClips] = useState<PoolSourceClip[]>([]);
  const [showRemoved, setShowRemoved] = useState(false);
  const [selectedClip, setSelectedClip] = useState<PoolSourceClip | null>(null);
  const [nicheAccounts, setNicheAccounts] = useState<NicheAccount[]>([]);
  const [distributeSel, setDistributeSel] = useState<Set<number>>(new Set());
  const [manualTargets, setManualTargets] = useState<Record<number, number>>({});
  // Uniform "keep this many ready per account" for Auto-distribute. Blank falls
  // back to each account's own distribute_daily_target.
  const [autoTarget, setAutoTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [usage, setUsage] = useState<ApifyUsage | null>(null);
  const [newSourceUrl, setNewSourceUrl] = useState("");
  const [scrapeCount, setScrapeCount] = useState(30);
  const [scraping, setScraping] = useState(false);
  const [scrapeProgress, setScrapeProgress] = useState<{ value: number; message: string } | null>(
    null,
  );

  const openClip = (clip: PoolSourceClip) => {
    setSelectedClip(clip);
    setDistributeSel(new Set());
  };

  const load = useCallback(async () => {
    try {
      setError(null);
      setMessage(null);
      const [nextOverview, nextSources, accounts] = await Promise.all([
        bridge.poolingOverview(),
        bridge.listPoolSources(niche),
        bridge.poolNicheAccounts(niche),
      ]);
      setOverview(nextOverview);
      setSources(nextSources);
      setNicheAccounts(accounts);
      // Best-effort cost hint; a usage failure must not break the pool screen.
      bridge
        .apifyUsage()
        .then(setUsage)
        .catch(() => setUsage(null));
      setSource(null);
      setClips([]);
      setSelectedClip(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [niche]);

  useEffect(() => {
    const timer = window.setTimeout(load, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  // Kept alive behind the sub-tab bar; reload on revisit like a remount did.
  // `load` resets the source drill-down (same as the old remount) while any
  // in-flight distribute/scrape job keeps its progress state.
  useRevisitRefresh(active, () => void load());

  // Success banners read as transient alerts: clear them after a few seconds so
  // an old "distributed N" doesn't linger as if it just happened. Errors stay
  // until the next action so they aren't missed.
  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(() => setMessage(null), 8000);
    return () => window.clearTimeout(timer);
  }, [message]);

  const fetchClips = async (label: string, withRemoved: boolean) => {
    setClips(await bridge.listPoolSourceClips(niche, label, withRemoved));
  };

  const chooseSource = async (label: string) => {
    setSource(label);
    setMessage(null);
    try {
      await fetchClips(label, showRemoved);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const toggleRemoved = async () => {
    const next = !showRemoved;
    setShowRemoved(next);
    if (!source) return;
    try {
      await fetchClips(source, next);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  // Remove/restore a pool clip, then refresh the open source and close the
  // preview only on success so a failure stays visible.
  const runClipAction = async (fn: () => Promise<unknown>, note: string) => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await fn();
      if (source) await fetchClips(source, showRemoved);
      setMessage(note);
      setSelectedClip(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const toggleDistribute = (accountId: number, checked: boolean) => {
    setDistributeSel((prev) => {
      const next = new Set(prev);
      if (checked) next.add(accountId);
      else next.delete(accountId);
      return next;
    });
  };

  // Distribute the open clip to the checked accounts (idempotent; same-niche).
  const distribute = async () => {
    if (!selectedClip) return;
    const accountIds = [...distributeSel];
    if (accountIds.length === 0) {
      setError("Pick at least one account to distribute to.");
      return;
    }
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await bridge.distributePoolItem(selectedClip.pool_item_id, accountIds);
      setMessage(`Distributed to ${result.assigned} account(s).`);
      setDistributeSel(new Set());
      if (source) await fetchClips(source, showRemoved);
      setSelectedClip(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  // Engagement-rank the whole niche pool and spread the strongest undistributed
  // clips across its accounts (each clip to one account, volume-balanced).
  const autoDistribute = async () => {
    // Blank = each account's own distribute_daily_target. A number here is a
    // uniform TOTAL target, so an account holding 3 of 6 receives 3 more, not 6.
    const target = autoTarget.trim() === "" ? null : Number(autoTarget);
    if (target !== null && (!Number.isInteger(target) || target < 1)) {
      setError("Reels per account must be a whole number of at least 1.");
      return;
    }
    if (
      !window.confirm(
        `Auto-distribute the ${niche} pool?\n\nThis ranks undistributed clips by engagement ` +
          `(likes + recency) and assigns the strongest ones across this niche's accounts — ` +
          `each clip to a single account, balanced and capped per account.` +
          (target === null
            ? "\n\nEach account tops up to its own daily target."
            : `\n\nEach account tops up TO ${target} clip(s) total — one already holding ` +
              `${target - 1} receives 1 more, not ${target}.`),
      )
    )
      return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await bridge.distributeNiche(niche, target);
      // Refresh the pool/overview first — load() clears banners, so the
      // confirmation must be set afterwards or it would be wiped immediately.
      await load();
      if (result.assigned === 0) {
        const reasonMessages: Record<string, string> = {
          no_accounts: `No accounts are set to the "${niche}" niche — assign accounts first in Account settings.`,
          no_ready_accounts: `No "${niche}" accounts are currently ready to publish.`,
          all_at_cap:
            target === null
              ? "All accounts already hold their daily backlog target. Raise \"Reels per account\" to top them up further, or use Distribute now to add more on top."
              : `All accounts already hold ${target} clip(s) or more. Raise "Reels per account" to top them up further, or use Distribute now to add more on top.`,
          pool_empty: "Pool is fully distributed — no unused clips remain.",
        };
        setMessage(reasonMessages[result.reason ?? ""] ?? "Nothing to distribute.");
      } else {
        const breakdown = result.accounts
          .map(
            (account) =>
              `${account.account_name} (${account.count}/${account.target}` +
              `${account.pinned ? `, ${account.pinned} pinned` : ""})`,
          )
          .join(", ");
        setMessage(
          `✓ Distributed ${result.assigned} clip(s) — ${breakdown}. ` +
            "Footage downloads in the background.",
        );
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const manualDistribute = async () => {
    const targets = Object.fromEntries(
      Object.entries(manualTargets).filter(([, count]) => count > 0),
    );
    if (Object.keys(targets).length === 0) {
      setError("Enter a count for at least one account first.");
      return;
    }
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await bridge.distributeNicheExplicit(niche, targets);
      // Refresh first — load() clears banners — then reset the inputs and show
      // the confirmation so success is unmistakable.
      await load();
      setManualTargets({});
      if (result.assigned === 0) {
        setMessage(
          "No clips were distributed — this niche's pool has no unused clips left to add.",
        );
      } else {
        const breakdown = result.accounts
          .filter((account) => account.count > 0)
          .map((account) => `${account.account_name} (${account.count})`)
          .join(", ");
        setMessage(
          `✓ Distributed ${result.assigned} clip(s)${breakdown ? ` — ${breakdown}` : ""}. ` +
            "Find them in Processing (pending review); footage downloads in the background.",
        );
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const stats = overview?.niches.find((row) => row.niche === niche);
  // Total clips the "Distribute now" click would add — drives the button label
  // and disabled state so a zero-count click reads as intentionally inert
  // instead of a silent no-op.
  const manualTotal = Object.values(manualTargets).reduce(
    (sum, count) => sum + (count > 0 ? count : 0),
    0,
  );

  // Only "active" accounts can receive assignments; resting/flagged ones are
  // listed so they can be shown disabled instead of silently missing.
  const activeNicheAccounts = nicheAccounts.filter(
    (account) => account.operational_status === "active",
  );

  // Scrapes attach a Source to an account, and clips pool by that account's
  // niche — so the owner must be an active account IN the selected niche.
  // Prefer the globally-active account when it qualifies, else the first one.
  const scrapeOwner =
    activeNicheAccounts.find((account) => account.id === activeAccountId) ??
    activeNicheAccounts[0] ??
    null;

  // Add a source profile to the niche's owner account and immediately scrape it
  // into this niche's pool (idempotent: re-adding an existing handle just
  // refreshes it). Mirrors the Scraping tab so the two stay behaviourally in sync.
  const addAndScrape = async () => {
    const url = newSourceUrl.trim();
    if (!url || !scrapeOwner || scraping) return;
    if (
      usage?.over_free_tier &&
      !window.confirm(
        "You're over the Apify free tier this month — scraping may incur charges. Continue?",
      )
    )
      return;
    setScraping(true);
    setError(null);
    setMessage(null);
    setScrapeProgress({ value: 0, message: "Starting…" });
    try {
      const created = await bridge.addSource(scrapeOwner.id, url);
      const { job_id } = await bridge.startSourceScrape(created.id, Math.max(1, scrapeCount));
      const result = (await waitForScrapeJob(job_id, (value, scrapeMessage) =>
        setScrapeProgress({ value, message: scrapeMessage }),
      )) as ScrapeToPoolResult;
      setNewSourceUrl("");
      setMessage(
        `Scraped ${created.label} into ${scrapeOwner.name}: ${result.added} added, ` +
          `${result.duplicates} duplicate(s) of ${result.scraped} result(s).`,
      );
      await load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setScraping(false);
      setScrapeProgress(null);
    }
  };

  return (
    <section className="space-y-4 rounded-xl border bg-card p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Pool &amp; Distribute</h2>
          <p className="text-sm text-muted-foreground">Browse each niche pool by source, preview a clip, then keep the pool clean by removing or restoring it.</p>
        </div>
        <Button size="sm" variant="secondary" onClick={load}>Refresh</Button>
      </div>
      {error && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          <span className="grow">{error}</span>
          <button
            type="button"
            aria-label="Dismiss"
            className="shrink-0 leading-none text-destructive/70 hover:text-destructive"
            onClick={() => setError(null)}
          >
            ×
          </button>
        </div>
      )}
      {message && (
        <div
          role="status"
          className="flex items-start gap-2 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-600"
        >
          <span className="grow">{message}</span>
          <button
            type="button"
            aria-label="Dismiss"
            className="shrink-0 leading-none text-emerald-600/70 hover:text-emerald-600"
            onClick={() => setMessage(null)}
          >
            ×
          </button>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <label className="text-sm text-muted-foreground">Niche</label>
        <select className="h-9 rounded-md border border-input px-3 text-sm" value={niche} onChange={(e) => setNiche(e.target.value)}>
          <option value="history">History</option>
          <option value="movie">Movie</option>
        </select>
        {stats && <p className="text-sm text-muted-foreground">{stats.pooled} pooled · {stats.assigned} assigned · {stats.unused} unused · {stats.rejected} rejected</p>}
        <div className="grow" />
        <label className="text-sm text-muted-foreground" htmlFor="auto-target">
          Reels per account
        </label>
        <input
          id="auto-target"
          type="number"
          min={1}
          className="h-9 w-20 rounded-md border border-input px-2 text-sm"
          placeholder="auto"
          value={autoTarget}
          disabled={busy}
          onChange={(event) => setAutoTarget(event.target.value)}
          title="How many unposted clips to keep ready per account. Blank uses each account's own daily target. This is a TOTAL, so an account already holding some receives only the difference."
        />
        <Button
          size="sm"
          disabled={busy}
          onClick={autoDistribute}
          title="Rank the undistributed pool by engagement (likes + recency) and spread the strongest clips across this niche's accounts"
        >
          Auto-distribute (ranked)
        </Button>
      </div>
      <div className="space-y-2 rounded-md border border-border p-3">
        <div>
          <p className="text-sm font-medium">Distribute now</p>
          <p className="text-xs text-muted-foreground">
            Counts are ADDITIONAL clips per account (on top of what it already holds), bypassing
            the daily target. New clips appear in the Processing screen as pending review and their
            footage downloads in the background.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {nicheAccounts.map((account) => {
            const inactive = account.operational_status !== "active";
            return (
              <label
                key={account.id}
                className={`flex items-center gap-2 rounded-md border border-border px-2 py-1 text-xs ${inactive ? "opacity-60" : ""}`}
                title={
                  inactive
                    ? `${account.name} is ${account.operational_status} — set it to Active in Account settings to distribute to it.`
                    : undefined
                }
              >
                <input
                  type="checkbox"
                  disabled={inactive}
                  checked={(manualTargets[account.id] ?? 0) > 0}
                  onChange={(event) =>
                    setManualTargets((current) => ({
                      ...current,
                      [account.id]: event.target.checked ? Math.max(current[account.id] ?? 1, 1) : 0,
                    }))
                  }
                />
                {account.name}
                {inactive && (
                  <span className="text-[10px] uppercase text-amber-500">
                    {account.operational_status}
                  </span>
                )}
                <input
                  type="number"
                  min={0}
                  disabled={inactive}
                  className="h-7 w-16 rounded border border-input px-1"
                  value={manualTargets[account.id] ?? 0}
                  onChange={(event) =>
                    setManualTargets((current) => ({
                      ...current,
                      [account.id]: Math.max(0, Number(event.target.value) || 0),
                    }))
                  }
                />
              </label>
            );
          })}
        </div>
        <Button
          size="sm"
          variant="outline"
          disabled={busy || manualTotal === 0}
          title={manualTotal === 0 ? "Enter a count for at least one account first" : undefined}
          onClick={manualDistribute}
        >
          {manualTotal > 0 ? `Distribute now (${manualTotal})` : "Distribute now"}
        </Button>
      </div>
      <div className="space-y-2 rounded-md border border-border p-3">
        <div>
          <p className="text-sm font-medium">Add source &amp; scrape</p>
          <p className="text-xs text-muted-foreground">
            Paste an Instagram profile or hashtag URL to pull its recent posts into this niche&apos;s
            pool via Apify (deduped). The source is attached to{" "}
            {scrapeOwner ? <span className="font-medium">{scrapeOwner.name}</span> : "this niche"}.
            {usage ? ` Apify: ${usage.used} / ${usage.free_cap} used this month.` : ""}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            className="h-9 min-w-72 grow rounded-md border border-input bg-background px-3 text-sm text-foreground"
            placeholder="https://www.instagram.com/<handle>/ or /explore/tags/<tag>"
            value={newSourceUrl}
            disabled={scraping || !scrapeOwner}
            onChange={(event) => setNewSourceUrl(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void addAndScrape();
            }}
          />
          <label className="flex items-center gap-1 text-xs text-muted-foreground">
            Posts
            <input
              type="number"
              min={1}
              className="h-9 w-16 rounded-md border border-input bg-background px-2 text-sm text-foreground"
              value={scrapeCount}
              disabled={scraping}
              onChange={(event) => setScrapeCount(Math.max(1, Number(event.target.value) || 1))}
            />
          </label>
          <Button
            size="sm"
            disabled={scraping || !newSourceUrl.trim() || !scrapeOwner}
            title={!scrapeOwner ? "This niche has no accounts to attach a source to" : undefined}
            onClick={() => void addAndScrape()}
          >
            {scraping ? "Scraping…" : "Add & Scrape"}
          </Button>
        </div>
        {scrapeProgress && (
          <p className="text-xs text-muted-foreground">
            {Math.round(scrapeProgress.value * 100)}% · {scrapeProgress.message}
          </p>
        )}
      </div>
      <DashboardTable headers={["Source", "Clips", "Newest post"]}>
        {sources.map((row) => (
          <tr key={row.source_label} onClick={() => chooseSource(row.source_label)} className={`cursor-pointer border-t border-border hover:bg-accent ${source === row.source_label ? "bg-accent" : ""}`}>
            <td className="px-3 py-2">{row.source_label}</td>
            <td className="px-3 py-2 text-center">{row.clip_count}</td>
            <td className="px-3 py-2 text-center">{formatDate(row.newest_post_at)}</td>
          </tr>
        ))}
      </DashboardTable>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">{source ? `${source}: ${clips.length} clip(s)` : "Select a source above to see its clips."}</p>
        {source && (
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            <input type="checkbox" checked={showRemoved} onChange={toggleRemoved} />
            Show removed
          </label>
        )}
      </div>
      <DashboardTable headers={["Clip", "Caption", "Likes", "Score", "Published", "Status", "Distribution", "Actions"]}>
        {clips.map((clip) => {
          const removed = clip.acceptance_status === "removed";
          return (
            <tr
              key={clip.pool_item_id}
              onClick={() => openClip(clip)}
              className={`cursor-pointer border-t border-border hover:bg-accent ${removed ? "opacity-60" : ""}`}
            >
              <td className="px-3 py-2 font-mono text-xs">
                {clip.shortcode ?? "—"}
                {removed && (
                  <span className="ml-2 rounded bg-muted px-1 py-0.5 text-[10px] uppercase tracking-wide">
                    removed
                  </span>
                )}
              </td>
              <td className="max-w-md truncate px-3 py-2">{clip.caption ?? "—"}</td>
              <td className="px-3 py-2 text-center">{clip.like_count?.toLocaleString() ?? "—"}</td>
              <td
                className="px-3 py-2 text-center font-mono text-xs"
                title="Engagement score: log-damped likes + recency (drives the ranking)"
              >
                {clip.score != null ? clip.score.toFixed(2) : "—"}
              </td>
              <td className="px-3 py-2 text-center">{formatDate(clip.published_at)}</td>
              <td className="px-3 py-2 text-center">{clip.download_status}</td>
              <td className="px-3 py-2">{clip.distributed_to.join(", ") || "Not distributed"}</td>
              <td className="px-3 py-2 text-right" onClick={(e) => e.stopPropagation()}>
                {removed ? (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy}
                    onClick={() =>
                      runClipAction(
                        () => bridge.restorePoolItem(clip.pool_item_id),
                        "Restored to the pool.",
                      )
                    }
                  >
                    Restore
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() =>
                      runClipAction(
                        () => bridge.removePoolItem(clip.pool_item_id),
                        "Removed from the pool.",
                      )
                    }
                  >
                    Remove
                  </Button>
                )}
              </td>
            </tr>
          );
        })}
      </DashboardTable>

      {selectedClip && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setSelectedClip(null)}
        >
          <div
            className="max-h-[85vh] w-full max-w-3xl overflow-auto rounded-xl border border-border bg-card p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-xs uppercase tracking-wider text-muted-foreground">
                  Pool clip · {selectedClip.shortcode ?? `#${selectedClip.pool_item_id}`}
                </p>
                <div className="mt-1 flex items-center gap-2">
                  <Badge variant={selectedClip.acceptance_status === "removed" ? "secondary" : "outline"}>
                    {selectedClip.acceptance_status}
                  </Badge>
                  <span className="text-sm text-muted-foreground">{selectedClip.download_status}</span>
                </div>
              </div>
              <Button size="sm" variant="ghost" onClick={() => setSelectedClip(null)}>
                Close
              </Button>
            </div>

            <div className="mt-4 grid gap-4 sm:grid-cols-[240px_1fr]">
              <div className="flex aspect-[9/16] items-center justify-center overflow-hidden rounded-lg border border-border bg-black">
                {selectedClip.preview_url ? (
                  <video
                    key={selectedClip.preview_url}
                    className="h-full w-full object-contain"
                    src={selectedClip.preview_url}
                    controls
                    preload="metadata"
                  />
                ) : (
                  <p className="px-3 text-center text-xs text-muted-foreground">
                    No local video — the original isn't downloaded yet (status:{" "}
                    {selectedClip.download_status}).
                  </p>
                )}
              </div>
              <div className="space-y-3 text-sm">
                <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
                  <div>
                    <dt className="text-muted-foreground">Likes</dt>
                    <dd>{selectedClip.like_count?.toLocaleString() ?? "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Engagement score</dt>
                    <dd>{selectedClip.score != null ? selectedClip.score.toFixed(2) : "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Published</dt>
                    <dd>{formatDate(selectedClip.published_at)}</dd>
                  </div>
                </dl>
                <div>
                  <p className="text-xs text-muted-foreground">Distribution</p>
                  <p>{selectedClip.distributed_to.join(", ") || "Not distributed"}</p>
                </div>
                {selectedClip.caption && (
                  <p className="max-h-40 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-muted/30 p-3 text-xs text-muted-foreground">
                    {selectedClip.caption}
                  </p>
                )}
                {selectedClip.source_url && (
                  <a
                    href={selectedClip.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="block truncate text-xs text-sky-500 hover:underline"
                    title={selectedClip.source_url}
                  >
                    {selectedClip.source_url}
                  </a>
                )}
              </div>
            </div>

            {selectedClip.acceptance_status !== "removed" && nicheAccounts.length > 0 && (
              <div className="mt-4 border-t border-border pt-3">
                <p className="mb-2 text-xs text-muted-foreground">
                  Distribute to accounts (checked = will receive this clip)
                </p>
                <div className="flex flex-wrap gap-2">
                  {nicheAccounts.map((account) => {
                    const already = selectedClip.distributed_to.includes(account.name);
                    const inactive = account.operational_status !== "active";
                    return (
                      <label
                        key={account.id}
                        className={`flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs ${
                          already || inactive ? "opacity-60" : "cursor-pointer hover:bg-accent"
                        }`}
                        title={
                          inactive
                            ? `${account.name} is ${account.operational_status} — set it to Active in Account settings to distribute to it.`
                            : undefined
                        }
                      >
                        <input
                          type="checkbox"
                          checked={already || distributeSel.has(account.id)}
                          disabled={already || inactive || busy}
                          onChange={(event) => toggleDistribute(account.id, event.target.checked)}
                        />
                        {account.name}
                        {already && <span className="text-[10px] text-emerald-500">✓ has it</span>}
                        {!already && inactive && (
                          <span className="text-[10px] uppercase text-amber-500">
                            {account.operational_status}
                          </span>
                        )}
                      </label>
                    );
                  })}
                </div>
              </div>
            )}

            {error && <p className="mt-3 text-sm text-destructive">{error}</p>}

            <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-border pt-4">
              {selectedClip.acceptance_status !== "removed" && (
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={busy || distributeSel.size === 0}
                  onClick={distribute}
                >
                  {distributeSel.size > 0 ? `Distribute (${distributeSel.size})` : "Distribute"}
                </Button>
              )}
              <div className="grow" />
              {selectedClip.acceptance_status === "removed" ? (
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() =>
                    runClipAction(
                      () => bridge.restorePoolItem(selectedClip.pool_item_id),
                      "Added back to the pool.",
                    )
                  }
                >
                  Add back to pool
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="destructive"
                  disabled={busy}
                  onClick={() =>
                    runClipAction(
                      () => bridge.removePoolItem(selectedClip.pool_item_id),
                      "Removed from the pool.",
                    )
                  }
                >
                  Remove from pool
                </Button>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
