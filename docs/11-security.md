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

**As built, honestly.** Control 1 is enforced in exactly one place: the media post-processor, which is the only thing here that fetches a URL nobody typed. A seed, a feed, a sitemap and a `verify_url` are all URLs the single user of a single-user tool entered, and blocking private ranges there would break archiving an internal wiki — the same reasoning the notification section below already applies to webhooks. Media URLs are different in kind: they come out of archived HTML somebody else wrote. Every address a media host resolves to is checked, not just the first, and a name that does not resolve is refused rather than attempted. `allow_private_hosts` lifts it per site for anyone archiving a video on their own LAN.

Controls 2 and 3 — resolve-then-connect, and validating each redirect hop — are **not** built. The residual risk is DNS rebinding and a public URL redirecting into private space, and it is written here rather than left implied.

wget follows redirects itself, so the engine can't validate hop-by-hop the way the app's own fetcher can. Mitigate with `--max-redirect=10` plus the scope allowlist — wget won't fetch a host that isn't in `--domains`, which incidentally makes it much harder to redirect a crawl somewhere interesting. Note the residual risk rather than pretending it's zero.

### The Docker socket is root on the host

A container engine needs `/var/run/docker.sock` mounted into cairn. **That grants root-equivalent control of the machine** — anything that can talk to the daemon can start a privileged container that mounts `/`. It is not a partial privilege and there is no way to scope it down from inside.

So it is opt-in by omission: the socket is not mounted by default, nothing asks for it, and the engine picker says plainly that the engine is unavailable without it rather than failing at capture time.

**Mounting it is not the same as being able to open it,** and the preflight used to conflate the two. On Unraid the socket is group-owned by `docker` on the host, that group does not exist in this image, and the template runs as 99:100 — so the mount succeeded, the check passed, and the capture died several frames deep in httpx with `[Errno 13] Permission denied`. The check now tests access as well as existence, and distinguishes a socket that is absent from one it is not allowed to stat: `Path.exists()` catches `OSError` broadly, so a permission failure on the path reported as "not mounted" and sent people to fix a mount that was already correct.

**`--group-add` on the container does not reach the application, and this cost a full round trip to find.** It adds the gid to PID 1's supplementary groups, but this image drops privileges with `s6-setuidgid abc`, which rebuilds the group list from `/etc/group` for that user — so a gid Docker injected is discarded. Measured in the shipped image: PID 1 has `groups=0(root),281`, and `s6-setuidgid abc id` reports `groups=1000(abc)`. The correct-looking fix does nothing, which is the worst kind.

So `init-perms` does it properly: if the socket is present it reads the gid, creates a group with it if `/etc/group` has none, and adds `abc` to it. The kernel checks numeric gids, so the name does not matter and `--group-add` is not needed at all. **One case is deliberately left to a human** — a socket group-owned by root (gid 0), where joining automatically would grant far more than socket access. `chgrp` it to a docker group on the host instead.

**This is a real grant, not a formality:** whichever way it is arranged, reaching that socket is the root-equivalent access described above. `jobs.allow_docker` exists so an operator who mounted the socket for something else can still forbid engines from using it; it defaults to on, because mounting the socket is already the deliberate act and a second switch defaulting to off would mean somebody does the work and is told the feature is unavailable with no hint why.

What cairn does with it, once granted:

- **Two directories, chosen by cairn.** The engine container gets the capture's output directory and the job's temp directory, and nothing else — not `/config`, which holds the database and the master key, and not the socket. Verified by probe: `/config` is not visible from inside an engine container.
- **No privilege escalation.** `Privileged: false`, `no-new-privileges`, no added capabilities, and the socket is never passed on.
- **Every container is labelled and swept.** `cairn.managed=true` plus the job id, so a process killed mid-capture does not leave a crawler running against somebody's site indefinitely. Swept at boot.

None of that changes the first sentence. An engine image is third-party code running on a daemon that can do anything; the containment above limits what it reaches *by accident*, not what a hostile image could do. Install engines you trust, or leave the socket unmounted and use the ones in the image.

### The metrics endpoint is unauthenticated by design

A Prometheus scraper cannot log in, so `/api/metrics` is off by default and open when on. What makes that safe is the rule it is built around: **no names**. Not a site title, not a URL, not a host, not a folder, not a tag — only counts and durations, with labels drawn from fixed vocabularies (job types, statuses). An exporter is scraped by something that stores forever and is often reachable more widely than the app, and "which sites does this person archive" is the most sensitive thing this application holds that is not a credential. `metrics.token` adds a bearer token for anyone who wants one; the API reports whether a token is set and never what it is.

### Importing reads somebody else's database

The ArchiveBox importer opens `index.sqlite3` **read-only** (`file:…?mode=ro`) and copies WARCs rather than moving them, so a botched import cannot damage the archive it read. It deliberately does not read `ArchiveBox.conf`: a real 0.7.4 writes its Django `SECRET_KEY` there and no version at all, so reading it would mean handling somebody's secret to learn nothing.

