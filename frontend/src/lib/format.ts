export function bytes(n: number | null | undefined): string {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/**
 * A human interval, in either direction.
 *
 * Future timestamps are real here — a cookie jar's earliest expiry is the
 * whole point of showing one — and treating them as past produced
 * "-30d ago" for something 30 days away.
 */
export function relative(iso: string | null | undefined): string {
  if (!iso) return "never";
  const diff = Date.now() - new Date(iso).getTime();
  const past = diff >= 0;
  const mins = Math.round(Math.abs(diff) / 60_000);
  const phrase = (value: number, unit: string) =>
    past ? `${value}${unit} ago` : `in ${value}${unit}`;

  if (mins < 1) return past ? "just now" : "any moment";
  if (mins < 60) return phrase(mins, "m");
  const hours = Math.round(mins / 60);
  if (hours < 24) return phrase(hours, "h");
  return phrase(Math.round(hours / 24), "d");
}
