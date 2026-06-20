import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { bridge } from "@/lib/bridge";
import type { CropRect } from "@/types";

const MIN_SIZE = 0.05; // smallest keep-region, as a fraction of the source
const FULL: CropRect = { x: 0, y: 0, w: 1, h: 1 };

type Handle = "nw" | "ne" | "sw" | "se" | "n" | "e" | "s" | "w";
type Mode = "move" | Handle;

interface DragState {
  mode: Mode;
  startX: number;
  startY: number;
  startRect: CropRect;
  boxW: number;
  boxH: number;
}

const clamp = (value: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, value));

// Apply a pointer delta (already converted to source fractions) to the rect.
function applyDrag(drag: DragState, dx: number, dy: number): CropRect {
  const { x, y, w, h } = drag.startRect;
  if (drag.mode === "move") {
    return { x: clamp(x + dx, 0, 1 - w), y: clamp(y + dy, 0, 1 - h), w, h };
  }
  if (drag.mode === "nw") {
    const nx = clamp(x + dx, 0, x + w - MIN_SIZE);
    const ny = clamp(y + dy, 0, y + h - MIN_SIZE);
    return { x: nx, y: ny, w: w + (x - nx), h: h + (y - ny) };
  }
  if (drag.mode === "ne") {
    const ny = clamp(y + dy, 0, y + h - MIN_SIZE);
    return { x, y: ny, w: clamp(w + dx, MIN_SIZE, 1 - x), h: h + (y - ny) };
  }
  if (drag.mode === "sw") {
    const nx = clamp(x + dx, 0, x + w - MIN_SIZE);
    return { x: nx, y, w: w + (x - nx), h: clamp(h + dy, MIN_SIZE, 1 - y) };
  }
  // Edge handles move a single side; the opposite three stay put.
  if (drag.mode === "n") {
    const ny = clamp(y + dy, 0, y + h - MIN_SIZE);
    return { x, y: ny, w, h: h + (y - ny) };
  }
  if (drag.mode === "s") {
    return { x, y, w, h: clamp(h + dy, MIN_SIZE, 1 - y) };
  }
  if (drag.mode === "w") {
    const nx = clamp(x + dx, 0, x + w - MIN_SIZE);
    return { x: nx, y, w: w + (x - nx), h };
  }
  if (drag.mode === "e") {
    return { x, y, w: clamp(w + dx, MIN_SIZE, 1 - x), h };
  }
  // "se"
  return { x, y, w: clamp(w + dx, MIN_SIZE, 1 - x), h: clamp(h + dy, MIN_SIZE, 1 - y) };
}

interface CropEditorProps {
  itemId: number;
  onClose: () => void;
  onSaved: (message: string) => void;
}

