# 11 — Security

Covers R2. The requirement is "single user with authentication so unauthorized users can't access it when open to the internet." Login is the easy part; this application has three properties that make it a more interesting target than a typical self-hosted tool.

---

## Threat model

**What's being protected:** the archives, the stored credentials (cookie jars are credentials), the NAS the container runs on, and the user's browser session.

**Who's attacking:** internet background noise (scanners, credential stuffing), anyone who gets the URL, and — least obvious, most dangerous — **the websites being archived**.

### The three properties that shape everything

| Property | Consequence |
|---|---|
| **Replay executes untrusted JavaScript** | Every archived page contains code that runs in your browser when you view it. The site you archived is effectively a code-execution vector aimed at your session. |
| **The app fetches arbitrary URLs by design** | SSRF is a feature. Without controls, the archiver can be pointed at NAS admin interfaces, cloud metadata endpoints, or other containers on the Docker network. |
| **It stores and uses session cookies** | A jar exported from a browser routinely contains full account sessions, not just the one cookie needed. |

Each gets a section below. Standard authentication hardening follows.

---

## Replay is untrusted code execution

If replay shares an origin with the app, an archived page's JavaScript can read the session cookie, call the API as you, and exfiltrate everything. Full detail in [07](07-replay.md#replay-is-untrusted-code-execution); the requirements, restated as a checklist:

- [ ] pywb on a **separate hostname** when internet-exposed — not merely a separate port. Ports don't isolate cookies; `app.example.com:8080` and `app.example.com:8081` share a jar.
- [ ] Session cookie set with an explicit host-only `Domain`; never a shared parent like `.example.com`.
- [ ] `enable_content_security_policy: true` in pywb.
- [ ] Iframe `sandbox="allow-scripts allow-same-origin allow-forms allow-popups"` + `referrerpolicy="no-referrer"`.
- [ ] Raw WARC payloads served only as `Content-Disposition: attachment` with `Content-Type: application/octet-stream` and `X-Content-Type-Options: nosniff`. Never rendered inline on the app origin.
- [ ] Startup check: if the computed replay origin shares a hostname with the app origin, log a prominent warning.

Getting this wrong is the difference between "someone archived a compromised blog" and "someone archived a compromised blog and lost the NAS."

---

## SSRF

Every URL the app fetches — seeds, discovery targets, feeds, sitemaps, `verify_url` — is user-supplied. On a NAS, the network neighborhood is unusually rich: Unraid's own web UI, other containers, router admin pages, and any cloud metadata service.

**Controls:**

