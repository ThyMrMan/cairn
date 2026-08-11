import { useQuery } from "@tanstack/react-query";

import { endpoints } from "./api";

/**
 * The running build, for the sidebar and the settings page.
 *
 * Refetches on window focus even though the app disables that globally: the
 * whole point of showing a build is to answer "is this the version I just
 * deployed?", and a tab left open across a restart would otherwise keep
 * insisting on the old one — the exact wrong answer at the exact wrong moment.
 */
export function useVersion() {
  return useQuery({
    queryKey: ["version"],
    queryFn: endpoints.version,
    staleTime: 60_000,
    refetchOnWindowFocus: true,
  });
}
