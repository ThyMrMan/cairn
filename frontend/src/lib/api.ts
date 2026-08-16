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

/** `label` is what the UI shows: "0.1.0 (a9873aa)". */
export type Version = {
  version: string;
  build: string;
  built_at: string | null;
  label: string;
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

export type FolderUsage = {
  id: number;
  path: string;
  site_count: number;
  size_bytes: number;
  total_site_count: number;
  total_size_bytes: number;
};

export type Storage = {
  data_dir: string;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  sites: number;
  archives_bytes: number;
  trash_sites: number;
  trash_bytes: number;
  folders: FolderUsage[];
  largest_sites: { id: number; title: string; path: string; size_bytes: number; url_count: number }[];
};

// ── organization ─────────────────────────────────────────────────────────

export type Folder = {
  id: number;
  parent_id: number | null;
  name: string;
  slug: string;
  path: string;
  sort_order: number;
  site_count: number;
  total_site_count: number;
  size_bytes: number;
  total_size_bytes: number;
  children: Folder[];
};

export type Tag = {
  id: number;
  name: string;
  slug: string;
  color: string | null;
  description: string | null;
  site_count: number;
};

/**
 * A move is one rename and finishes in the request — unless the two ends are
 * on different filesystems, where it is a byte copy and becomes a job. The
 * client cannot predict which, so the server says.
 */
export type MoveOutcome = {
  status: "done" | "queued";
  method: "rename" | "copy" | "noop";
  path: string;
  job_id: number | null;
};

export type SavedView = {
  id: number;
  name: string;
  query: SiteFilter;
  query_string: string;
  pinned: boolean;
};

export type TrashEntry = {
  id: number;
  slug: string;
  title: string;
  seed_url: string;
  folder_path: string;
  deleted_at: string | null;
  size_bytes: number;
  on_disk: boolean;
  purge_after_days: number | null;
};

/**
 * Mirrors `cairn.services.filters.SiteFilter` field for field. The server
 * round-trips a saved view through the same object, so anything added here
 * has to exist there under the same name or it is silently dropped.
 */
export type SiteFilter = {
  folder_id?: number;
  folder_recursive?: boolean;
  tags?: string[];
  tag_mode?: "all" | "any";
  status?: string;
  engine_id?: string;
  profile_id?: number;
  host?: string;
  has_errors?: boolean;
  never_captured?: boolean;
  last_capture_after?: string;
  last_capture_before?: string;
  size_min?: number;
  size_max?: number;
  q?: string;
  sort?: string;
};

export type BulkResult = {
  tagged: number;
  untagged: number;
  moved: number;
  queued_job_ids: number[];
  skipped: string[];
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
  has_thumbnail: boolean;
};

export type SiteDetail = Site & {
  notes: string | null;
  engine_config: Record<string, unknown>;
  scope: Scope;
  capture_count: number;
  running_job_id: number | null;
  /** Detail only — the summary would need a profile lookup per row. */
  profile_has_browser_profile: boolean;
  profile_has_cookies: boolean;
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
  /** `unit` says what `done` counts: browsertrix reports pages, wget URLs. */
  progress: {
    done?: number;
    total?: number;
    bytes?: number;
    eta_s?: number;
    unit?: string;
  } | null;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  attempts: number;
  /** Whether this job's engine can stop in a way that can be continued. Sent
   *  by the server, which knows the engine's manifest; false on wget, which
   *  has no crawl state to stop into. */
  can_pause: boolean;
};

/**
 * A browsertrix browser-profile tarball, described but never sent. Size and a
 * truncated digest are enough to answer "is one attached, and did it change?"
 */
/** What the crawler saw, in the three shapes that need different fixes. */
export type BrowserCheckResult = {
  ok: boolean;
  /** pass | gate | no_profile | error */
  verdict: string;
  reason: string;
  final_url: string;
  status: number;
  bytes: number;
  /** False with a `gate` verdict means the tarball never reached the
   *  container — a different bug from a session the site rejected. */
  profile_loaded: boolean;
  log_tail: string[];
};

export type BrowserProfileMeta = {
  size: number;
  sha256: string;
  stored_at: string;
  /** Hosts the tarball holds cookies for. Names only, never values. */
  hosts?: string[];
  /** The real total. `hosts` is capped, so its length is not the answer. */
  host_count?: number;
  cookies?: number;
  session_cookies?: number;
  /**
   * Soonest expiry per host, ISO. Per host rather than one date for the file:
   * a profile holds consent cookies expiring next week beside a sign-in that
   * lasts months, so the earliest overall means nothing. Nothing refreshes a
   * browsertrix profile automatically, so this is the only notice there is.
   */
  expiries?: Record<string, string>;
  /** False when the file could not be opened as a tarball at all. */
  readable?: boolean;
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
  has_cookies: boolean;
  has_script: boolean;
  has_storage: boolean;
  has_browser_profile: boolean;
  /** Size and digest only — never the tarball, which is a live session. */
  browser_profile: BrowserProfileMeta | null;
  /** Counts only — never a key and never a value (docs/06). */
  storage?: { cookies: number; origins: string[]; local_items: number };
  /** Present when the profile holds more than the wget engine can use. */
  storage_note?: string | null;
  script: ParsedScript | null;
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

export type ParsedScript = {
  name: string | null;
  version: string | null;
  description: string | null;
  run_at: string;
  matches: string[];
  includes: string[];
  excludes: string[];
  grants: string[];
  requires: string[];
  resources: string[];
  warnings: string[];
};

export type MintResult = {
  ok: boolean;
  reason: string;
  final_url: string;
  cookie_count: number;
  hosts: string[];
  console: string[];
  warnings: string[];
  has_screenshot: boolean;
};

export type VerifyResult = {
  ok: boolean;
  reason: string;
  status: number;
  final_url?: string;
  bytes?: number;
};

export type InteractiveSession = {
  session_id: string;
  profile_id: number;
  url: string;
  width: number;
  height: number;
};

/**
 * `enabled` is whether the manifest loaded; `available` is whether the
 * environment can actually run it. An engine needing the Docker socket on a
 * host without one is valid and unusable, and the picker has to say which.
 */
export type Engine = {
  id: string;
  name: string;
  version: string;
  source: string;
  description: string;
  capabilities: Record<string, unknown>;
  enabled: boolean;
  available: boolean;
  unavailable_reason: string | null;
  error: string | null;
};

export type EngineSchema = {
  schema: Record<string, unknown>;
  defaults: Record<string, unknown>;
};

// ── feeds ────────────────────────────────────────────────────────────────

export type Feed = {
  id: number;
  site_id: number;
  url: string;
  kind: string;
  title: string | null;
  enabled: boolean;
  auto_capture: boolean;
  recapture_on_update: boolean;
  interval_min: number;
  next_poll_at: string | null;
  last_polled_at: string | null;
  last_success_at: string | null;
  last_status: number | null;
  consecutive_failures: number;
  last_error: string | null;
  /** Set when the tool switched it off, not when a person did. */
  disabled_reason: string | null;
  counts: { seen: number; pending: number; captured: number; failed: number; skipped: number; gone: number };
  /** The capture half's backoff, separate from the poll's. A feed sitting on
   *  pending items it is not capturing looks broken without these. */
  capture_failures: number;
  next_capture_at: string | null;
};

export type FeedPoll = {
  id: number;
  ts: string;
  status: number;
  duration_ms: number;
  entries_seen: number;
  new_items: number;
  gone_items: number;
  action: string;
  error: string | null;
};

export type FeedItem = {
  id: number;
  url: string;
  title: string | null;
  status: string;
  published_at: string | null;
  first_seen_at: string;
  gone_at: string | null;
  capture_id: number | null;
};

/**
 * What the add-feed dialog shows before anything is saved. `in_scope` is the
 * point of testing first: a feed whose entries fall outside the site's scope
 * polls happily forever and archives none of them.
 */
export type FeedCandidate = {
  url: string;
  kind: string;
  title: string | null;
  entry_count: number;
  recent_titles: string[];
  is_comments: boolean;
  in_scope: number;
  out_of_scope: string[];
  error: string | null;
  ok: boolean;
};

export type FeedPollResult = {
  status: number;
  action: string;
  entries_seen: number;
  new_items: number;
  gone_items: number;
  /** The first poll of a feed records what already exists and captures none of it. */
  baseline: boolean;
  error: string | null;
  job_ids: number[];
};

export type ScheduleSettings = {
  quiet_hours: { enabled: boolean; start: string; end: string };
  per_host_serial: boolean;
  full_recapture_days: number;
  in_quiet_hours_now: boolean;
  digest_every_days: number;
};

/** What a copy of the archive has, and what it is missing. */
export type MirrorSurvey = {
  root: string;
  captures: number;
  present: number;
  missing: number;
  complete: boolean;
  complete_sites: number;
  sites: {
    site_id: number;
    title: string;
    archive_path: string;
    captures: number;
    present: number;
    missing: string[];
    complete: boolean;
  }[];
  unknown_dirs: string[];
};

/** Whether the live sites behind the archives are still there. */
export type SiteHealthProblem = {
  site_id: number;
  title: string;
  state: string;
  http_status: number | null;
  final_url: string | null;
  error: string | null;
  since: string | null;
  checked_at: string | null;
};

export type SiteHealthSummary = {
  counts: Record<string, number>;
  problems: SiteHealthProblem[];
  checked: number;
};

export type SiteHealthCheck = {
  state: string;
  changed: string | null;
  http_status: number | null;
  final_url: string | null;
  error: string | null;
  message: string;
  health: {
    state: string;
    since: string | null;
    checked_at: string | null;
    pending_state: string | null;
  } | null;
};

/** The periodic report: what happened, and what quietly did not. */
export type DigestReport = {
  since: string;
  until: string;
  days: number;
  sites: number;
  captures: { ok: number; partial: number; failed: number };
  urls_archived: number;
  bytes_archived: number;
  new_items: number;
  failed_jobs: {
    job_id: number;
    type: string;
    site: string | null;
    error: string;
    finished_at: string | null;
  }[];
  quiet_sites: { site_id: number; title: string; last_capture_at: string | null; days: number }[];
  stalled_feeds: {
    feed_id: number;
    url: string;
    site_id: number;
    site: string;
    last_entry_at: string | null;
  }[];
  expiring_profiles: {
    profile_id: number;
    name: string;
    mode: string;
    expires_at: string | null;
    expired: boolean;
  }[];
  vanished_sites: {
    site_id: number;
    title: string;
    state: string;
    http_status: number | null;
    final_url: string | null;
    since: string | null;
  }[];
  integrity: {
    captures: number;
    verified: number;
    oldest_unverified: { site_title: string } | null;
    last_run_at: string | null;
    findings: number;
    due: boolean;
  };
  total_bytes: number;
  growth_bytes: number | null;
  has_problems: boolean;
  text: string;
};

export type NotifyTarget = { url: string; enabled: boolean; label: string };

export type NotifySettings = {
  targets: NotifyTarget[];
  events: Record<string, boolean>;
  labels: Record<string, string>;
  apprise_available: boolean;
};

// ── replay ───────────────────────────────────────────────────────────────

export type ReplayStatus = {
  collection: string;
  records: number;
  /** Whether anything here is a page somebody could open, rather than a
   *  redirect or an error. A boolean rather than the count it used to be: the
   *  exact number cost a full parse of the index on every open of the tab. */
  has_pages: boolean;
  indexed_at: number | null;
  origin: string;
  /** Empty when the origin could not be determined; the tab says so. */
  base_url: string;
  seed_url: string;
  shares_host_with_app: boolean;
  /** The replay port is a guess: this deployment remaps ports and never said
   *  which one replay is published on. A wrong guess is a blank iframe. */
  port_is_assumed: boolean;
  replay_port: number;
};

export type CdxVersion = {
  timestamp: string;
  url: string;
  mime: string | null;
  status: string | null;
  digest: string | null;
  filename: string;
  offset: number;
  length: number;
};

export type RecordDetail = CdxVersion & {
  record_type: string;
  http_status: string | null;
  http_headers: Record<string, string>;
  warc_headers: Record<string, string>;
};

/** Where a site starts from. One entry unless it spans domains. */
export type SeedList = { primary: string; seeds: string[]; max: number };

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

export type PlatformPreset = { id: string; name: string; notes: string };

export type DiscoverySummary = {
  seed_url: string;
  seed_host: string;
  title: string | null;
  fingerprint: {
    platform: string;
    confidence: string;
    evidence: string[];
    preset: PlatformPreset | null;
    /** Variants of the detected preset, offered beside it so the two can be
     * compared on the same site. Absent on discoveries recorded before
     * variants existed. */
    alternatives?: PlatformPreset[];
  };
  robots: { fetched: boolean; sitemaps: string[]; disallowed: string[] };
  sitemaps: string[];
  feeds: string[];
  urls_from_sitemaps: number;
  urls_from_feeds: number;
  pages_fetched: number;
  browser?: {
    rendered_pages: number;
    requests_seen: number;
    hosts_only_a_browser_saw: string[];
  };
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
  /** Whether this install can render pages, and why not when it cannot. */
  browser?: { available: boolean; reason: string };
  message?: string;
};

// ── the reader view ──────────────────────────────────────────────────────

export type ReaderBlock = { kind: string; text: string };

export type ReaderArticle = {
  url: string;
  title: string;
  timestamp: string;
  capture_dir: string;
  capture_id: number | null;
  words: number;
  minutes: number;
  blocks: ReaderBlock[];
  annotations: Annotation[];
};

/** A note or highlight, placed against the article it was asked about. */
export type Annotation = {
  id: number;
  site_id: number;
  url: string;
  quote: string;
  note: string | null;
  color: string;
  created_at: string | null;
  updated_at: string | null;
  block_index: number;
  start: number;
  end: number;
  /** False when the quoted sentence is not in this capture any more. */
  found: boolean;
};

export type ReaderVersion = {
  capture_id: number | null;
  capture_dir: string;
  started_at: string | null;
  timestamp: string;
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

export type JobAccepted = { job_id: number };

// ── search, exports, integrity ───────────────────────────────────────────

export type SearchHit = {
  site_id: number;
  site_title: string;
  site_slug: string;
  folder_path: string;
  url: string;
  title: string;
  snippets: string[];
  score: number;
  capture_id: number | null;
  /** 14-digit CDXJ timestamp — what replay wants to open this version. */
  timestamp: string;
  words: number;
};

export type SearchResults = {
  query: string;
  terms: string[];
  total: number;
  hits: SearchHit[];
  truncated: boolean;
};

export type SearchStatus = {
  /** Pages in the full-text index — unrelated to the replay index's count. */
  pages: number;
  words: number;
  sites: number;
  unindexed_sites: number[];
};

export type ExportEntry = { name: string; size_bytes: number; created_at: string };

/** A pasted list of URLs, grouped into the sites it would become. */
export type UrlImportGroup = {
  key: string;
  origin: string;
  hosts: string[];
  urls: string[];
  url_count: number;
  site_id: number | null;
  site_title: string | null;
  is_new: boolean;
};

export type UrlImportSurvey = {
  found: number;
  groups: UrlImportGroup[];
  new_sites: number;
  existing_sites: number;
  skipped: string[];
  skipped_count: number;
};

export type UrlImportResult = {
  created: number[];
  updated: number[];
  jobs: number[];
  urls: number;
  errors: string[];
};

export type ArchiveBoxSurvey = {
  version: string;
  snapshots: number;
  with_warcs: number;
  without_warcs: number;
  warc_bytes: number;
  hosts: Record<string, number>;
  tags: string[];
  problems: string[];
};

// ── diffs and retention ──────────────────────────────────────────────────

export type PageSummary = {
  url: string;
  title: string;
  kind: "added" | "removed" | "changed" | "unchanged";
  change_ratio: number;
};

export type CaptureDiff = {
  before_capture: string;
  after_capture: string;
  before_capture_id: number;
  after_capture_id: number;
  added: number;
  removed: number;
  changed: number;
  unchanged: number;
  pages: PageSummary[];
  note: string;
};

export type WordEdit = { kind: string; before: string; after: string };

export type BlockChange = {
  kind: "added" | "removed" | "changed";
  before: string;
  after: string;
  words: WordEdit[];
};

export type PageDiff = {
  url: string;
  before_capture: string;
  after_capture: string;
  before_title: string;
  after_title: string;
  changed: boolean;
  change_ratio: number;
  blocks: BlockChange[];
  note: string;
};

export type ResourceChange = {
  kind: "added" | "removed" | "changed";
  url: string;
  mime: string;
  before_digest: string;
  after_digest: string;
};

export type RetentionDecision = {
  capture_id: number;
  dir_name: string;
  started_at: string;
  size_bytes: number;
  keep: boolean;
  reason: string;
  detail: string;
};

export type RetentionPlan = {
  site_id: number;
  site_title: string;
  policy: {
    enabled: boolean;
    keep_last: number;
    keep_monthly: number;
    min_age_days: number;
  };
  captures: RetentionDecision[];
  prunable: number;
  freed_bytes: number;
};

/** All optional: what is not set falls back to the instance default. */
export type MediaPolicy = {
  enabled?: boolean;
  max_item_bytes?: number;
  max_total_bytes?: number;
  max_items?: number;
  format?: string;
  allow_private_hosts?: boolean;
};

export type MediaItem = {
  url: string;
  status: "downloaded" | "skipped" | "failed";
  reason: string;
  filename: string;
  bytes: number;
  title: string;
  capture_id: number;
  capture_dir: string;
  /** The file is still on disk and has a type the server will serve. */
  playable: boolean;
};

export type CrawlProjection = {
  /** What the live counter counts; the index always counts pages. */
  counts?: string;
  index_counts?: string;
  /** Robots-disallowed paths in scope because robots.txt is off. */
  unlisted_paths?: string[];
  running: boolean;
  urls: number;
  bytes: number;
  elapsed_s: number;
  per_minute: number;
  max_pages: number | null;
  remaining_to_cap: number | null;
  eta_to_cap_s: number | null;
  /** Pages the index expected — a different quantity from `urls`, on purpose. */
  index_estimate: number | null;
};

export type UrlShape = {
  /** The path with varying segments replaced, and the query reduced to keys. */
  shape: string;
  count: number;
  bytes: number;
  example: string;
};

export type UrlShapes = {
  total: number;
  distinct_shapes: number;
  shapes: UrlShape[];
  truncated: boolean;
};

/** The instance-wide default, with no site's override on top. */
export type MediaDefaults = {
  policy: Required<MediaPolicy>;
  override: MediaPolicy;
  available: boolean;
  unavailable_reason: string;
  hosts: string[];
};

export type MediaSettings = MediaDefaults & {
  /** Already merged: built-in under instance setting under site override. */
  policy: Required<MediaPolicy>;
  /** What the instance default contributes, so the UI can label inheritance. */
  instance: MediaPolicy;
  /** Only the fields this site overrides, which is what the form edits. */
  override: MediaPolicy;
  items: MediaItem[];
  total_bytes: number;
};

export type IntegrityFinding = {
  kind: string;
  site_id: number;
  site_title: string;
  capture_id: number | null;
  capture_dir: string;
  file: string;
  detail: string;
  severity: number;
};

export type IntegrityHealth = {
  captures: number;
  verified: number;
  oldest_unverified: {
    capture_id: number;
    site_id: number;
    site_title: string;
    dir_name: string;
    started_at: string;
  } | null;
  last_run: {
    started_at: string;
    finished_at: string | null;
    sites: number;
    captures: number;
    files: number;
    bytes_read: number;
    ok: boolean;
    findings: IntegrityFinding[];
  } | null;
  due: boolean;
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
  digest: (days = 7) => api.get<DigestReport>(`/digest?days=${days}`),
  siteHealth: () => api.get<SiteHealthSummary>("/site-health"),
  surveyMirror: (path: string) =>
    api.get<MirrorSurvey>(`/mirror?path=${encodeURIComponent(path)}`),
  verifyMirror: (path: string, deep = false) =>
    api.post<{ job_id: number }>(
      `/mirror/verify?path=${encodeURIComponent(path)}&deep=${deep}`,
    ),
  checkSiteHealth: (id: number) =>
    api.post<SiteHealthCheck>(`/sites/${id}/health-check`),
  version: () => api.get<Version>("/version"),

  // ── organization ───────────────────────────────────────────────────────
  folders: () => api.get<Folder[]>("/folders"),
  createFolder: (body: { name: string; parent_id?: number | null }) =>
    api.post<Folder>("/folders", body),
  renameFolder: (id: number, name: string) =>
    api.patch<MoveOutcome>(`/folders/${id}`, { name }),
  reparentFolder: (id: number, parentId: number | null) =>
    api.patch<MoveOutcome>(`/folders/${id}`, { parent_id: parentId, reparent: true }),
  deleteFolder: (id: number, reassignTo?: number) =>
    api.del<{ ok: boolean }>(
      `/folders/${id}${reassignTo != null ? `?reassign_to=${reassignTo}` : ""}`,
    ),

  tags: () => api.get<Tag[]>("/tags"),
  createTag: (body: { name: string; color?: string | null; description?: string | null }) =>
    api.post<Tag>("/tags", body),
  updateTag: (id: number, body: Record<string, unknown>) => api.patch<Tag>(`/tags/${id}`, body),
  deleteTag: (id: number) => api.del<{ ok: boolean }>(`/tags/${id}`),

  views: () => api.get<SavedView[]>("/views"),
  createView: (body: { name: string; query: SiteFilter; pinned?: boolean }) =>
    api.post<SavedView>("/views", body),
  updateView: (id: number, body: Record<string, unknown>) =>
    api.patch<SavedView>(`/views/${id}`, body),
  deleteView: (id: number) => api.del<{ ok: boolean }>(`/views/${id}`),

  trash: () => api.get<TrashEntry[]>("/trash"),
  emptyTrash: () => api.del<{ ok: boolean }>("/trash"),
  purgeTrash: () => api.post<{ purged: number; freed_bytes: number }>("/maintenance/purge-trash"),
  rebuildSymlinks: () =>
    api.post<{ linked: number; removed: number }>("/maintenance/rebuild-symlinks"),
  rebuildCollections: () =>
    api.post<{ linked: number; removed: number }>("/maintenance/rebuild-collections"),
  verifyArchive: (params: { site_id?: number; deep?: boolean } = {}) =>
    api.post<JobAccepted>(`/maintenance/verify${query(params as never)}`),
  integrity: () => api.get<IntegrityHealth>("/maintenance/integrity"),
  rebuildThumbnails: (params: { site_id?: number; force?: boolean } = {}) =>
    api.post<JobAccepted>(`/maintenance/thumbnails${query(params as never)}`),
  thumbnailSettings: () => api.get<{ enabled: boolean }>("/thumbnails/settings"),
  putThumbnailSettings: (body: { enabled: boolean }) =>
    api.put<{ enabled: boolean }>("/thumbnails/settings", body),
  // Not fetched through `api` — it goes in an <img src>, which carries the
  // session cookie itself and cannot carry the CSRF header the JSON client
  // adds. Safe because it is a GET that changes nothing.
  thumbnailUrl: (siteId: number) => `/api/sites/${siteId}/thumbnail`,

  // ── search ─────────────────────────────────────────────────────────────
  search: (params: {
    q: string;
    site_id?: number;
    folder?: string;
    tag?: string;
    limit?: number;
    offset?: number;
  }) => api.get<SearchResults>(`/search${query(params as never)}`),
  searchStatus: () => api.get<SearchStatus>("/search/status"),
  reindexSearch: (params: { site_id?: number; extract?: boolean } = {}) =>
    api.post<JobAccepted>(`/maintenance/reindex-search${query(params as never)}`),

  // ── exports ────────────────────────────────────────────────────────────
  exports: (siteId: number) => api.get<ExportEntry[]>(`/sites/${siteId}/exports`),
  exportSite: (siteId: number) => api.post<JobAccepted>(`/sites/${siteId}/export/wacz`),
  exportCapture: (captureId: number) =>
    api.post<JobAccepted>(`/captures/${captureId}/export/wacz`),
  deleteExport: (siteId: number, name: string) =>
    api.del<{ ok: boolean }>(`/sites/${siteId}/exports/${encodeURIComponent(name)}`),
  verifyExport: (siteId: number, name: string) =>
    api.get<{ ok: boolean; problems: string[]; records: number; resources: number }>(
      `/sites/${siteId}/exports/${encodeURIComponent(name)}/verify`,
    ),
  exportUrl: (siteId: number, name: string) =>
    `/api/sites/${siteId}/exports/${encodeURIComponent(name)}`,

  // ── diffs and retention ────────────────────────────────────────────────
  diffCaptures: (siteId: number, params: { before?: number; after?: number } = {}) =>
    api.get<CaptureDiff>(`/sites/${siteId}/diff${query(params as never)}`),
  diffPage: (siteId: number, params: { url: string; before?: number; after?: number }) =>
    api.get<PageDiff>(`/sites/${siteId}/diff/page${query(params as never)}`),
  diffResources: (siteId: number, params: { before?: number; after?: number } = {}) =>
    api.get<{ before_capture: string; after_capture: string; resources: ResourceChange[] }>(
      `/sites/${siteId}/diff/resources${query(params as never)}`,
    ),
  retention: (siteId: number) => api.get<RetentionPlan>(`/sites/${siteId}/retention`),

  // ── embedded media ─────────────────────────────────────────────────────
  media: (siteId: number) => api.get<MediaSettings>(`/sites/${siteId}/media`),
  putMedia: (siteId: number, body: MediaPolicy) =>
    api.put<MediaSettings>(`/sites/${siteId}/media`, body),
  /** The instance default a site inherits when it sets nothing of its own. */
  mediaDefaults: () => api.get<MediaDefaults>("/media/settings"),
  putMediaDefaults: (body: MediaPolicy) => api.put<MediaDefaults>("/media/settings", body),
  /** Playable straight from a <video>/<audio> element; it answers Range. */
  mediaUrl: (siteId: number, captureDir: string, filename: string) =>
    `/api/sites/${siteId}/media/${encodeURIComponent(captureDir)}/${encodeURIComponent(filename)}`,

  // ── importing ──────────────────────────────────────────────────────────
  metricsSettings: () =>
    api.get<{ enabled: boolean; token_set: boolean }>("/metrics/settings"),
  putMetricsSettings: (body: { enabled?: boolean; token?: string }) =>
    api.put<{ enabled: boolean; token_set: boolean }>("/metrics/settings", body),
  surveyUrls: (text: string) => api.post<UrlImportSurvey>("/import/urls/survey", { text }),
  importUrls: (text: string, options: { capture?: boolean; crawl?: boolean } = {}) =>
    api.post<UrlImportResult>("/import/urls", { text, ...options }),

  surveyArchiveBox: (path: string) =>
    api.get<ArchiveBoxSurvey>(`/import/archivebox${query({ path })}`),
  importArchiveBox: (path: string, hosts: string[] = []) =>
    api.post<JobAccepted>(
      `/import/archivebox?${new URLSearchParams([
        ["path", path],
        ...hosts.map((h) => ["host", h] as [string, string]),
      ])}`,
    ),
  putRetention: (siteId: number, body: Record<string, unknown>) =>
    api.put<RetentionPlan>(`/sites/${siteId}/retention`, body),
  applyRetention: (siteId: number) =>
    api.post<JobAccepted>(`/sites/${siteId}/retention/apply`),

  // ── sites ──────────────────────────────────────────────────────────────
  sites: (params: Record<string, string | number | undefined> = {}) =>
    api.get<Page<Site>>(`/sites${query(params)}`),
  /** Filtered listing. `filterToQuery` produces exactly what a saved view stores. */
  filterSites: (filter: SiteFilter, page = 1, perPage = 50) =>
    api.get<Page<Site>>(`/sites?${filterToQuery(filter, { page, per_page: perPage })}`),
  moveSite: (id: number, folderId: number) =>
    api.post<MoveOutcome>(`/sites/${id}/move`, { folder_id: folderId }),
  restoreSite: (id: number) => api.post<SiteDetail>(`/sites/${id}/restore`),
  purgeSite: (id: number) => api.del<{ ok: boolean }>(`/sites/${id}?purge=true`),
  bulkSites: (body: {
    site_ids: number[];
    add_tags?: string[];
    remove_tags?: string[];
    folder_id?: number | null;
  }) => api.post<BulkResult>("/sites/bulk", body),
  site: (id: number) => api.get<SiteDetail>(`/sites/${id}`),
  createSite: (body: {
    seed_url: string;
    title?: string;
    folder_id?: number;
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
  /** What a capture is fetching, grouped by URL shape. Works mid-crawl. */
  captureUrlShapes: (id: number, limit = 30) =>
    api.get<UrlShapes>(`/captures/${id}/url-shapes${query({ limit })}`),
  deleteCapture: (id: number, force = false) =>
    api.del<{ ok: boolean }>(`/captures/${id}?force=${force}`),

  // ── jobs ───────────────────────────────────────────────────────────────
  jobs: (params: Record<string, string | number | undefined> = {}) =>
    api.get<Page<Job>>(`/jobs${query(params)}`),
  job: (id: number) => api.get<Job>(`/jobs/${id}`),
  /** Rate and distance-to-cap for a running crawl. No invented percentage. */
  jobProjection: (id: number) => api.get<CrawlProjection>(`/jobs/${id}/projection`),
  cancelJob: (id: number) => api.post<{ ok: boolean }>(`/jobs/${id}/cancel`),
  /** Stop a running capture so it can be continued. Only offered on an engine
   *  whose manifest declares `resumable`; 409s otherwise rather than quietly
   *  behaving like cancel. */
  pauseJob: (id: number) => api.post<{ ok: boolean }>(`/jobs/${id}/pause`),
  resumeCapture: (id: number) => api.post<{ job_id: number }>(`/captures/${id}/resume`),
  deleteJob: (id: number) => api.del<{ ok: boolean }>(`/jobs/${id}`),
  /** Deletes finished jobs only; queued and running ones are never touched. */
  clearJobs: (body: { status?: string; type?: string; site_id?: number }) =>
    api.post<{ deleted: number }>("/jobs/clear", body),

  // ── profiles ───────────────────────────────────────────────────────────
  profiles: () => api.get<Profile[]>("/profiles"),
  createProfile: (body: {
    name: string;
    mode?: string;
    verify_url?: string;
    user_agent?: string;
    notes?: string;
  }) => api.post<Profile>("/profiles", body),
  updateProfile: (id: number, body: Record<string, unknown>) =>
    api.patch<Profile>(`/profiles/${id}`, body),
  deleteProfile: (id: number) => api.del<{ ok: boolean }>(`/profiles/${id}`),
  uploadCookies: (id: number, file: File) =>
    api.upload<CookieReport>(`/profiles/${id}/cookies`, file),
  uploadScript: (id: number, file: File) =>
    api.upload<{ script: ParsedScript; profile: Profile }>(`/profiles/${id}/script`, file),
  mintProfile: (id: number) =>
    api.post<{ result: MintResult; profile: Profile }>(`/profiles/${id}/mint`),
  verifyProfile: (id: number) => api.post<VerifyResult>(`/profiles/${id}/verify`),
  /** The browser-profile counterpart: starts the real crawler, because a
   *  browsertrix profile is a Brave user-data-dir only that browser can
   *  decrypt. Slow — it boots Chromium — so it is a button, not a poll. */
  verifyBrowserProfile: (id: number) =>
    api.post<BrowserCheckResult>(`/profiles/${id}/verify-browser-profile`),
  clearMaterial: (id: number) => api.del<{ ok: boolean }>(`/profiles/${id}/material`),
  uploadBrowserProfile: (id: number, file: File) =>
    api.upload<{ browser_profile: BrowserProfileMeta; profile: Profile }>(
      `/profiles/${id}/browser-profile`,
      file,
    ),
  clearBrowserProfile: (id: number) =>
    api.del<{ ok: boolean }>(`/profiles/${id}/browser-profile`),

  // ── interactive ────────────────────────────────────────────────────────
  startInteractive: (id: number, url?: string) =>
    api.post<InteractiveSession>(`/profiles/${id}/interactive`, { url: url || null }),
  saveInteractive: (id: number) =>
    api.post<{ cookie_count: number; hosts_covered: string[]; warnings: string[] }>(
      `/profiles/${id}/interactive/save`,
    ),
  stopInteractive: (id: number) => api.del<{ ok: boolean }>(`/profiles/${id}/interactive`),
  interactiveSocketUrl: (id: number, sessionId: string) => {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    return `${scheme}://${window.location.host}/api/profiles/${id}/interactive/ws?session_id=${sessionId}`;
  },
  coverage: (id: number, siteId: number) =>
    api.get<Coverage>(`/profiles/${id}/coverage?site_id=${siteId}`),

  // ── discovery ──────────────────────────────────────────────────────────
  // ── seeds ──────────────────────────────────────────────────────────────
  seeds: (id: number) => api.get<SeedList>(`/sites/${id}/seeds`),
  addSeed: (id: number, url: string) =>
    api.post<SeedList & { added: string; note: string }>(`/sites/${id}/seeds`, { url }),
  removeSeed: (id: number, url: string) =>
    api.del<{ ok: boolean }>(`/sites/${id}/seeds?url=${encodeURIComponent(url)}`),

  discover: (id: number, useBrowser = false) =>
    api.post<{ job_id: number }>(`/sites/${id}/discover?use_browser=${useBrowser}`),
  discovery: (id: number) => api.get<DiscoveryResponse>(`/sites/${id}/discovery`),
  scopePreview: (id: number) => api.post<ScopePreview>(`/sites/${id}/scope/preview`),
  applyPreset: (id: number, preset: string) =>
    api.post<Scope>(`/sites/${id}/scope/apply-preset`, { preset }),

  // ── replay ─────────────────────────────────────────────────────────────
  replayStatus: (id: number) => api.get<ReplayStatus>(`/sites/${id}/replay`),
  replayVersions: (id: number, url: string) =>
    api.get<{ url: string; count: number; versions: CdxVersion[] }>(
      `/sites/${id}/replay/versions?url=${encodeURIComponent(url)}`,
    ),
  replayRecord: (id: number, url: string, timestamp?: string) =>
    api.get<RecordDetail>(
      `/sites/${id}/replay/record?url=${encodeURIComponent(url)}` +
        (timestamp ? `&timestamp=${timestamp}` : ""),
    ),
  reindex: (id: number) => api.post<{ records: number; warcs: number }>(`/sites/${id}/reindex`),

  // ── the reader view ────────────────────────────────────────────────────
  readerPage: (id: number, url: string, capture?: string) =>
    api.get<ReaderArticle>(
      `/sites/${id}/reader?url=${encodeURIComponent(url)}` +
        (capture ? `&capture=${encodeURIComponent(capture)}` : ""),
    ),
  annotations: (id: number, url?: string) =>
    api.get<{ annotations: Annotation[] }>(
      `/sites/${id}/annotations${url ? `?url=${encodeURIComponent(url)}` : ""}`,
    ),
  addAnnotation: (
    id: number,
    body: {
      url: string;
      quote: string;
      note?: string;
      prefix?: string;
      suffix?: string;
      block_index?: number;
      color?: string;
    },
  ) => api.post<Annotation>(`/sites/${id}/annotations`, body),
  editAnnotation: (annotationId: number, body: { note?: string; color?: string }) =>
    api.patch<Annotation>(`/annotations/${annotationId}`, body),
  deleteAnnotation: (annotationId: number) =>
    api.del<{ ok: boolean }>(`/annotations/${annotationId}`),

  readerVersions: (id: number, url: string) =>
    api.get<{ url: string; versions: ReaderVersion[] }>(
      `/sites/${id}/reader/versions?url=${encodeURIComponent(url)}`,
    ),

  // ── feeds ──────────────────────────────────────────────────────────────
  feeds: (siteId: number) => api.get<Feed[]>(`/sites/${siteId}/feeds`),
  addFeed: (
    siteId: number,
    body: {
      url: string;
      kind?: string;
      title?: string | null;
      interval_min?: number | null;
      enabled?: boolean;
      auto_capture?: boolean;
    },
  ) => api.post<Feed>(`/sites/${siteId}/feeds`, body),
  discoverFeeds: (siteId: number) =>
    api.post<FeedCandidate[]>(`/sites/${siteId}/feeds/discover`),
  testFeed: (siteId: number, url: string, kind = "auto") =>
    api.post<FeedCandidate>(`/sites/${siteId}/feeds/test`, { url, kind }),
  updateFeed: (id: number, body: Record<string, unknown>) =>
    api.patch<Feed>(`/feeds/${id}`, body),
  deleteFeed: (id: number) => api.del<{ ok: boolean }>(`/feeds/${id}`),
  pollFeed: (id: number) => api.post<FeedPollResult>(`/feeds/${id}/poll`),
  captureFeed: (id: number) => api.post<FeedPollResult>(`/feeds/${id}/capture`),
  feedPolls: (id: number, limit = 25) =>
    api.get<FeedPoll[]>(`/feeds/${id}/polls?limit=${limit}`),
  feedItems: (id: number, params: { status?: string; limit?: number } = {}) =>
    api.get<FeedItem[]>(`/feeds/${id}/items${query(params)}`),

  // ── scheduling & notifications ─────────────────────────────────────────
  schedule: () => api.get<ScheduleSettings>("/schedule"),
  putSchedule: (body: Omit<ScheduleSettings, "in_quiet_hours_now">) =>
    api.put<ScheduleSettings>("/schedule", { ...body, in_quiet_hours_now: false }),
  notifications: () => api.get<NotifySettings>("/notifications"),
  putNotifications: (body: { targets?: NotifyTarget[]; events?: Record<string, boolean> }) =>
    api.put<NotifySettings>("/notifications", body),
  testNotifications: () =>
    api.post<{ targets: number; delivered: number; problems: string[] }>("/notifications/test"),

  // ── engines ────────────────────────────────────────────────────────────
  engines: () => api.get<Engine[]>("/engines"),
  engineSchema: (id: string) => api.get<EngineSchema>(`/engines/${id}/schema`),
  rescanEngines: () => api.post<Engine[]>("/engines/rescan"),
};

function query(params: Record<string, string | number | undefined>): string {
  const pairs = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  return pairs.length ? `?${new URLSearchParams(pairs.map(([k, v]) => [k, String(v)]))}` : "";
}

/**
 * Serialize a filter the way the server reads one.
 *
 * `tags` becomes repeated `tag` parameters, which is the one place the wire
 * name differs from the field name — the server accepts both `tag` repeated
 * and `tags` comma-separated, and this picks the form that cannot be confused
 * by a tag containing a comma.
 */
export function filterToQuery(
  filter: SiteFilter,
  extra: Record<string, string | number | undefined> = {},
): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filter)) {
    if (value === undefined || value === "" || (Array.isArray(value) && !value.length)) continue;
    if (key === "tags") for (const tag of value as string[]) params.append("tag", tag);
    else params.set(key, String(value));
  }
  for (const [key, value] of Object.entries(extra)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  return params.toString();
}
