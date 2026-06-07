import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { bridge } from "@/lib/bridge";
import type { ScrapeCandidate, SourceProfile } from "@/types";

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

function shortDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString();
}

export function ScrapingScreen({ activeAccountId, activeAccountName }: ScrapingScreenProps) {
  const [sources, setSources] = useState<SourceProfile[]>([]);
  const [candidates, setCandidates] = useState<ScrapeCandidate[]>([]);
  const [filter, setFilter] = useState("candidate");
  const [newUrl, setNewUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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

  useEffect(() => {
    loadSources();
  }, [loadSources]);
  useEffect(() => {
    loadCandidates();
  }, [loadCandidates]);

  const run = async (fn: () => Promise<unknown>, note: string, reload: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await fn();
      await reload();
      setMessage(note);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const addSource = () => {
    if (!newUrl.trim()) return;
    void run(
      async () => {
        await bridge.addSource(activeAccountId, newUrl.trim());
        setNewUrl("");
      },
      "Source added.",
      loadSources,
    );
  };

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Scraping</h1>
        <p className="text-sm text-muted-foreground">
          Sources and candidates for {activeAccountName ?? "this account"}. Running the actual
          scrape, queuing downloads, and accepting into the pool happen in the desktop app.
        </p>
      </div>

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
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <CardTitle className="text-base">Candidates</CardTitle>
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
            <div className="overflow-auto rounded-md border border-border">
              <table className="w-full text-left text-sm">
                <thead className="bg-muted text-xs text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 font-medium">State</th>
                    <th className="px-3 py-2 font-medium">Title</th>
                    <th className="px-3 py-2 font-medium">Source</th>
                    <th className="px-3 py-2 text-right font-medium">Likes</th>
                    <th className="px-3 py-2 font-medium">Published</th>
                    <th className="px-3 py-2 font-medium">Review</th>
                  </tr>
                </thead>
                <tbody>
                  {candidates.map((c) => (
                    <tr key={c.id} className="border-t border-border">
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
                      <td className="px-3 py-2">
                        {c.state === "ignored" ? (
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busy}
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
                            disabled={busy}
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
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="mt-2 text-xs text-muted-foreground">
            Queue for download and accept into the niche pool from the desktop app.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
