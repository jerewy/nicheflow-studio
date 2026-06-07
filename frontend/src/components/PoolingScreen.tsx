import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { DashboardTable } from "@/components/DashboardTable";
import { formatDate } from "@/lib/format";
import { bridge } from "@/lib/bridge";
import type { PoolSource, PoolSourceClip, PoolingOverview } from "@/types";

export function PoolingScreen() {
  const [overview, setOverview] = useState<PoolingOverview | null>(null);
  const [niche, setNiche] = useState("history");
  const [sources, setSources] = useState<PoolSource[]>([]);
  const [source, setSource] = useState<string | null>(null);
  const [clips, setClips] = useState<PoolSourceClip[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setError(null);
      const [nextOverview, nextSources] = await Promise.all([
        bridge.poolingOverview(),
        bridge.listPoolSources(niche),
      ]);
      setOverview(nextOverview);
      setSources(nextSources);
      setSource(null);
      setClips([]);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [niche]);

  useEffect(() => {
    const timer = window.setTimeout(load, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const chooseSource = async (label: string) => {
    setSource(label);
    try {
      setClips(await bridge.listPoolSourceClips(niche, label));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const stats = overview?.niches.find((row) => row.niche === niche);

  return (
    <section className="space-y-4 rounded-xl border bg-card p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Pool &amp; Distribute</h2>
          <p className="text-sm text-muted-foreground">Browse each niche pool by source, then inspect distribution per clip.</p>
        </div>
        <Button size="sm" variant="secondary" onClick={load}>Refresh</Button>
      </div>
      {error && <p className="text-sm text-destructive">{error}</p>}
      <div className="flex flex-wrap items-center gap-3">
        <label className="text-sm text-muted-foreground">Niche</label>
        <select className="h-9 rounded-md border border-input px-3 text-sm" value={niche} onChange={(e) => setNiche(e.target.value)}>
          <option value="history">History</option>
          <option value="movie">Movie</option>
        </select>
        {stats && <p className="text-sm text-muted-foreground">{stats.pooled} pooled · {stats.assigned} assigned · {stats.unused} unused · {stats.rejected} rejected</p>}
      </div>
      <DashboardTable headers={["Source", "Clips", "Newest post"]}>
        {sources.map((row) => (
          <tr key={row.source_label} onClick={() => chooseSource(row.source_label)} className="cursor-pointer border-t border-border hover:bg-accent">
            <td className="px-3 py-2">{row.source_label}</td>
            <td className="px-3 py-2 text-center">{row.clip_count}</td>
            <td className="px-3 py-2 text-center">{formatDate(row.newest_post_at)}</td>
          </tr>
        ))}
      </DashboardTable>
      <p className="text-sm text-muted-foreground">{source ? `${source}: ${clips.length} clip(s)` : "Select a source above to see its clips."}</p>
      <DashboardTable headers={["Clip", "Caption", "Likes", "Published", "Status", "Distribution"]}>
        {clips.map((clip) => (
          <tr key={clip.pool_item_id} className="border-t border-border">
            <td className="px-3 py-2 font-mono text-xs">{clip.shortcode ?? "—"}</td>
            <td className="max-w-md truncate px-3 py-2">{clip.caption ?? "—"}</td>
            <td className="px-3 py-2 text-center">{clip.like_count?.toLocaleString() ?? "—"}</td>
            <td className="px-3 py-2 text-center">{formatDate(clip.published_at)}</td>
            <td className="px-3 py-2 text-center">{clip.download_status}</td>
            <td className="px-3 py-2">{clip.distributed_to.join(", ") || "Not distributed"}</td>
          </tr>
        ))}
      </DashboardTable>
    </section>
  );
}
