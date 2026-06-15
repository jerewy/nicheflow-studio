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
