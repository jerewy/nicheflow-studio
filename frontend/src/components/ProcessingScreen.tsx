import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { OptionCard } from "@/components/OptionCard";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { bridge } from "@/lib/bridge";
import type { DraftRevision, ProcessingContext } from "@/types";

const POLL_INTERVAL_MS = 4000;
const JOB_POLL_INTERVAL_MS = 1000;
const JOB_TIMEOUT_MS = 180000;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// Poll a background job until it finishes; resolve with its result or throw the
// job's error message.
async function waitForJob(jobId: string): Promise<unknown> {
  const deadline = Date.now() + JOB_TIMEOUT_MS;
  for (;;) {
    const snapshot = await bridge.getJob(jobId);
    if (snapshot.status === "succeeded") return snapshot.result;
    if (snapshot.status === "failed") {
      throw new Error(snapshot.error ?? "Generation failed.");
    }
    if (Date.now() > deadline) throw new Error("Generation timed out.");
    await sleep(JOB_POLL_INTERVAL_MS);
  }
}

interface EditableOptions {
  titles: string[];
  captions: string[];
}

function toEditable(revision: DraftRevision | null): EditableOptions {
  if (!revision) return { titles: [], captions: [] };
  return {
    titles: [...revision.title_options],
    captions: [...revision.caption_options],
  };
}

function sameOptions(a: EditableOptions, revision: DraftRevision | null): boolean {
  if (!revision) return a.titles.length === 0 && a.captions.length === 0;
  return (
    JSON.stringify(a.titles) === JSON.stringify(revision.title_options) &&
    JSON.stringify(a.captions) === JSON.stringify(revision.caption_options)
  );
}

