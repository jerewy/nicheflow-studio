import { useEffect, useState } from "react";

import { bridge } from "@/lib/bridge";
import type { CropSource, EffectiveCrop } from "@/types";

// Everything the reel keeps is inside the box; everything outside is dimmed, so
// a mis-detected rectangle reads at a glance instead of needing to be measured.
const SOURCE_LABEL: Record<CropSource, string> = {
  manual: "Manual crop",
  auto: "Auto-detected",
  none: "Full frame",
};

const SOURCE_HINT: Record<CropSource, string> = {
  manual: "Framing you set by hand.",
  auto: "Detected footage area. Check it before publishing.",
  none: "No footage rectangle found, so the whole frame is kept.",
};

interface LoadedCrop {
  itemId: number;
  crop: EffectiveCrop;
  previewUrl: string | null;
}

interface CropPreviewProps {
  itemId: number;
  /** Opens the manual editor, so a bad crop is one click from being fixed. */
  onAdjust?: () => void;
}

/**
 * Read-only view of the keep-region an export will use.
 *
 * This exists because the automatic crop used to be invisible: the manual
 * editor opened on the full frame whenever no override was saved, so a wrong
 * rectangle only became apparent in the rendered reel. Detection is heuristic
 * and always will be, so the cheapest guard is showing the result at review
 * time rather than adding another rule to the detector.
 */
export function CropPreview({ itemId, onAdjust }: CropPreviewProps) {
  // Both results carry the item they describe, so a slow fetch for a previous
  // reel can never be painted over the current one, and switching reels reads
  // as "loading" without the effect having to reset state synchronously.
  const [loaded, setLoaded] = useState<LoadedCrop | null>(null);
  const [failure, setFailure] = useState<{ itemId: number; message: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([bridge.getEffectiveCrop(itemId), bridge.getCropPreview(itemId)])
      .then(([effective, preview]) => {
        if (cancelled) return;
        setLoaded({ itemId, crop: effective, previewUrl: preview.preview_url || null });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setFailure({ itemId, message: err instanceof Error ? err.message : String(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [itemId]);

  const ready = loaded && loaded.itemId === itemId ? loaded : null;
  const error = failure && failure.itemId === itemId ? failure.message : null;

  if (error) {
    return <p className="text-xs text-destructive">Could not check the crop: {error}</p>;
  }
  if (!ready) {
    return <p className="text-xs text-muted-foreground">Checking the crop…</p>;
  }
  if (!ready.previewUrl) {
    return <p className="text-xs text-muted-foreground">No crop preview available.</p>;
  }

  const previewUrl = ready.previewUrl;
  const { rect, source } = ready.crop;
  const pct = (value: number) => `${(value * 100).toFixed(3)}%`;

  return (
    <div className="flex gap-3">
      <div className="relative w-28 shrink-0 overflow-hidden rounded bg-black">
        <img src={previewUrl} alt="Source frame with the export crop marked" className="w-full" />
        {/* Four dimmers rather than one outlined box: the discarded area is
            what tells you the crop is wrong, so it gets the visual weight. */}
        <div className="absolute inset-x-0 top-0 bg-black/65" style={{ height: pct(rect.y) }} />
        <div
          className="absolute inset-x-0 bottom-0 bg-black/65"
          style={{ height: pct(1 - rect.y - rect.h) }}
        />
        <div
          className="absolute left-0 bg-black/65"
          style={{ top: pct(rect.y), height: pct(rect.h), width: pct(rect.x) }}
        />
        <div
          className="absolute right-0 bg-black/65"
          style={{ top: pct(rect.y), height: pct(rect.h), width: pct(1 - rect.x - rect.w) }}
        />
        <div
          className="absolute border-2 border-emerald-400"
          style={{
            left: pct(rect.x),
            top: pct(rect.y),
            width: pct(rect.w),
            height: pct(rect.h),
          }}
        />
      </div>
      <div className="min-w-0 space-y-1">
        <p className="text-sm font-medium">{SOURCE_LABEL[source]}</p>
        <p className="text-xs text-muted-foreground">{SOURCE_HINT[source]}</p>
        {onAdjust && (
          <button
            type="button"
            className="text-xs font-medium text-primary underline-offset-2 hover:underline"
            onClick={onAdjust}
          >
            Adjust crop
          </button>
        )}
      </div>
    </div>
  );
}