1. **Deny private ranges by default.** Block `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `169.254/16` (including `169.254.169.254`), `::1`, `fc00::/7`, `fe80::/10`, and `0.0.0.0/8`. A global setting allows private targets for people archiving an internal wiki, off by default, with an explicit warning.
2. **Resolve then check then connect.** Validating the hostname and letting the HTTP client resolve independently is a DNS-rebinding hole. Resolve to an IP, validate the IP, then connect to that IP with the `Host` header set — or use a connection hook that validates the socket's actual peer address.
3. **Validate every redirect hop.** A public URL that 302s to `169.254.169.254` defeats a check performed only on the initial URL.
4. **Schemes: `http` and `https` only.** Reject `file:`, `gopher:`, `ftp:`, `dict:`.
5. **Cap redirects** (10) and response size for non-capture fetches.

wget follows redirects itself, so the engine can't validate hop-by-hop the way the app's own fetcher can. Mitigate with `--max-redirect=10` plus the scope allowlist — wget won't fetch a host that isn't in `--domains`, which incidentally makes it much harder to redirect a crawl somewhere interesting. Note the residual risk rather than pretending it's zero.

---

## Secrets

**Cookie jars are credentials.** A "just the interstitial cookie" export frequently includes `SID`, `HSID`, `SSID`, and `__Secure-*` — full Google account sessions.

| Control | Implementation |
|---|---|
| Encrypted at rest | AES-GCM, key from `CAIRN_SECRET_KEY` via HKDF, per-record nonce |
| Never returned by the API | `GET` yields metadata only ([06](06-access-profiles.md#storage-and-api-surface)) |
| Minimal plaintext window | Materialized into the job's `temp_dir` mode `600`, deleted on job end, swept on boot |
| Not in logs | Cookie headers, `--load-cookies` contents, and userscript bodies are redacted from all logging paths |
| Not in exports | `GET /api/export/config` omits material entirely |
| Scope warnings | Upload-time detection of broad account cookies, with a nudge to export narrower ([06](06-access-profiles.md#validation-on-upload)) |

`CAIRN_SECRET_KEY` loss is unrecoverable by design. Say so at generation time, in the template description, and in the docs.

---

## Authentication

Single user, but "single user" is not a reason for weak auth — it means there's exactly one credential between the internet and everything.

| Control | Choice |
|---|---|
| Password hashing | Argon2id, ≥64 MB memory, ≥3 iterations. Not bcrypt, not PBKDF2 |
| Minimum length | 12 characters; check against a compromised-password list if one is bundled |
| Second factor | TOTP (RFC 6238), optional but prompted at setup, with one-time recovery codes |
| Sessions | 256-bit random ID, stored **hashed** so a DB read doesn't yield usable sessions |
| Cookie flags | `HttpOnly; Secure; SameSite=Lax; Path=/`; host-only `Domain` |
| Timeouts | 7-day idle, 30-day absolute; both configurable |
| Rate limiting | 5 attempts per 15 min per IP *and* per account; progressive lockout to 1 hour |
| Enumeration | One generic failure message for every cause |
| Timing | Always run the hash comparison, including for unknown usernames |
| Reauth | Password change, 2FA removal, and profile-material access require the current password |
| Revocation | Password change revokes all other sessions; a session list with per-session revoke |
| Audit | Every login, failure, lockout, and privileged action in `audit_log` with IP and UA |

### Trusted-header auth

For users fronting the app with Authelia, Authentik, or Cloudflare Access, `CAIRN_AUTH_HEADER_MODE=on` accepts an authenticated username from a header (`Remote-User`). **Only honored when the request comes from `CAIRN_TRUSTED_PROXY`.** Without that binding, anyone can send the header and walk in — which is the single most common way this feature is misconfigured. Refuse to enable it if `CAIRN_TRUSTED_PROXY` is unset.

### CSRF

`SameSite=Lax` blocks cross-site POSTs from forms, but not everything. Additionally require `X-Requested-With: XMLHttpRequest` on all mutating requests — a header simple `<form>`-based CSRF can't set and which CORS won't let a cross-origin script add without a preflight the server will reject. Set no permissive CORS headers on the app origin; the SPA is same-origin.

---

## User-supplied code

Two places accept executable input:

**Userscripts** run in headless Chromium against the target site. This is intended behavior, but the script is running inside your container. Mitigations: no `--no-sandbox` (keep Chromium's sandbox on), a hard timeout, network access permitted only to the profile's declared hosts via request interception, no filesystem access, no downloads, and a dedicated low-privilege user for the browser process.

**Engine addons** are arbitrary executables by definition. There's no meaningful sandbox for "run this program to crawl a website," so be honest: engine installation is a trust decision equivalent to installing a package. Show a clear confirmation on install, keep the Docker-socket runtime **off by default** (socket access is root on the host), and never auto-install engines from a remote registry.

---

## Input handling

| Vector | Control |
|---|---|
| Path traversal | Slugs restricted to `[a-z0-9-]`; every resolved path checked to be inside `/data` after `realpath`; symlinks resolved before validation |
| Command injection | `subprocess` with argv lists, `shell=False`, always. URLs, hosts, and regexes are user-controlled and go straight into wget's command line |
| Regex DoS | User-supplied scope regexes compiled with a timeout guard and rejected if they exhibit catastrophic backtracking on a probe string |
| Zip/archive bombs | `--quota` per capture, per-site size cap, free-space floor that aborts cleanly |
| XML entity expansion | `defusedxml` for all sitemap and feed parsing. Sitemaps are XML from untrusted sources — billion-laughs is a live risk here |
| Upload size | Capped (default 8 MB) for cookie jars and userscripts |
| SQL injection | Parameterized queries via SQLAlchemy; no string-built SQL anywhere, including in the filter builder |

The `defusedxml` point deserves emphasis. Sitemap parsing is a core code path processing attacker-controlled XML, and Python's stdlib XML parsers are vulnerable to entity-expansion attacks by default.

---

## Response headers (app origin)

```
Content-Security-Policy: default-src 'self'; script-src 'self' 'sha256-{INLINE}';
                         style-src 'self' 'unsafe-inline';
                         img-src 'self' data: blob:; frame-src {REPLAY_ORIGIN};
                         connect-src 'self'; frame-ancestors 'none'; base-uri 'none';
                         form-action 'self'; object-src 'none'
