import { useQuery } from "@tanstack/react-query";

import { endpoints, type PatternCheck } from "../lib/api";

/**
 * How many archived URLs each skip pattern actually matches.
 *
 * A reject pattern has only ever had one piece of feedback — whether it
 * compiles — and a pattern that is valid and matches nothing looks exactly
 * like one that works. The way that gets found out is counting a crawl an hour
 * later and seeing the URLs still there.
 *
 * The archive already knows. `capture_urls` holds every URL a capture fetched,
 * so this is a real number rather than a guess.
 *
 * Counted against what *was* fetched, not what will be. A pattern that fires
 * stops those URLs being discovered at all, so the next capture's list is
 * smaller than the count suggests — it is a floor and a sanity check, which is
 * what "did I write this right?" needs.
 */
export function usePatternMatches(patterns: string[], siteId?: number) {
  return useQuery({
    // The patterns are the key: the answer changes only when the list does,
    // and re-asking on every re-render would scan the sample each time.
    queryKey: ["pattern-check", siteId ?? null, patterns],
    queryFn: () => endpoints.checkSkipPatterns(patterns, siteId),
    enabled: patterns.length > 0,
    staleTime: 60_000,
  });
}

export function matchesFor(check: PatternCheck | undefined, pattern: string) {
  return check?.results.find((r) => r.pattern === pattern);
}

/** The count beside one pattern. Silent until the answer is in. */
export function Matches({ check, pattern }: { check: PatternCheck | undefined; pattern: string }) {
  const hit = matchesFor(check, pattern);
  if (!hit) return null;
  if (hit.error) {
    return <span className="shrink-0 text-xs text-danger">{hit.error}</span>;
  }
  if (hit.count === 0) {
    return (
      <span
        className="shrink-0 text-xs text-warn"
        title={
          `No URL in the last ${check?.captures ?? 0} capture(s) matches this. ` +
          "If it was copied from the “what it fetched” report, that is a URL " +
          "shape and not a regular expression — # there means a number, and no " +
          "fetched URL contains a literal #."
        }
      >
        matches nothing
      </span>
    );
  }
  return (
    <span
      className="shrink-0 text-xs text-muted tabular-nums"
      title={hit.examples.join("\n")}
    >
      {hit.count.toLocaleString()}
      {check?.truncated ? "+" : ""}
    </span>
  );
}

/** One line under a list saying what the counts were measured against. */
export function MatchesFootnote({ check }: { check: PatternCheck | undefined }) {
  if (!check || check.checked === 0) return null;
  return (
    <p className="mt-2 text-xs text-muted">
      Counted against {check.checked.toLocaleString()}
      {check.truncated ? "+" : ""} URLs from the {check.captures} most recent capture
      {check.captures === 1 ? "" : "s"} — what these patterns <em>would have</em> skipped. Hover a
      count for examples.
    </p>
  );
}
