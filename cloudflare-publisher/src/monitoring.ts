export const STALE_AWAITING_UPLOAD_MINUTES = 120;
export const STALE_PROCESSING_MINUTES = 120;

export interface StatusCountRow {
  status: string;
  count: number;
}

export function statusCounts(rows: StatusCountRow[]): Record<string, number> {
  return Object.fromEntries(rows.map((row) => [row.status, Number(row.count) || 0]));
}

export function ageMinutes(iso: string | null, now: string): number | null {
  if (!iso) return null;
  const ageMs = Date.parse(now) - Date.parse(iso);
  if (!Number.isFinite(ageMs)) return null;
  return Math.max(0, Math.floor(ageMs / 60_000));
}
