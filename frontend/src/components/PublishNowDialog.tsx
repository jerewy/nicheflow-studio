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
 * Shown before "Publish Now" when the target account already posted within the
 * same-account recency window. Offers three explicit choices instead of a blunt
 * OK/Cancel: move this reel to the account's next safe slot (the safe default),
 * post anyway, or cancel. Mirrors {@link PublishDueDialog} for the single-item
 * live-post path so an accidental back-to-back post can't slip through.
 */
export function PublishNowDialog({
  recency,
  busy,
  onSchedule,
  onPublishAnyway,
  onCancel,
}: PublishNowDialogProps) {
  const accountName = recency.account_name ?? "This account";
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
            {accountName} posted recently
          </h2>
          <p className="text-sm text-muted-foreground">
            It posted{" "}
            {recency.minutes_since !== undefined
              ? formatAgo(recency.minutes_since)
              : "recently"}
            . Posting again now can split each reel&apos;s reach — a safe gap is
            ~{formatDate(recency.recommended_next_at ?? null)}.
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
          <Button variant="outline" onClick={onPublishAnyway} disabled={busy}>
            Post anyway
          </Button>
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        </div>
      </div>
    </div>
  );
}
