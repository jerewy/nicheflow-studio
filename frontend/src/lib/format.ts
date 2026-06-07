export function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}
