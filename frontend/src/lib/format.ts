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

/** A CDXJ timestamp as something readable: `20260810120000` → `2026-08-10 12:00`. */
export function readableTimestamp(ts: string): string {
  if (ts.length < 12) return ts;
  const [y, mo, d, h, mi] = [
    ts.slice(0, 4),
    ts.slice(4, 6),
    ts.slice(6, 8),
    ts.slice(8, 10),
    ts.slice(10, 12),
  ];
  return `${y}-${mo}-${d} ${h}:${mi}`;
}

/** Just the clock part: `10:50 PM`, or `22:50` where that is the convention. */
export function clock(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString(undefined, { timeStyle: "short" });
}

/** Just the day: `Aug 14, 2026`. */
export function day(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" });
}

/**
 * Whole days from now until `iso`. Negative once it has passed, null if there
 * is no date at all.
 *
 * Floored rather than rounded, and that direction is deliberate: something
 * expiring in 30 hours is "1 day", not "2". Rounding up on a deadline reads as
 * more notice than there is.
 */
export function daysUntil(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const when = new Date(iso).getTime();
  if (Number.isNaN(when)) return null;
  return Math.floor((when - Date.now()) / 86_400_000);
}

/**
 * How long something took, from two timestamps.
 *
 * Coarser as it gets longer, because "1h 4m" is the useful shape of a long
 * capture and "1h 4m 12s" is the same fact with noise on the end. Seconds
 * survive only under a minute, where they are the whole answer.
 */
export function duration(from: string | null | undefined, to: string | null | undefined): string {
  if (!from || !to) return "";
  const ms = new Date(to).getTime() - new Date(from).getTime();
  // A clock that went backwards, or a finish recorded before its start. Saying
  // nothing beats "-3m", which reads as a bug in the capture rather than in
  // the two timestamps.
  if (!Number.isFinite(ms) || ms < 0) return "";
  const secs = Math.round(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

/**
 * A run as one line: the day, when it started, when it stopped, how long.
 *
 * Relative times answer "is this recent?" and nothing else — "finished 3h ago"
 * cannot tell you a capture took forty minutes, and that is usually the
 * question. A run spanning midnight carries both dates rather than implying
 * the finish belongs to the start's day.
 */
export function ranFromTo(
  started: string | null | undefined,
  finished: string | null | undefined,
): string {
  if (!started) return "";
  if (!finished) return `${day(started)} · started ${clock(started)}`;
  const sameDay = day(started) === day(finished);
  const took = duration(started, finished);
  const end = sameDay ? clock(finished) : `${day(finished)} ${clock(finished)}`;
  return `${day(started)} · ${clock(started)} → ${end}${took ? ` (${took})` : ""}`;
}
