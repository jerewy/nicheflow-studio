import { Button } from "@/components/ui/button";
import { formatAgo, formatDate } from "@/lib/format";
import type { DueRecencyWarning } from "@/types";

interface PublishDueDialogProps {
  warnings: DueRecencyWarning[];
  dueCount: number;
  busy: boolean;
  onRescheduleSafely: () => void;
  onPublishAnyway: () => void;
  onCancel: () => void;
}

/**
 * Shown before "Publish due now" when one or more due reels would post too soon
 * after their account's last post. Offers three explicit choices instead of a
 * blunt OK/Cancel: reschedule the too-soon reels to a safe slot (and post the
 * rest), publish everything anyway, or cancel.
 */
export function PublishDueDialog({
  warnings,
  dueCount,
  busy,
  onRescheduleSafely,
  onPublishAnyway,
  onCancel,
}: PublishDueDialogProps) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="publish-due-dialog-title"
    >
      <div className="w-full max-w-md space-y-4 rounded-xl border bg-card p-5 shadow-lg">
        <div className="space-y-1">
          <h2 id="publish-due-dialog-title" className="text-lg font-semibold">
            Some accounts posted recently
          </h2>
          <p className="text-sm text-muted-foreground">
            Posting again within 4h can split each reel&apos;s reach. These due
            reel(s) would post too soon after the account&apos;s last post:
          </p>
        </div>
        <ul className="space-y-1 rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-sm">
          {warnings.map((warning) => (
            <li key={warning.account_id} className="flex justify-between gap-3">
              <span className="font-medium">{warning.account_name ?? "Account"}</span>
              <span className="text-muted-foreground">
                posted {formatAgo(warning.minutes_since)} · safe ~
                {formatDate(warning.recommended_next_at)}
              </span>
            </li>
          ))}
        </ul>
        <div className="flex flex-col gap-2">
          <Button onClick={onRescheduleSafely} disabled={busy}>
            Reschedule safely
          </Button>
          <p className="px-1 text-xs text-muted-foreground">
            Posts the reels that are safe now and moves the too-soon one(s) to
            their next safe slot.
          </p>
          <Button variant="outline" onClick={onPublishAnyway} disabled={busy}>
            Publish all {dueCount} anyway
          </Button>
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        </div>
      </div>
    </div>
  );
}