export function ProcessingScreen() {
  const [context, setContext] = useState<ProcessingContext | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [canGenerate, setCanGenerate] = useState(false);

  // The revision currently loaded into the editor, and the user's edits on top.
  const [loadedRevision, setLoadedRevision] = useState<DraftRevision | null>(null);
  const [edits, setEdits] = useState<EditableOptions>({ titles: [], captions: [] });
  // A newer revision detected while the user has unsaved edits (dirty-edit guard).
  const [pendingRevision, setPendingRevision] = useState<DraftRevision | null>(null);

  const itemId = context?.item.id ?? null;
  const dirty = useMemo(
    () => loadedRevision !== null && !sameOptions(edits, loadedRevision),
    [edits, loadedRevision],
  );
  // Keep the latest dirty flag readable inside the polling closure without
  // resubscribing the interval on every keystroke.
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const loadedRef = useRef<DraftRevision | null>(loadedRevision);
  loadedRef.current = loadedRevision;

  const loadRevisionIntoEditor = useCallback((revision: DraftRevision | null) => {
    setLoadedRevision(revision);
    setEdits(toEditable(revision));
    setPendingRevision(null);
  }, []);

  // Initial context load.
  useEffect(() => {
    let cancelled = false;
    bridge
      .getContext()
      .then((ctx) => {
        if (cancelled) return;
        setContext(ctx);
        loadRevisionIntoEditor(ctx.latest_revision);
        // Tell the backend which item is active so the Codex CLI `current`
        // command follows this window's selection. Best-effort.
        void bridge.setActiveItem(ctx.item.id).catch(() => undefined);
      })
      .catch((err: unknown) =>
        setLoadError(err instanceof Error ? err.message : String(err)),
      );
    return () => {
      cancelled = true;
    };
  }, [loadRevisionIntoEditor]);

  // Whether a draft provider is configured (enables the Generate button).
  useEffect(() => {
    bridge.canGenerate().then(setCanGenerate).catch(() => setCanGenerate(false));
  }, []);

  // Poll for newer revisions (Codex writes, or another window's edits).
  useEffect(() => {
    if (itemId === null) return;
    const timer = window.setInterval(async () => {
      try {
        const latest = await bridge.getLatestRevision(itemId);
        if (!latest) return;
        const loaded = loadedRef.current;
        if (loaded && latest.revision_number <= loaded.revision_number) return;
        if (dirtyRef.current) {
          setPendingRevision(latest);
        } else {
          loadRevisionIntoEditor(latest);
        }
      } catch {
        // Transient bridge errors during polling are non-fatal; try again next tick.
      }
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [itemId, loadRevisionIntoEditor]);

  const updateTitle = (index: number, value: string) =>
    setEdits((prev) => {
      const titles = [...prev.titles];
      titles[index] = value;
      return { ...prev, titles };
    });

  const updateCaption = (index: number, value: string) =>
    setEdits((prev) => {
      const captions = [...prev.captions];
      captions[index] = value;
      return { ...prev, captions };
    });

  const generate = async () => {
    if (itemId === null) return;
    setGenerating(true);
    setActionError(null);
    try {
      const { job_id } = await bridge.startGeneration(itemId, {});
      const revision = (await waitForJob(job_id)) as DraftRevision;
      // Respect dirty-edit protection: don't clobber unsaved edits.
      if (dirtyRef.current) {
        setPendingRevision(revision);
      } else {
        loadRevisionIntoEditor(revision);
      }
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setGenerating(false);
    }
  };

  const apply = async (optionNumber: number) => {
    if (itemId === null) return;
    setBusy(true);
    setActionError(null);
    try {
      await bridge.applyRevision(itemId, optionNumber, loadedRevision?.id ?? null);
      const ctx = await bridge.getContext(itemId);
      setContext(ctx);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const saveEdits = async () => {
    if (itemId === null) return;
    setBusy(true);
    setActionError(null);
    try {
      const saved = await bridge.saveRevision(itemId, {
        title_options: edits.titles,
        caption_options: edits.captions,
        summary: loadedRevision?.summary ?? null,
        option_notes: loadedRevision?.option_notes ?? null,
        option_tiers: loadedRevision?.option_tiers ?? null,
        source: "ui",
      });
      loadRevisionIntoEditor(saved);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (loadError) {
    return (
      <div className="mx-auto max-w-2xl p-8">
        <Card>
          <CardHeader>
            <CardTitle>Could not load Processing</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">{loadError}</CardContent>
        </Card>
      </div>
    );
  }

  if (!context) {
    return <div className="p-8 text-sm text-muted-foreground">Loading…</div>;
  }

  const { item, account } = context;
  const appliedIndex = loadedRevision?.applied_title_index ?? null;

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-6">
      <header className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-semibold tracking-tight">Processing</h1>
            {!bridge.available() && <Badge variant="outline">browser preview</Badge>}
            {loadedRevision && (
              <Badge variant="secondary">revision {loadedRevision.revision_number}</Badge>
            )}
            {dirty && <Badge variant="destructive">unsaved edits</Badge>}
          </div>
          <p className="text-sm text-muted-foreground">
            {item.title ?? item.source_url}
            {account?.niche_label ? ` — ${account.niche_label}` : ""}
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <Button
            onClick={generate}
            disabled={generating || busy || !canGenerate}
            title={canGenerate ? undefined : "No draft provider configured (set GROQ_API_KEY)"}
          >
            {generating ? "Generating…" : "Generate options"}
          </Button>
          {!canGenerate && (
            <span className="text-xs text-muted-foreground">no provider configured</span>
          )}
        </div>
      </header>

      {pendingRevision && (
        <Card className="border-amber-500/50 bg-amber-500/5">
          <CardContent className="flex flex-wrap items-center justify-between gap-2 p-4">
            <span className="text-sm">
              A newer revision (#{pendingRevision.revision_number}) is available. You
              have unsaved edits.
            </span>
            <div className="flex gap-2">
              <Button size="sm" onClick={() => loadRevisionIntoEditor(pendingRevision)}>
                Review update
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setPendingRevision(null)}
              >
                Keep my edits
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {actionError && (
        <p className="text-sm text-destructive">{actionError}</p>
      )}

      {loadedRevision?.summary && (
        <p className="text-sm text-muted-foreground">{loadedRevision.summary}</p>
      )}

      {!loadedRevision ? (
        <Card>
          <CardContent className="p-6 text-sm text-muted-foreground">
            No draft revisions yet. Ask Codex to generate options (or run{" "}
            <code>scripts/nicheflow_drafts.py save</code>) and they will appear here
            automatically.
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {edits.titles.map((title, index) => (
            <OptionCard
              key={index}
              optionNumber={index + 1}
              title={title}
              caption={edits.captions[index] ?? ""}
              note={loadedRevision.option_notes[index]}
              tier={loadedRevision.option_tiers[index]}
              recommended={loadedRevision.recommended_title_index === index + 1}
              applied={appliedIndex === index + 1}
              busy={busy}
              onTitleChange={(v) => updateTitle(index, v)}
              onCaptionChange={(v) => updateCaption(index, v)}
              onApply={() => apply(index + 1)}
            />
          ))}
        </div>
      )}

      {loadedRevision && (
        <footer className="flex items-center gap-2">
          <Button onClick={saveEdits} disabled={!dirty || busy}>
            Save edits as new revision
          </Button>
          {dirty && (
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => loadRevisionIntoEditor(loadedRevision)}
            >
              Discard edits
            </Button>
          )}
        </footer>
      )}
    </div>
  );
}
