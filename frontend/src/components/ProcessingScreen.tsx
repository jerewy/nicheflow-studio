import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { OptionCard } from "@/components/OptionCard";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { bridge, whenBridgeReady } from "@/lib/bridge";
import type {
  DraftRevision,
  ExportResult,
  LibraryItem,
  ProcessingContext,
  PublishJob,
  WorkflowSettings,
} from "@/types";

const POLL_INTERVAL_MS = 4000;
const JOB_POLL_INTERVAL_MS = 1000;
const JOB_TIMEOUT_MS = 180000;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// Workflow status -> label + colored dot (Tailwind bg class) for the videos table.
const STATUS_META: Record<string, { label: string; dot: string }> = {
  new: { label: "New", dot: "bg-sky-500" },
  draft: { label: "Draft", dot: "bg-amber-500" },
  exported: { label: "Exported", dot: "bg-violet-500" },
  posted: { label: "Posted", dot: "bg-emerald-500" },
  skipped: { label: "Skipped", dot: "bg-zinc-500" },
};

function statusMeta(status: string): { label: string; dot: string } {
  return STATUS_META[status] ?? { label: status, dot: "bg-zinc-400" };
}

function shortDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString();
}

interface ProcessingScreenProps {
  activeAccountId: number;
  activeAccountName: string | null;
}

function workflowPayload(workflow: WorkflowSettings | null): Record<string, unknown> {
  return {
    clip_premise: workflow?.clip_premise ?? "",
    caption_style: workflow?.caption_style ?? "",
    title_style: workflow?.title_style ?? "",
    template: workflow?.template ?? "",
  };
}

