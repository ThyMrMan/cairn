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
  put: <T>(path: string, json?: unknown) => request<T>(path, { method: "PUT", json }),
  text: async (path: string) => {
    const res = await fetch(`/api${path}`, {
      headers: { [CSRF_HEADER]: CSRF_VALUE },
      credentials: "same-origin",
    });
    if (!res.ok) throw new ApiError("error", `Request failed (${res.status})`, res.status);
    return res.text();
  },
  upload: async <T>(path: string, file: File): Promise<T> => {
    const body = new FormData();
    body.append("file", file);
    // No Content-Type header: the browser must set the multipart boundary.
    const res = await fetch(`/api${path}`, {
      method: "PUT",
      headers: { [CSRF_HEADER]: CSRF_VALUE },
      credentials: "same-origin",
      body,
    });
    const text = await res.text();
    const parsed: unknown = text ? JSON.parse(text) : null;
    if (!res.ok) {
      const err = parsed as ApiErrorBody | null;
      throw new ApiError(
        err?.error?.code ?? "error",
        err?.error?.message ?? `Upload failed (${res.status})`,
        res.status,
        err?.error?.detail,
      );
    }
    return parsed as T;
  },
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

// ── sites, scope, captures ───────────────────────────────────────────────

export type HostRule = {
  host: string;
  crawl_pages: boolean;
  fetch_assets: boolean;
  path_prefix: string | null;
  allow_extensionless: boolean;
};

export type Scope = {
  seeds: string[];
  hosts: HostRule[];
  exclude_hosts: string[];
  accept_patterns: string[];
  reject_patterns: string[];
  path_prefix: string | null;
  max_depth: number | null;
  max_pages: number | null;
  max_bytes: number | null;
  obey_robots: boolean;
  politeness: Record<string, unknown>;
  notes: string[];
  wget_preview: string[];
};

export type Site = {
  id: number;
  slug: string;
  title: string;
  seed_url: string;
  primary_host: string;
  folder_id: number;
  folder_path: string;
  status: string;
  engine_id: string;
  profile_id: number | null;
  keep_mirror: boolean;
  tags: string[];
  size_bytes: number;
  url_count: number;
  archive_path: string;
  last_capture_at: string | null;
  created_at: string;
  updated_at: string;
};

export type SiteDetail = Site & {
  notes: string | null;
  engine_config: Record<string, unknown>;
  scope: Scope;
  capture_count: number;
  running_job_id: number | null;
};

export type Capture = {
  id: number;
  site_id: number;
  job_id: number | null;
  kind: string;
  engine_id: string;
  engine_version: string | null;
  dir_name: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  url_count: number;
  error_count: number;
  bytes_written: number;
};

export type CaptureDetail = Capture & {
  artifacts: { name: string; kind: string; size: number; sha256: string }[];
  manifest: Record<string, unknown> | null;
};

export type CaptureUrl = {
  id: number;
  url: string;
  host: string;
  status_code: number | null;
  mime: string | null;
  size_bytes: number | null;
  is_revisit: boolean;
  fetched_at: string | null;
  error: string | null;
};

export type Job = {
  id: number;
  type: string;
  site_id: number | null;
  site_title: string | null;
  status: string;
  progress: { done?: number; total?: number; bytes?: number; eta_s?: number } | null;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  attempts: number;
};

export type Profile = {
  id: number;
  name: string;
  mode: string;
  hosts: string[];
  user_agent: string | null;
  cookie_count: number;
  session_cookie_count: number;
  hosts_covered: string[];
  sensitive: string[];
  warnings: string[];
  has_material: boolean;
  minted_at: string | null;
  expires_at: string | null;
  fingerprint: string | null;
  last_verified_at: string | null;
  last_verify_result: string | null;
  verify_url: string | null;
  notes: string | null;
  created_at: string;
};

export type CookieReport = {
  cookie_count: number;
  hosts_covered: string[];
  session_cookies: number;
  expired_cookies: number;
  earliest_expiry: string | null;
  sensitive: string[];
  warnings: string[];
  errors: string[];
  ok: boolean;
};

export type Coverage = { covered: Record<string, boolean>; warnings: string[] };

export type Engine = {
  id: string;
  name: string;
  version: string;
  source: string;
  description: string;
  capabilities: Record<string, unknown>;
  enabled: boolean;
  error: string | null;
};

// ── discovery ────────────────────────────────────────────────────────────

export type DiscoveredHost = {
  host: string;
  registrable: string;
  is_seed_host: boolean;
  link_refs: number;
  asset_refs: number;
  distinct_urls: number;
  role: string;
  sample_urls: string[];
  crawl_pages: boolean;
  fetch_assets: boolean;
  allow_extensionless: boolean;
};

