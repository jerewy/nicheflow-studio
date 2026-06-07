import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { bridge } from "@/lib/bridge";
import type { PublishQueueJob } from "@/types";

type FormState = {
  posted_url: string;
  posted_views: string;
  posted_likes: string;
  posted_comments: string;
  posted_shares: string;
  content_type: string;
  scheduled_at: string;
};

const EMPTY_FORM: FormState = {
  posted_url: "",
  posted_views: "",
  posted_likes: "",
  posted_comments: "",
  posted_shares: "",
  content_type: "",
  scheduled_at: "",
};

function jobToForm(job: PublishQueueJob): FormState {
  const num = (v: number | null) => (v == null ? "" : String(v));
  return {
    posted_url: job.posted_url ?? "",
    posted_views: num(job.posted_views),
    posted_likes: num(job.posted_likes),
    posted_comments: num(job.posted_comments),
    posted_shares: num(job.posted_shares),
    content_type: job.content_type ?? "",
    scheduled_at: "",
  };
}

const STATUS_VARIANT: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  posted: "default",
  scheduled: "secondary",
  draft: "outline",
  ready: "secondary",
  failed: "destructive",
};

export function PublishingScreen() {
  const [jobs, setJobs] = useState<PublishQueueJob[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setJobs(await bridge.listPublishQueue());
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const selected = jobs.find((j) => j.id === selectedId) ?? null;
  const isPosted = selected?.status === "posted" || selected?.posted_at != null;

  const select = (job: PublishQueueJob) => {
    setError(null);
    setMessage(null);
    setSelectedId(job.id);
    setForm(jobToForm(job));
  };

  const setField = (key: keyof FormState, value: string) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const run = async (fn: () => Promise<unknown>, note: string) => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await fn();
      await refresh();
      setMessage(note);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const markPosted = () =>
    run(
      () =>
        bridge.markJobPosted(selectedId!, {
          posted_url: form.posted_url,
          posted_views: form.posted_views,
          posted_likes: form.posted_likes,
          posted_comments: form.posted_comments,
          posted_shares: form.posted_shares,
          content_type: form.content_type,
        }),
      "Recorded as posted.",
    );

  const reschedule = () =>
    run(() => bridge.rescheduleJob(selectedId!, form.scheduled_at), "Rescheduled.");

  const unschedule = () => run(() => bridge.unscheduleJob(selectedId!), "Unscheduled.");

  const remove = (job: PublishQueueJob) => {
    if (!window.confirm(`Remove "${job.title ?? "this job"}" from the queue?`)) return;
    void run(async () => {
      await bridge.removePublishJob(job.id);
      if (selectedId === job.id) {
        setSelectedId(null);
        setForm(EMPTY_FORM);
      }
    }, "Removed from queue.");
  };

  return (
    <div className="mx-auto grid max-w-5xl gap-4 p-6 md:grid-cols-[300px_1fr]">
      <aside className="space-y-2">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-semibold tracking-tight">Publish Queue</h1>
          <Button size="sm" variant="secondary" onClick={refresh}>
            Refresh
          </Button>
        </div>
        <div className="space-y-1">
          {jobs.map((job) => (
            <button
              key={job.id}
              onClick={() => select(job)}
              className={cn(
                "w-full rounded-md border px-3 py-2 text-left text-sm transition-colors hover:bg-accent",
                selectedId === job.id && "border-ring bg-accent",
              )}
            >
              <div className="flex items-center gap-2">
                <Badge variant={STATUS_VARIANT[job.status] ?? "outline"}>{job.status}</Badge>
                <span className="truncate font-medium">{job.title ?? `Job #${job.id}`}</span>
              </div>
              <div className="truncate text-xs text-muted-foreground">
                {job.account_name ?? "no account"}
                {job.scheduled_at ? ` · scheduled ${job.scheduled_at.slice(0, 16)}` : ""}
                {job.posted_at ? ` · posted` : ""}
              </div>
            </button>
          ))}
          {jobs.length === 0 && (
            <p className="text-sm text-muted-foreground">The publish queue is empty.</p>
          )}
        </div>
      </aside>

      <section className="space-y-3">
        {error && <p className="text-sm text-destructive">{error}</p>}
        {message && <p className="text-sm text-emerald-600">{message}</p>}

        {!selected ? (
          <Card>
            <CardContent className="p-6 text-sm text-muted-foreground">
              Select a queue item to record a post or change its schedule. Actual posting to
              Instagram still runs from the desktop app.
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardHeader className="space-y-0">
              <CardTitle className="text-base">{selected.title ?? `Job #${selected.id}`}</CardTitle>
              <p className="text-xs text-muted-foreground">
                {selected.account_name ?? "no account"} · status {selected.status}
                {selected.posted_url ? (
                  <>
                    {" · "}
                    <a className="underline" href={selected.posted_url} target="_blank" rel="noreferrer">
                      posted link
                    </a>
                  </>
                ) : null}
              </p>
            </CardHeader>
            <CardContent className="space-y-4">
              {!isPosted && (
                <div className="flex flex-wrap items-end gap-2">
                  <div className="space-y-1">
                    <label className="text-xs font-medium text-muted-foreground">Schedule for</label>
                    <input
                      type="datetime-local"
                      className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
                      value={form.scheduled_at}
                      onChange={(e) => setField("scheduled_at", e.target.value)}
                    />
                  </div>
                  <Button disabled={!form.scheduled_at || busy} onClick={reschedule}>
                    Reschedule
                  </Button>
                  <Button variant="outline" disabled={busy} onClick={unschedule}>
                    Unschedule
                  </Button>
                </div>
              )}

              <div className="space-y-2 border-t border-border pt-3">
                <p className="text-sm font-medium">Record a manual post</p>
                <div className="space-y-1">
                  <label className="text-xs font-medium text-muted-foreground">Posted URL</label>
                  <Input
                    value={form.posted_url}
                    placeholder="https://instagram.com/reel/..."
                    onChange={(e) => setField("posted_url", e.target.value)}
                  />
                </div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {(
                    [
                      ["posted_views", "Views"],
                      ["posted_likes", "Likes"],
                      ["posted_comments", "Comments"],
                      ["posted_shares", "Shares"],
                    ] as [keyof FormState, string][]
                  ).map(([key, label]) => (
                    <div key={key} className="space-y-1">
                      <label className="text-xs font-medium text-muted-foreground">{label}</label>
                      <Input
                        inputMode="numeric"
                        value={form[key]}
                        onChange={(e) => setField(key, e.target.value)}
                      />
                    </div>
                  ))}
                </div>
                <div className="flex items-center gap-2 pt-1">
                  <Button onClick={markPosted} disabled={busy}>
                    {isPosted ? "Update post info" : "Mark posted"}
                  </Button>
                  <Button variant="destructive" disabled={busy} onClick={() => remove(selected)}>
                    Remove from queue
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
        )}
      </section>
    </div>
  );
}
