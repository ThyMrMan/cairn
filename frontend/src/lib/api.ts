/**
 * Typed API client.
 *
 * Every mutating request carries `X-Requested-With: XMLHttpRequest`, which the
 * backend requires as CSRF protection — a cross-site form cannot set it.
 * Credentials are cookies, so nothing here touches localStorage.
 */

export const CSRF_HEADER = "X-Requested-With";
export const CSRF_VALUE = "XMLHttpRequest";

export type ApiErrorBody = {
  error: { code: string; message: string; detail?: unknown };
};

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status: number,
    readonly detail?: unknown,
    readonly retryAfter?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** Field-level problems, when the server sent a list of them. */
  get problems(): string[] {
    return Array.isArray(this.detail)
      ? this.detail.filter((d): d is string => typeof d === "string")
      : [];
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const { json, ...rest } = init;
  const headers = new Headers(rest.headers);
  headers.set(CSRF_HEADER, CSRF_VALUE);
  if (json !== undefined) headers.set("Content-Type", "application/json");

  const res = await fetch(`/api${path}`, {
    ...rest,
    headers,
    credentials: "same-origin",
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  const parsed: unknown = text ? JSON.parse(text) : null;

  if (!res.ok) {
    const body = parsed as ApiErrorBody | null;
    const retryAfter = Number(res.headers.get("Retry-After")) || undefined;
    throw new ApiError(
      body?.error?.code ?? "error",
      body?.error?.message ?? `Request failed (${res.status})`,
      res.status,
      body?.error?.detail,
      retryAfter,
    );
  }
  return parsed as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path, { method: "GET" }),
  post: <T>(path: string, json?: unknown) => request<T>(path, { method: "POST", json }),
  patch: <T>(path: string, json?: unknown) => request<T>(path, { method: "PATCH", json }),
  del: <T>(path: string, json?: unknown) => request<T>(path, { method: "DELETE", json }),
};

// ── response types (mirror cairn/api/schemas.py) ─────────────────────────

export type Health = {
  status: string;
  version: string;
  db: boolean;
  setup_complete: boolean;
  disk_free_bytes: number | null;
};

export type SetupStatus = { setup_complete: boolean; password_min_length: number };

export type LoginResponse = {
  username: string;
  expires_at: string;
  totp_enabled: boolean;
};

export type Me = {
  username: string;
  totp_enabled: boolean;
  created_at: string;
  last_login_at: string | null;
};

export type SessionInfo = {
  id: string;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  user_agent: string | null;
  ip: string | null;
  current: boolean;
};

export type AuditEntry = {
  id: number;
  ts: string;
  actor: string | null;
  action: string;
  target: string | null;
  ip: string | null;
};

export type Page<T> = { items: T[]; total: number; page: number; per_page: number };

export type Storage = {
  data_dir: string;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  sites: number;
  archives_bytes: number;
};

// ── endpoints ────────────────────────────────────────────────────────────

export const endpoints = {
  health: () => api.get<Health>("/health"),
  setupStatus: () => api.get<SetupStatus>("/setup"),
  setup: (username: string, password: string) =>
    api.post<LoginResponse>("/setup", { username, password }),
  login: (username: string, password: string, totp?: string) =>
    api.post<LoginResponse>("/auth/login", { username, password, totp: totp || null }),
  logout: () => api.post<{ ok: boolean }>("/auth/logout"),
  me: () => api.get<Me>("/auth/me"),
  changePassword: (current: string, next: string) =>
    api.post<{ revoked_sessions: number }>("/auth/password", { current, new: next }),
  totpSetup: () => api.post<{ secret: string; provisioning_uri: string }>("/auth/totp/setup"),
  totpConfirm: (code: string) =>
    api.post<{ recovery_codes: string[] }>("/auth/totp/confirm", { code }),
  totpDisable: (password: string, code: string) =>
    api.del<{ ok: boolean }>("/auth/totp", { password, code }),
  sessions: () => api.get<SessionInfo[]>("/auth/sessions"),
  revokeSession: (id: string) => api.del<{ ok: boolean }>(`/auth/sessions/${id}`),
  revokeOthers: () => api.del<{ ok: boolean }>("/auth/sessions"),
  audit: (page = 1) => api.get<Page<AuditEntry>>(`/audit?page=${page}`),
  storage: () => api.get<Storage>("/storage"),
};
