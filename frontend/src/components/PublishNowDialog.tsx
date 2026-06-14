import { Button } from "@/components/ui/button";
import { formatAgo, formatDate } from "@/lib/format";
import type { PublishRecency } from "@/types";

interface PublishNowDialogProps {
  recency: PublishRecency;
  busy: boolean;
  onSchedule: () => void;
  onPublishAnyway: () => void;
  onCancel: () => void;
}

/**
 * Shown before "Publish Now" when posting this reel would be unsafe. Two cases:
 *
 * - The account posted recently (within the same-account window): offers
 *   reschedule to the next safe slot, post anyway, or cancel.
 * - A live post for the account is already running (`in_progress`): posting now
 *   is impossible (one browser, one post at a time), so "Post anyway" is hidden —
 *   only reschedule or cancel.
 *
 * Mirrors {@link PublishDueDialog} so an accidental back-to-back post can't slip
 * through whichever way the conflict arises.
 */
export function PublishNowDialog({
  recency,
  busy,
  onSchedule,
  onPublishAnyway,
  onCancel,
}: PublishNowDialogProps) {
  const accountName = recency.account_name ?? "This account";
  const inProgress = recency.in_progress === true;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="publish-now-dialog-title"
    >
      <div className="w-full max-w-md space-y-4 rounded-xl border bg-card p-5 shadow-lg">
        <div className="space-y-1">
          <h2 id="publish-now-dialog-title" className="text-lg font-semibold">
            {inProgress
              ? `${accountName} is already posting`
              : `${accountName} posted recently`}
          </h2>
          <p className="text-sm text-muted-foreground">
            {inProgress ? (
              <>
                A post to this account is already running — only one can post at a
                time. Move this reel to the next safe slot, or cancel and try again
                once it finishes.
              </>
            ) : (
              <>
                It posted{" "}
                {recency.minutes_since !== undefined
                  ? formatAgo(recency.minutes_since)
                  : "recently"}
                . Posting again now can split each reel&apos;s reach — a safe gap is
                ~{formatDate(recency.recommended_next_at ?? null)}.
              </>
            )}
          </p>
        </div>
        <div className="flex flex-col gap-2">
          <Button onClick={onSchedule} disabled={busy}>
            Schedule to next safe slot
          </Button>
          <p className="px-1 text-xs text-muted-foreground">
            Moves this reel to the account&apos;s next safe posting slot instead
            of posting now.
          </p>
          {!inProgress && (
            <Button variant="outline" onClick={onPublishAnyway} disabled={busy}>
              Post anyway
            </Button>
          )}
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        </div>
      </div>
    </div>
  );
}