**Notification targets are outbound requests to a user-supplied URL, and they are not SSRF.** A webhook pointed at `http://192.168.1.1/` is the single user of a single-user tool choosing to POST to their own router, which they could do from a shell on the same box. Two things do apply: the request carries no credentials and no archive content beyond the notification text, and the HTTP clients run with `trust_env=False` so an inherited `HTTP_PROXY` cannot silently redirect them. The private-range block above deliberately does not extend here — a self-hosted ntfy on the LAN is the *expected* configuration, and blocking it would make the feature useless for the people it is for.

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

**Userscripts** run in headless Chromium against the target site. This is intended behavior, but the script is running inside your container. Mitigations: no `--no-sandbox` (keep Chromium's sandbox on), a hard timeout, no filesystem access, no downloads, no service workers, and a dedicated low-privilege user for the browser process.

**As built:** the sandbox requirement turned out to be satisfiable rather than aspirational, and it is verified rather than assumed — the image build launches Chromium as a non-root user with the sandbox on and fails the build if it cannot. At runtime the launcher retries without it and logs a prominent warning, because a host that denies unprivileged user namespaces should degrade loudly rather than silently.

One mitigation in the original list was dropped: **network access is not restricted to the profile's declared hosts.** An interstitial bypass routinely redirects through a login domain, a consent domain and a CDN, none of which the person filling in the form can be expected to enumerate first — so request interception would break the normal case to constrain a script the user chose to install. The containment that matters is the browser sandbox and the fact that nothing it fetches is written anywhere but a cookie jar.

### An archive contains the credential that fetched it

A WARC records the **request** as well as the response, and the request carries the `Cookie:` header wget sent. So a capture of a gated site contains the jar that opened the gate, and it travels with any copy, backup or WACZ export of that file.

There is no flag to suppress it, and rewriting finished WARCs to redact would invalidate the checksums the integrity job depends on. So it is handled by saying so:

- A capture whose profile carries **account session cookies** — Google's `SID`, `__Secure-*` and friends, which the cookie parser already recognises — emits a warning before the crawl starts, naming them.
- The profile upload already warns when a whole-browser export includes a full account session, and recommends a narrower one.

The practical advice is unchanged and now enforced by a visible warning: use a jar containing only what the gate needs. A consent cookie inside an archive is uninteresting; a login session inside a file you might share is not.

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
- **Dependency scanning** in CI (`pip-audit`, `npm audit`, Trivy on the image). pywb and Chromium both pull large dependency trees. **Built, and deliberately advisory** — see below.
- **Version pinning** in the image; `:latest` plus Unraid auto-update plus automatic migrations is a bad combination for anything you care about.
- **Log rotation** with size caps — a crawl log from a 100k-page site is large, and unbounded logs are their own availability problem.

### The scanners report; they do not gate

`pip-audit`, `npm audit` and Trivy run in CI, and none of them can fail a
build. That is a decision with evidence behind it rather than a shortcut.

The image deliberately pins `setuptools<81` because pywb 2.9.1 still imports
`pkg_resources`, so "upgrade until the scanner is quiet" is not always a move
that exists here. A gate that cannot be satisfied is a gate somebody switches
off, and then nothing is scanned at all. The findings go to the run summary,
where a person decides between a version bump, a workaround, and living with
it.

They also run **weekly on a schedule**, which for this repository is the point:
the code can go three months without a commit while the advisories keep
arriving, and a scan that only runs on push is one that never runs.

### What the first scan found, and what was done about it

One HIGH in the image: CVE-2024-34069 in Werkzeug 2.2.3, which was not our
choice — pywb 2.9.1 requires it *exactly* (`Requires-Dist: werkzeug==2.2.3`),
and 2.9.1 is the newest release there is. That single finding makes the case
for both halves of the policy above. A blocking gate would have been red from
the day it was added, on something no version bump of ours could clear; and a
report nobody acts on is just a slower way of ignoring it.

**It was acted on. The image now installs `werkzeug>=3.0.3` over pywb's pin**,
and the scan reports zero HIGH or CRITICAL findings with fixes available.

The override is measured rather than hoped. Against a real capture through a
real `wayback`, pywb 2.9.1 replays byte-for-byte identically on 3.1.8 and on
2.2.3 — same responses on the bare content (`mp_`), the framed wrapper, the
untimestamped redirect and the CDX API. `pip` prints a dependency-conflict
warning naming the pin every time the image builds, which is the intended
paper trail rather than something to suppress.

What makes it safe to keep is not the measurement, which ages. It is that
`test_replay_e2e.py` and `test_thumbnail.py` drive a real pywb on every
container run, so a future pywb that genuinely needs 2.2.3 semantics fails a
test rather than somebody's replay tab. If that day comes, the honest response
is to pin back and accept the finding, not to delete the test.

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
