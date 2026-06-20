export function normalizeAccountKey(value: string): string {
  const normalized = value.trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9_-]{1,62}$/.test(normalized)) {
    throw new Error("account_key must be 2-63 lowercase letters, numbers, underscores, or hyphens");
  }
  return normalized;
}

export function safeFileName(value: string): string {
  const name = value.trim().replace(/[^a-zA-Z0-9._-]+/g, "_").replace(/^_+|_+$/g, "");
  return name.slice(-120) || "reel.mp4";
}

export function parseRange(value: string | null, size: number): R2Range | undefined {
  const match = value?.match(/^bytes=(\d*)-(\d*)$/);
  if (!match) return undefined;
  if (!match[1] && match[2]) return { suffix: Number(match[2]) };
  const start = Number(match[1]);
  const end = match[2] ? Number(match[2]) : size - 1;
  if (!Number.isFinite(start) || !Number.isFinite(end) || start > end) return undefined;
  return { offset: start, length: end - start + 1 };
}

// Meta error codes that mean "not this job's fault — it will recover on its own" rather
// than a permanent failure: blocked/restricted API access (200), invalid or expired access
// token (190, 102, 463, 467), and rate limits (4, 17, 32, 368, 613). On these we keep the
// media and back off, instead of burning retries and deleting the video — so a temporary
// account restriction only delays posts rather than destroying them.
const RECOVERABLE_META_CODES = new Set([4, 17, 32, 102, 190, 200, 368, 463, 467, 613]);
const MANUAL_LOCAL_META_CODES = new Set([4, 17, 32, 102, 190, 200, 463, 467, 613]);

function metaErrorCode(error: unknown): number | null {
  const match = String(error).match(/"code":\s*(\d+)/);
  if (!match) return null;
  return Number(match[1]);
}

export function isRecoverableMetaError(error: unknown): boolean {
  const code = metaErrorCode(error);
  return code !== null && RECOVERABLE_META_CODES.has(code);
}

export function shouldOfferManualLocalPublish(error: unknown): boolean {
  const code = metaErrorCode(error);
  return code !== null && MANUAL_LOCAL_META_CODES.has(code);
}