export type DiscoverySummary = {
  seed_url: string;
  seed_host: string;
  title: string | null;
  fingerprint: {
    platform: string;
    confidence: string;
    evidence: string[];
    preset: { id: string; name: string; notes: string } | null;
  };
  robots: { fetched: boolean; sitemaps: string[]; disallowed: string[] };
  sitemaps: string[];
  feeds: string[];
  urls_from_sitemaps: number;
  urls_from_feeds: number;
  pages_fetched: number;
  errors: string[];
  warnings: string[];
};

export type DiscoveryResponse = {
  discovery: {
    id: number;
    started_at: string;
    finished_at: string | null;
    pages_fetched: number;
    urls_found: number;
    summary: DiscoverySummary;
  } | null;
  hosts: DiscoveredHost[];
  diff?: { new_hosts: string[]; gone_hosts: string[] };
  scope_user_edited?: boolean;
  message?: string;
};

export type ScopePreview = {
  pages_to_crawl: number;
  excluded_by_pattern: number;
  crawl_hosts: string[];
  asset_hosts: string[];
  estimated_bytes: number;
  estimated_seconds: number;
  notes: string[];
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

  // ── sites ──────────────────────────────────────────────────────────────
  sites: (params: Record<string, string | number | undefined> = {}) =>
    api.get<Page<Site>>(`/sites${query(params)}`),
  site: (id: number) => api.get<SiteDetail>(`/sites/${id}`),
  createSite: (body: {
    seed_url: string;
    title?: string;
    engine_id?: string;
    profile_id?: number | null;
    keep_mirror?: boolean;
    tags?: string[];
  }) => api.post<SiteDetail>("/sites", body),
  updateSite: (id: number, body: Record<string, unknown>) =>
    api.patch<SiteDetail>(`/sites/${id}`, body),
  deleteSite: (id: number) => api.del<{ ok: boolean }>(`/sites/${id}`),
  scope: (id: number) => api.get<Scope>(`/sites/${id}/scope`),
  putScope: (id: number, body: Record<string, unknown>) =>
    api.put<Scope>(`/sites/${id}/scope`, body),

  // ── captures ───────────────────────────────────────────────────────────
  startCapture: (id: number, kind = "full") =>
    api.post<{ job_id: number }>(`/sites/${id}/capture`, { kind }),
  captures: (id: number) => api.get<Capture[]>(`/sites/${id}/captures`),
  capture: (id: number) => api.get<CaptureDetail>(`/captures/${id}`),
  captureLog: (id: number, tail = 500) => api.text(`/captures/${id}/log?tail=${tail}`),
  captureUrls: (id: number, params: Record<string, string | number | undefined> = {}) =>
    api.get<Page<CaptureUrl>>(`/captures/${id}/urls${query(params)}`),
  deleteCapture: (id: number, force = false) =>
    api.del<{ ok: boolean }>(`/captures/${id}?force=${force}`),

  // ── jobs ───────────────────────────────────────────────────────────────
  jobs: (params: Record<string, string | number | undefined> = {}) =>
    api.get<Page<Job>>(`/jobs${query(params)}`),
  job: (id: number) => api.get<Job>(`/jobs/${id}`),
  cancelJob: (id: number) => api.post<{ ok: boolean }>(`/jobs/${id}/cancel`),

  // ── profiles ───────────────────────────────────────────────────────────
  profiles: () => api.get<Profile[]>("/profiles"),
  createProfile: (body: { name: string; mode?: string; user_agent?: string; notes?: string }) =>
    api.post<Profile>("/profiles", body),
  updateProfile: (id: number, body: Record<string, unknown>) =>
    api.patch<Profile>(`/profiles/${id}`, body),
  deleteProfile: (id: number) => api.del<{ ok: boolean }>(`/profiles/${id}`),
  uploadCookies: (id: number, file: File) =>
    api.upload<CookieReport>(`/profiles/${id}/cookies`, file),
  clearMaterial: (id: number) => api.del<{ ok: boolean }>(`/profiles/${id}/material`),
  coverage: (id: number, siteId: number) =>
    api.get<Coverage>(`/profiles/${id}/coverage?site_id=${siteId}`),

  // ── discovery ──────────────────────────────────────────────────────────
  discover: (id: number) => api.post<{ job_id: number }>(`/sites/${id}/discover`),
  discovery: (id: number) => api.get<DiscoveryResponse>(`/sites/${id}/discovery`),
  scopePreview: (id: number) => api.post<ScopePreview>(`/sites/${id}/scope/preview`),
  applyPreset: (id: number, preset: string) =>
    api.post<Scope>(`/sites/${id}/scope/apply-preset`, { preset }),

  // ── engines ────────────────────────────────────────────────────────────
  engines: () => api.get<Engine[]>("/engines"),
  rescanEngines: () => api.post<Engine[]>("/engines/rescan"),
};

function query(params: Record<string, string | number | undefined>): string {
  const pairs = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  return pairs.length ? `?${new URLSearchParams(pairs.map(([k, v]) => [k, String(v)]))}` : "";
}