export function CropEditor({ itemId, onClose, onSaved }: CropEditorProps) {
  const [rect, setRect] = useState<CropRect>(FULL);
  const [aspect, setAspect] = useState(9 / 16);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  // Mirror the rect so a drag can capture the current value without making the
  // pointer-down handler depend on (and re-create with) every rect change.
  const rectRef = useRef(rect);
  useEffect(() => {
    rectRef.current = rect;
  }, [rect]);

  // Load any existing override so the box opens where the user last left it.
  useEffect(() => {
    let cancelled = false;
    Promise.all([bridge.getCropOverride(itemId), bridge.getCropPreview(itemId)])
      .then(([saved, preview]) => {
        if (cancelled) return;
        if (saved) setRect(saved);
        setPreviewUrl(preview.preview_url);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setPreviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [itemId]);

  // Track the pointer on the whole window during a drag so the box keeps
  // following even if the cursor leaves the frame.
  useEffect(() => {
    const onMove = (event: PointerEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      const dx = (event.clientX - drag.startX) / drag.boxW;
      const dy = (event.clientY - drag.startY) / drag.boxH;
      setRect(applyDrag(drag, dx, dy));
    };
    const onUp = () => {
      dragRef.current = null;
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, []);

  // One stable handler for the box + all handles; the element's data-mode says
  // which drag it is. Reading refs here (not in render) keeps react-hooks happy.
  const onHandlePointerDown = useCallback((event: React.PointerEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    const box = containerRef.current?.getBoundingClientRect();
    if (!box) return;
    const mode = (event.currentTarget.dataset.mode as Mode | undefined) ?? "move";
    dragRef.current = {
      mode,
      startX: event.clientX,
      startY: event.clientY,
      startRect: rectRef.current,
      boxW: box.width,
      boxH: box.height,
    };
  }, []);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await bridge.saveCropOverride(itemId, {
        x: Number(rect.x.toFixed(5)),
        y: Number(rect.y.toFixed(5)),
        w: Number(rect.w.toFixed(5)),
        h: Number(rect.h.toFixed(5)),
      });
      onSaved("Crop saved - re-export the reel to apply it.");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const resetToAuto = async () => {
    setBusy(true);
    setError(null);
    try {
      await bridge.clearCropOverride(itemId);
      setRect(FULL);
      onSaved("Reset to auto-crop - re-export to apply.");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const handleStyle = "absolute h-3 w-3 rounded-sm border border-black/60 bg-white";
  // Edge handles are elongated along their side so they read as "drag this edge"
  // and offer a bigger hit target than the corner squares.
  const edgeStyle = "absolute rounded-sm border border-black/60 bg-white";

  return (
    <div className="flex w-full flex-col gap-3 rounded-xl border border-border bg-muted/20 p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold">Adjust source crop for next export</h2>
            <p className="text-sm text-muted-foreground">
              Drag inside the box to move it, or drag any edge or corner to resize.
              The title and layout are applied after this crop.
            </p>
          </div>
          <Button size="sm" variant="ghost" onClick={onClose}>
            Close
          </Button>
        </div>

        <div
          ref={containerRef}
          className="relative mx-auto touch-none select-none overflow-hidden bg-black"
          style={{ aspectRatio: String(aspect), height: "min(78vh, 92vw)" }}
        >
          {previewUrl ? (
            <img
              src={previewUrl}
              alt="Still frame from the original video"
              className="pointer-events-none absolute inset-0 h-full w-full object-contain"
              onLoad={(event) => {
                const image = event.currentTarget;
                if (image.naturalWidth > 0 && image.naturalHeight > 0) {
                  setAspect(image.naturalWidth / image.naturalHeight);
                }
              }}
            />
          ) : (
            <p className="absolute inset-0 flex items-center justify-center px-4 text-center text-sm text-zinc-400">
              {previewLoading ? "Preparing crop preview..." : "Crop preview unavailable."}
            </p>
          )}
          {/* Bound the dimmed surround to the video frame. A huge box-shadow can
              exhaust WebView2's compositing surface and blank the whole window. */}
          <div
            className="pointer-events-none absolute left-0 top-0 w-full bg-black/55"
            style={{ height: `${rect.y * 100}%` }}
          />
          <div
            className="pointer-events-none absolute bottom-0 left-0 w-full bg-black/55"
            style={{ height: `${(1 - rect.y - rect.h) * 100}%` }}
          />
          <div
            className="pointer-events-none absolute left-0 bg-black/55"
            style={{
              top: `${rect.y * 100}%`,
              width: `${rect.x * 100}%`,
              height: `${rect.h * 100}%`,
            }}
          />
          <div
            className="pointer-events-none absolute right-0 bg-black/55"
            style={{
              top: `${rect.y * 100}%`,
              width: `${(1 - rect.x - rect.w) * 100}%`,
              height: `${rect.h * 100}%`,
            }}
          />
          {/* Keep-region box: drag inside to move, or drag a corner to resize. */}
          <div
            className="absolute cursor-move border-2 border-white"
            style={{
              left: `${rect.x * 100}%`,
              top: `${rect.y * 100}%`,
              width: `${rect.w * 100}%`,
              height: `${rect.h * 100}%`,
            }}
            data-mode="move"
            onPointerDown={onHandlePointerDown}
          >
            <span
              className={`${handleStyle} -left-1.5 -top-1.5 cursor-nwse-resize`}
              data-mode="nw"
              onPointerDown={onHandlePointerDown}
            />
            <span
              className={`${handleStyle} -right-1.5 -top-1.5 cursor-nesw-resize`}
              data-mode="ne"
              onPointerDown={onHandlePointerDown}
            />
            <span
              className={`${handleStyle} -bottom-1.5 -left-1.5 cursor-nesw-resize`}
              data-mode="sw"
              onPointerDown={onHandlePointerDown}
            />
            <span
              className={`${handleStyle} -bottom-1.5 -right-1.5 cursor-nwse-resize`}
              data-mode="se"
              onPointerDown={onHandlePointerDown}
            />
            <span
              className={`${edgeStyle} -top-1.5 left-1/2 h-3 w-6 -translate-x-1/2 cursor-ns-resize`}
              data-mode="n"
              onPointerDown={onHandlePointerDown}
            />
            <span
              className={`${edgeStyle} -bottom-1.5 left-1/2 h-3 w-6 -translate-x-1/2 cursor-ns-resize`}
              data-mode="s"
              onPointerDown={onHandlePointerDown}
            />
            <span
              className={`${edgeStyle} -left-1.5 top-1/2 h-6 w-3 -translate-y-1/2 cursor-ew-resize`}
              data-mode="w"
              onPointerDown={onHandlePointerDown}
            />
            <span
              className={`${edgeStyle} -right-1.5 top-1/2 h-6 w-3 -translate-y-1/2 cursor-ew-resize`}
              data-mode="e"
              onPointerDown={onHandlePointerDown}
            />
          </div>
        </div>

        <p className="text-center text-xs text-muted-foreground">
          Keeping x {Math.round(rect.x * 100)}% | y {Math.round(rect.y * 100)}% | w{" "}
          {Math.round(rect.w * 100)}% | h {Math.round(rect.h * 100)}%
        </p>

        {error && <p className="text-sm text-destructive">{error}</p>}

        <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
          <Button variant="outline" disabled={busy} onClick={resetToAuto}>
            Reset to auto
          </Button>
          <div className="grow" />
          <Button variant="ghost" disabled={busy} onClick={() => setRect(FULL)}>
            Select all
          </Button>
          <Button disabled={busy} onClick={save}>
            Save crop
          </Button>
        </div>
    </div>
  );
}
