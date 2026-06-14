export function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

// "Xh Ym ago" from a minute count, for the recent-post publish warnings.
export function formatAgo(minutes: number): string {
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  if (hours <= 0) return `${mins}m ago`;
  if (mins <= 0) return `${hours}h ago`;
  return `${hours}h ${mins}m ago`;
}