Strict-Transport-Security: max-age=31536000; includeSubDomains
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: same-origin
Permissions-Policy: geolocation=(), microphone=(), camera=(), payment=()
```

**`{INLINE}` is computed at startup from the shipped `index.html`, never hardcoded.** The page carries exactly one inline script — it applies the stored theme before first paint so a dark-mode user does not get a white flash. Under a bare `script-src 'self'` the browser refuses to run it, and this is a good example of the kind of failure a strict CSP produces: nothing errors, nothing breaks, the flash guard simply becomes dead code and a violation appears in a console nobody is reading. It shipped that way in M0 and was found in M1 only by reading the browser console during a UI check.

Hashing the file rather than pinning a constant means editing the script cannot leave the policy pointing at the previous version — which would reintroduce the same silent failure with no diff to notice. If the inline script is ever removed, the hash list is simply empty and the policy tightens on its own.

The lesson generalizes: *verify a CSP in a browser, not by reading the header*. A policy that blocks your own code looks identical to one that works.

`frame-src {REPLAY_ORIGIN}` is the only third-party frame permitted, and it's set from `CAIRN_REPLAY_PUBLIC_URL` rather than wildcarded. `frame-ancestors 'none'` prevents the app itself being framed — including by an archived page.

---

## Operational

- **Don't expose it directly if you can avoid it.** Tailscale or a Cloudflare Tunnel removes the internet-facing surface entirely; a reverse proxy with Authelia in front adds a second gate. Say this plainly in the README, because it's better advice than any amount of in-app hardening.
- **Container runs unprivileged**, drops to `PUID`/`PGID`, no `--privileged`, no Docker socket unless the user explicitly opts into container-based engines.
- **Read-only where possible.** pywb needs no write access to the archive tree; mount it read-only within the container.
- **Dependency scanning** in CI (`pip-audit`, `npm audit`, Trivy on the image). pywb and Chromium both pull large dependency trees.
- **Version pinning** in the image; `:latest` plus Unraid auto-update plus automatic migrations is a bad combination for anything you care about.
- **Log rotation** with size caps — a crawl log from a 100k-page site is large, and unbounded logs are their own availability problem.

---

## Pre-release checklist

- [ ] Replay confirmed on a separate origin; verified an archived page cannot read the session cookie
- [ ] SSRF blocklist enforced post-DNS-resolution, on every redirect hop
- [ ] Cookie jars encrypted at rest; confirmed absent from every API response, log, and export
- [ ] Argon2id with sane parameters; TOTP working with recovery codes
- [ ] Login rate limiting verified per-IP and per-account
- [ ] CSRF: mutating requests rejected without the custom header
- [ ] All `subprocess` calls use argv lists; grepped for `shell=True`
- [ ] Path traversal tested with `../`, absolute paths, symlinks, and unicode normalization tricks
- [ ] `defusedxml` used for every XML parse path
- [ ] CSP verified in a browser with no console violations
- [ ] First-run setup unreachable once a user exists
- [ ] `/api/health` leaks nothing beyond liveness and version
- [ ] Refuses to start when sealed secrets exist and the key is missing or changed
- [ ] Container runs as non-root; no Docker socket by default