// Poll a background job until it finishes; resolve with its result or throw the
// job's error message. Reports progress on each poll.
async function waitForJob(
  jobId: string,
  onProgress?: (progress: number, message: string) => void,
): Promise<unknown> {
  const deadline = Date.now() + JOB_TIMEOUT_MS;
  for (;;) {
    const snapshot = await bridge.getJob(jobId);
    onProgress?.(snapshot.progress, snapshot.message);
    if (snapshot.status === "succeeded") return snapshot.result;
    if (snapshot.status === "failed") {
      throw new Error(snapshot.error ?? "The job failed.");
    }
    if (Date.now() > deadline) throw new Error("The job timed out.");
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

export function ProcessingScreen({ activeAccountId, activeAccountName }: ProcessingScreenProps) {
  const [context, setContext] = useState<ProcessingContext | null>(null);
  const [itemsLoaded, setItemsLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [canGenerate, setCanGenerate] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportProgress, setExportProgress] = useState<{ value: number; message: string } | null>(
    null,
  );
  const [exportedPath, setExportedPath] = useState<string | null>(null);
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [publishJobs, setPublishJobs] = useState<PublishJob[]>([]);
  const [scheduleAt, setScheduleAt] = useState("");
  const [publishMessage, setPublishMessage] = useState<string | null>(null);
  const [isDesktop, setIsDesktop] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [itemSearch, setItemSearch] = useState("");
  const [itemFilter, setItemFilter] = useState("all");
  const [handoffMessage, setHandoffMessage] = useState<string | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowSettings | null>(null);
  const [finalTitle, setFinalTitle] = useState("");
  const [finalCaption, setFinalCaption] = useState("");
  const [previewMode, setPreviewMode] = useState<"original" | "exported">("original");

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
  const loadedRef = useRef<DraftRevision | null>(loadedRevision);

  useEffect(() => {
    dirtyRef.current = dirty;
    loadedRef.current = loadedRevision;
  }, [dirty, loadedRevision]);

  useEffect(() => {
    if (!actionError) return;
    const timer = window.setTimeout(() => setActionError(null), 8000);
    return () => window.clearTimeout(timer);
  }, [actionError]);

  const loadRevisionIntoEditor = useCallback((revision: DraftRevision | null) => {
    setLoadedRevision(revision);
    setEdits(toEditable(revision));
    setPendingRevision(null);
  }, []);

  const refreshPublishJobs = useCallback((id: number) => {
    bridge
      .listPublishJobs(id)
      .then(setPublishJobs)
      .catch(() => setPublishJobs([]));
  }, []);

  const applyContext = useCallback(
    (ctx: ProcessingContext) => {
      setContext(ctx);
      loadRevisionIntoEditor(ctx.latest_revision);
      setExportedPath(null);
      setPublishMessage(null);
      setPreviewError(null);
      setPreviewMode(ctx.item.exported_preview_url ? "exported" : "original");
      setHandoffMessage(null);
      refreshPublishJobs(ctx.item.id);
      bridge
        .getWorkflowSettings(ctx.item.id)
        .then((settings) => {
          setWorkflow(settings);
          setFinalTitle(settings.title_draft);
          setFinalCaption(settings.caption_draft);
        })
        .catch(() => setWorkflow(null));
      // Tell the backend which item is active so the Codex CLI `current`
      // command follows this window's selection. Best-effort.
      void bridge.setActiveItem(ctx.item.id).catch(() => undefined);
    },
    [loadRevisionIntoEditor, refreshPublishJobs],
  );

  // Load the active account's videos, then open the first one. Re-runs when the
  // active niche account changes.
  useEffect(() => {
    let cancelled = false;
    whenBridgeReady().then(async (ready) => {
      if (cancelled) return;
      setIsDesktop(ready);
      bridge
        .canGenerate()
        .then(setCanGenerate)
        .catch(() => setCanGenerate(false));
      try {
        const list = await bridge.listLibraryItems(activeAccountId);
        if (cancelled) return;
        setItems(list);
        setItemsLoaded(true);
        if (list.length > 0) {
          const ctx = await bridge.getContext(list[0].id);
          if (!cancelled) applyContext(ctx);
        } else {
          setContext(null);
        }
      } catch (err: unknown) {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : String(err));
      }
    });
    return () => {
      cancelled = true;
    };
  }, [applyContext, activeAccountId]);

  const switchItem = async (id: number) => {
    if (id === itemId) return;
    setActionError(null);
    try {
      const ctx = await bridge.getContext(id);
      applyContext(ctx);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  };

  const queueForPublish = async (scheduled: string | null) => {
    if (itemId === null) return;
    setBusy(true);
    setActionError(null);
    setPublishMessage(null);
    try {
      const result = await bridge.queueForPublish(itemId, scheduled);
      setPublishMessage(
        `${result.created ? "Added to" : "Updated in"} publish queue as ${result.status}.`,
      );
      refreshPublishJobs(itemId);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const autoScheduleForPublish = async () => {
    if (itemId === null) return;
    setBusy(true);
    setActionError(null);
    setPublishMessage(null);
    try {
      const result = await bridge.autoScheduleForPublish(itemId);
      const scheduled = result.scheduled_at
        ? new Date(result.scheduled_at).toLocaleString()
        : "the next open slot";
      setPublishMessage(`Auto-scheduled for ${scheduled}.`);
      refreshPublishJobs(itemId);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

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
      const { job_id } = await bridge.startGeneration(itemId, {
        clip_premise: workflow?.clip_premise ?? "",
        caption_style: workflow?.caption_style ?? null,
        title_style: workflow?.title_style || null,
      });
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

  const copyChatPrompt = async () => {
    if (itemId === null) return;
    setActionError(null);
    setHandoffMessage(null);
    try {
      const { prompt } = await bridge.buildChatPrompt(itemId, workflowPayload(workflow));
      await navigator.clipboard.writeText(prompt);
      setHandoffMessage("Chat prompt copied. Paste it into ChatGPT, Claude, Codex, or Claude Code.");
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  };

  const saveWorkflow = async () => {
    if (itemId === null || !workflow) return;
    setBusy(true);
    setActionError(null);
    try {
      setWorkflow(await bridge.saveWorkflowSettings(itemId, workflowPayload(workflow)));
      setHandoffMessage("Workflow settings saved.");
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const saveFinalDraft = async () => {
    if (itemId === null) return;
    setBusy(true);
    setActionError(null);
    try {
      await bridge.saveFinalDraft(itemId, finalTitle, finalCaption);
      const ctx = await bridge.getContext(itemId);
      setContext(ctx);
      setPreviewMode("exported");
      setHandoffMessage("Selected title and caption saved.");
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const pasteDraft = async () => {
    if (itemId === null) return;
    setBusy(true);
    setActionError(null);
    setHandoffMessage(null);
    try {
      const text = await navigator.clipboard.readText();
      if (!text.trim()) throw new Error("Clipboard is empty. Copy the generated draft first.");
      const revision = await bridge.importPastedDraft(itemId, text);
      loadRevisionIntoEditor(revision);
      setHandoffMessage(`Imported clipboard draft as revision ${revision.revision_number}.`);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const exportReel = async () => {
    if (itemId === null) return;
    setExporting(true);
    setActionError(null);
    setExportedPath(null);
    setExportProgress({ value: 0, message: "Starting…" });
    try {
      const { job_id } = await bridge.startExport(itemId);
      const result = (await waitForJob(job_id, (value, message) =>
        setExportProgress({ value, message }),
      )) as ExportResult;
      setExportedPath(result.processed_path);
      // Refetch so the item's processed_path is reflected, and refresh queue/list.
      const ctx = await bridge.getContext(itemId);
      setContext(ctx);
      refreshPublishJobs(itemId);
      bridge.listLibraryItems(activeAccountId).then(setItems).catch(() => undefined);
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setExporting(false);
      setExportProgress(null);
    }
  };

  const apply = async (optionNumber: number) => {
    if (itemId === null) return;
    setBusy(true);
    setActionError(null);
    try {
      const applied = await bridge.applyRevision(itemId, optionNumber, loadedRevision?.id ?? null);
      setFinalTitle(applied.title_draft);
      setFinalCaption(applied.caption_draft);
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
    if (itemsLoaded && items.length === 0) {
      return (
        <div className="mx-auto max-w-2xl p-8">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">No videos for this account</CardTitle>
            </CardHeader>
            <CardContent className="text-sm text-muted-foreground">
              {activeAccountName ? `"${activeAccountName}" has` : "This account has"} no downloaded
              clips yet. Scrape or import clips in the desktop app (and assign them to this
              account), then they'll appear here.
            </CardContent>
          </Card>
        </div>
      );
    }
    return <div className="p-8 text-sm text-muted-foreground">Loading…</div>;
  }

  const { item, account } = context;
  const appliedIndex = loadedRevision?.applied_title_index ?? null;
  const previewUrl =
    previewMode === "exported" && item.exported_preview_url
      ? item.exported_preview_url
      : item.original_preview_url;
  const filteredItems = items.filter((candidate) => {
    const matchesStatus = itemFilter === "all" || candidate.status === itemFilter;
    const search = itemSearch.trim().toLowerCase();
    const matchesSearch =
      !search ||
      String(candidate.id).includes(search) ||
      (candidate.title ?? "").toLowerCase().includes(search) ||
      candidate.source_url.toLowerCase().includes(search);
    return matchesStatus && matchesSearch;
  });

  return (
    <div className="mx-auto max-w-7xl space-y-4 p-6">
      <header className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-semibold tracking-tight">Processing</h1>
            {!isDesktop && <Badge variant="outline">browser preview</Badge>}
            {loadedRevision && (
              <Badge variant="secondary">revision {loadedRevision.revision_number}</Badge>
            )}
            {dirty && <Badge variant="destructive">unsaved edits</Badge>}
          </div>
          <p className="text-sm text-muted-foreground">
            {activeAccountName ?? account?.name ?? "no account"}
            {account?.niche_label ? ` · ${account.niche_label}` : ""}
          </p>
        </div>
      </header>

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(300px,0.8fr)_minmax(360px,1.2fr)]">
        <section className="overflow-hidden rounded-xl border bg-card">
          <div className="space-y-3 border-b p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-semibold">Videos</p>
                <p className="text-xs text-muted-foreground">{filteredItems.length} shown</p>
              </div>
              <Badge variant="secondary">{items.length} total</Badge>
            </div>
            <div className="grid grid-cols-[1fr_120px] gap-2">
              <input
                className="h-9 min-w-0 rounded-md border border-input bg-transparent px-3 text-sm"
                placeholder="Search videos..."
                value={itemSearch}
                onChange={(event) => setItemSearch(event.target.value)}
              />
              <select
                className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
                value={itemFilter}
                onChange={(event) => setItemFilter(event.target.value)}
              >
                <option value="all">All</option>
                <option value="new">New</option>
                <option value="draft">Draft</option>
                <option value="exported">Exported</option>
                <option value="posted">Posted</option>
                <option value="skipped">Skipped</option>
              </select>
            </div>
          </div>
          <div className="max-h-[600px] overflow-auto">
            <table className="w-full table-fixed text-left text-sm">
              <thead className="sticky top-0 bg-muted text-xs text-muted-foreground">
                <tr>
                  <th className="w-28 px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Title</th>
                  <th className="w-28 px-3 py-2 font-medium">Added</th>
                </tr>
              </thead>
              <tbody>
                {filteredItems.map((candidate) => {
                  const meta = statusMeta(candidate.status);
                  return (
                    <tr
                      key={candidate.id}
                      className={`cursor-pointer border-t transition-colors hover:bg-accent ${
                        candidate.id === item.id ? "bg-accent" : ""
                      }`}
                      onClick={() => switchItem(candidate.id)}
                    >
                      <td className="px-3 py-2">
                        <span className="flex items-center gap-2">
                          <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${meta.dot}`} />
                          <span>{meta.label}</span>
                        </span>
                      </td>
                      <td
                        className="truncate px-3 py-2"
                        title={candidate.title ?? candidate.source_url}
                      >
                        <span className="flex items-center gap-2">
                          {candidate.is_new && (
                            <Badge variant="default" className="px-1.5 py-0 text-[10px]">
                              NEW
                            </Badge>
                          )}
                          <span className="truncate">
                            #{candidate.id} {candidate.title ?? candidate.source_url}
                          </span>
                        </span>
                      </td>
                      <td className="px-3 py-2 text-xs text-muted-foreground">
                        {shortDate(candidate.created_at)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>

        <section className="overflow-hidden rounded-xl bg-zinc-950">
          <div className="flex items-center justify-between border-b border-white/10 px-4 py-3 text-white">
            <div>
              <p className="text-xs font-medium uppercase tracking-wider text-zinc-400">
                Preview
              </p>
              <p className="mt-1 text-sm">{previewMode === "exported" ? "Exported reel" : "Original video"}</p>
            </div>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant={previewMode === "original" ? "default" : "ghost"}
                onClick={() => {
                  setPreviewError(null);
                  setPreviewMode("original");
                }}
                disabled={!item.original_preview_url}
              >
                Original
              </Button>
              <Button
                size="sm"
                variant={previewMode === "exported" ? "default" : "ghost"}
                onClick={() => {
                  setPreviewError(null);
                  setPreviewMode("exported");
                }}
                disabled={!item.exported_preview_url}
              >
                Exported
              </Button>
            </div>
          </div>
          <div className="flex min-h-[600px] items-center justify-center bg-black p-4">
            <div className="flex aspect-[9/16] max-h-[568px] w-full max-w-[320px] items-center justify-center overflow-hidden bg-black">
              {previewUrl && !previewError ? (
                <video
                  key={previewUrl}
                  className="h-full w-full object-contain"
                  src={previewUrl}
                  controls
                  preload="metadata"
                  onError={(event) => {
                    const code = event.currentTarget.error?.code;
                    const message = event.currentTarget.error?.message;
                    setPreviewError(
                      `Media error${code ? ` ${code}` : ""}${message ? `: ${message}` : ""}`,
                    );
                  }}
                />
              ) : (
                <p className="px-4 text-center text-sm text-zinc-400">
                  {previewError
                    ? `The selected video could not be loaded. ${previewError}`
                    : "This item has no local video file to preview."}
                </p>
              )}
            </div>
          </div>
        </section>
      </div>

      {workflow && (
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <div>
              <CardTitle className="text-base">Workflow Settings</CardTitle>
              <p className="mt-1 text-sm text-muted-foreground">
                These values guide Groq and the copied Codex/Claude prompt.
              </p>
            </div>
            <Button variant="outline" onClick={() => bridge.openItemFolder(item.id)}>
              Open Folder
            </Button>
          </CardHeader>
          <CardContent className="space-y-4">
            <label className="block space-y-1">
              <span className="text-sm font-medium">Clip premise</span>
              <textarea
                className="min-h-20 w-full rounded-md border border-input bg-transparent p-3 text-sm"
                placeholder="Optional direction, context, joke, or anomaly for the AI..."
                value={workflow.clip_premise}
                onChange={(event) =>
                  setWorkflow({ ...workflow, clip_premise: event.target.value })
                }
              />
            </label>
            <div className="grid gap-3 md:grid-cols-3">
              <label className="space-y-1">
                <span className="text-sm font-medium">Caption Style</span>
                <select
                  className="h-10 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                  value={workflow.caption_style}
                  onChange={(event) =>
                    setWorkflow({ ...workflow, caption_style: event.target.value })
                  }
                >
                  {workflow.caption_style_options.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label className="space-y-1">
                <span className="text-sm font-medium">Title Style</span>
                <select
                  className="h-10 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                  value={workflow.title_style}
                  onChange={(event) =>
                    setWorkflow({ ...workflow, title_style: event.target.value })
                  }
                >
                  {workflow.title_style_options.map((option) => (
                    <option key={option.value || "auto"} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label className="space-y-1">
                <span className="text-sm font-medium">Template</span>
                <select
                  className="h-10 w-full rounded-md border border-input bg-transparent px-2 text-sm"
                  value={workflow.template}
                  onChange={(event) =>
                    setWorkflow({ ...workflow, template: event.target.value })
                  }
                >
                  {workflow.template_options.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </label>
            </div>
            <Button onClick={saveWorkflow} disabled={busy}>Save workflow settings</Button>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Draft Generation</CardTitle>
          <p className="text-sm text-muted-foreground">
            Generate directly with Groq, or use a chat/coding agent and return the result
            through the clipboard or automatic database handoff.
          </p>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          <section className="space-y-3 rounded-lg border p-4">
            <div>
              <p className="font-medium">Generate with Groq API</p>
              <p className="text-sm text-muted-foreground">
                Runs in the background and saves the generated options as a new revision.
              </p>
            </div>
            <Button
              onClick={generate}
              disabled={generating || busy || !canGenerate}
              title={canGenerate ? undefined : "No draft provider configured (set GROQ_API_KEY)"}
            >
              {generating ? "Generating…" : "Generate with Groq"}
            </Button>
            {!canGenerate && (
              <p className="text-xs text-muted-foreground">
                No provider configured. Set GROQ_API_KEY or use the chat prompt path.
              </p>
            )}
          </section>

          <section className="space-y-3 rounded-lg border p-4">
            <div>
              <p className="font-medium">Copy Chat Prompt</p>
              <p className="text-sm text-muted-foreground">
                Use ChatGPT, Claude, Codex, or Claude Code to inspect the local video and
                write the options.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" onClick={copyChatPrompt} disabled={busy}>
                Copy Chat Prompt
              </Button>
              <Button variant="outline" onClick={pasteDraft} disabled={busy}>
                Paste Draft from Clipboard
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              Automatic Codex/Claude Code handoff: ask the agent to save through
              <code className="mx-1">scripts/nicheflow_drafts.py</code>. New revisions
              appear here automatically without pasting.
            </p>
          </section>

          {handoffMessage && (
            <p className="text-sm text-emerald-600 md:col-span-2">{handoffMessage}</p>
          )}
        </CardContent>
      </Card>

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
        <div
          role="alert"
          aria-live="assertive"
          className="fixed right-5 top-5 z-50 flex max-w-md items-start gap-3 rounded-lg border border-destructive/60 bg-destructive px-4 py-3 text-sm font-medium text-destructive-foreground shadow-2xl shadow-black/40"
        >
          <span className="grow">{actionError}</span>
          <button
            type="button"
            aria-label="Dismiss warning"
            className="rounded px-1 text-lg leading-none text-destructive-foreground/80 transition hover:bg-black/20 hover:text-destructive-foreground active:scale-90"
            onClick={() => setActionError(null)}
          >
            ×
          </button>
        </div>
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
        <div className="grid gap-4 md:grid-cols-3">
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
        <footer className="flex flex-wrap items-center gap-2">
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
          <div className="grow" />
          <Button variant="secondary" onClick={exportReel} disabled={exporting || dirty}>
            {exporting ? "Exporting…" : "Export Reel"}
          </Button>
        </footer>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Selected Title and Caption</CardTitle>
          <p className="text-sm text-muted-foreground">
            Applying an option fills these fields. Edit and save the final text used for export and publishing.
          </p>
        </CardHeader>
        <CardContent className="space-y-3">
          <label className="block space-y-1">
            <span className="text-sm font-medium">Title</span>
            <textarea
              className="min-h-20 w-full rounded-md border border-input bg-transparent p-3 text-sm"
              value={finalTitle}
              onChange={(event) => setFinalTitle(event.target.value)}
            />
          </label>
          <label className="block space-y-1">
            <span className="text-sm font-medium">Caption</span>
            <textarea
              className="min-h-48 w-full rounded-md border border-input bg-transparent p-3 text-sm"
              value={finalCaption}
              onChange={(event) => setFinalCaption(event.target.value)}
            />
          </label>
          <Button onClick={saveFinalDraft} disabled={busy}>Save selected title and caption</Button>
        </CardContent>
      </Card>

      {exportProgress && (
        <div className="space-y-1">
          <div className="h-2 w-full overflow-hidden rounded-full bg-secondary">
            <div
              className="h-full bg-primary transition-[width] duration-300"
              style={{ width: `${Math.round(exportProgress.value * 100)}%` }}
            />
          </div>
          <p className="text-xs text-muted-foreground">{exportProgress.message}</p>
        </div>
      )}

      {exportedPath && !exporting && (
        <p className="text-sm text-emerald-600">Exported: {exportedPath}</p>
      )}

      {dirty && (
        <p className="text-xs text-muted-foreground">
          Save or discard your edits before exporting.
        </p>
      )}

      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <CardTitle className="text-base">Publish</CardTitle>
          {!item.processed_path && (
            <span className="text-xs text-muted-foreground">export the reel first</span>
          )}
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              disabled={!item.processed_path || busy}
              onClick={() => queueForPublish(null)}
            >
              Add to Publish Queue
            </Button>
            <Button
              variant="secondary"
              disabled={!item.processed_path || busy}
              onClick={autoScheduleForPublish}
            >
              Auto Schedule
            </Button>
            <input
              type="datetime-local"
              className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
              value={scheduleAt}
              onChange={(e) => setScheduleAt(e.target.value)}
            />
            <Button
              disabled={!item.processed_path || !scheduleAt || busy}
              onClick={() => queueForPublish(scheduleAt)}
            >
              Schedule
            </Button>
          </div>
          {publishMessage && <p className="text-sm text-emerald-600">{publishMessage}</p>}
          {publishJobs.length > 0 ? (
            <ul className="space-y-1 text-sm">
              {publishJobs.map((job) => (
                <li key={job.id} className="flex items-center gap-2">
                  <Badge variant={job.posted_at ? "default" : "secondary"}>{job.status}</Badge>
                  <span className="text-muted-foreground">
                    {job.title ?? "(untitled)"}
                    {job.scheduled_at ? ` — scheduled ${job.scheduled_at}` : ""}
                    {job.posted_at ? ` — posted ${job.posted_at}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted-foreground">
              No publish-queue entries for this item yet. Posting to Instagram still happens
              from the desktop app's Publish Queue.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
