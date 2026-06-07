import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { bridge } from "@/lib/bridge";
import type { NichePool, PoolClip, PoolingOverview } from "@/types";

export function PoolingScreen() {
  const [overview, setOverview] = useState<PoolingOverview | null>(null);
  const [selectedNiche, setSelectedNiche] = useState<string | null>(null);
  const [clips, setClips] = useState<PoolClip[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loadingClips, setLoadingClips] = useState(false);

  const load = useCallback(async () => {
    try {
      setOverview(await bridge.poolingOverview());
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const selectNiche = async (niche: string) => {
    setSelectedNiche(niche);
    setLoadingClips(true);
    setError(null);
    try {
      setClips(await bridge.listPoolItems(niche));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingClips(false);
    }
  };

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Pool &amp; Distribute</h1>
        <Button size="sm" variant="secondary" onClick={load}>
          Refresh
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        Read-only overview of the shared niche pools. Accepting, pruning, and distributing clips
        still happen in the desktop app and the pool-admin scripts.
      </p>

      {error && <p className="text-sm text-destructive">{error}</p>}

      <div className="grid gap-4 md:grid-cols-2">
        {overview?.niches.map((pool) => (
          <NicheCard
            key={pool.niche}
            pool={pool}
            selected={selectedNiche === pool.niche}
            onSelect={() => selectNiche(pool.niche)}
          />
        ))}
      </div>

      {selectedNiche && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base capitalize">{selectedNiche} pool clips</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {loadingClips ? (
              <p className="text-sm text-muted-foreground">Loading…</p>
            ) : clips.length === 0 ? (
              <p className="text-sm text-muted-foreground">No accepted clips in this pool.</p>
            ) : (
              clips.map((clip) => (
                <div
                  key={clip.pool_item_id}
                  className="flex flex-wrap items-center gap-2 border-b border-border pb-2 text-sm last:border-0"
                >
                  <span className="font-mono text-xs">{clip.clip_label}</span>
                  <span className="text-muted-foreground">· {clip.source_label}</span>
                  {clip.is_distributed ? (
                    <Badge variant="secondary">{clip.distributed_to.join(", ")}</Badge>
                  ) : (
                    <Badge variant="outline">unused</Badge>
                  )}
                </div>
              ))
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function NicheCard({
  pool,
  selected,
  onSelect,
}: {
  pool: NichePool;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <Card className={cn(selected && "ring-2 ring-ring")}>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <CardTitle className="text-base capitalize">{pool.niche}</CardTitle>
        <Button size="sm" variant="outline" onClick={onSelect}>
          View clips
        </Button>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-4 gap-2 text-center">
          <Stat label="Pooled" value={pool.pooled} />
          <Stat label="Assigned" value={pool.assigned} />
          <Stat label="Unused" value={pool.unused} />
          <Stat label="Rejected" value={pool.rejected} />
        </div>
        <div>
          <p className="mb-1 text-xs font-medium text-muted-foreground">By account</p>
          {pool.assignments_by_account.length === 0 ? (
            <p className="text-xs text-muted-foreground">No assignments yet.</p>
          ) : (
            <ul className="space-y-0.5 text-sm">
              {pool.assignments_by_account.map((row) => (
                <li key={row.account_id} className="flex justify-between">
                  <span className="truncate">{row.account_name}</span>
                  <span className="text-muted-foreground">{row.count}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border border-border py-2">
      <div className="text-lg font-semibold">{value}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  );
}
