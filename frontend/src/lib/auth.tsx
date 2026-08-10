import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, type ReactNode } from "react";

import { ApiError, endpoints, type Me } from "./api";

type AuthState = {
  user: Me | null;
  loading: boolean;
  setupComplete: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();

  // Whether setup has run is public information and must be readable before
  // login — it decides between the setup screen and the login screen.
  const health = useQuery({
    queryKey: ["health"],
    queryFn: endpoints.health,
    staleTime: 30_000,
  });

  const me = useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await endpoints.me();
      } catch (err) {
        // 401 is the normal logged-out state, not an error worth retrying.
        if (err instanceof ApiError && err.status === 401) return null;
        throw err;
      }
    },
    retry: false,
    enabled: health.data?.setup_complete === true,
  });

  const refresh = useCallback(async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["me"] }),
      qc.invalidateQueries({ queryKey: ["health"] }),
    ]);
  }, [qc]);

  const logout = useCallback(async () => {
    try {
      await endpoints.logout();
    } finally {
      // Drop every cached response — some of it is user-scoped and must not
      // survive into the next session.
      qc.clear();
      await refresh();
    }
  }, [qc, refresh]);

  const value: AuthState = {
    user: me.data ?? null,
    loading: health.isLoading || (health.data?.setup_complete === true && me.isLoading),
    setupComplete: health.data?.setup_complete ?? false,
    refresh,
    logout,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
