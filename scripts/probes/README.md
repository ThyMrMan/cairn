# Probes

Scripts that answered a design question by measuring, rather than by reading
documentation and hoping. They are kept because the answers have a shelf life:
each one is pinned to a version of something outside this repo, and the right
move when that version moves is to run the probe again rather than to assume.

Not part of the test suite. They need Docker and a few minutes, they talk to
real images, and a CI run is the wrong place for either.

## `resume_probe.py` — does an interrupted crawl leave resumable state?

Answers the question pause/resume rests on: browsertrix's docs say state is
written when a crawl is "interrupted" without naming a signal, and Cairn stops
an engine container with Docker's stop, which is **SIGTERM**. An implementation
that only handled SIGINT would have failed as an empty directory rather than an
error — silently, and only in production.

Three arms against a local fixture site, each interrupted six pages in.

**Measured on `webrecorder/browsertrix-crawler:1.14.1`, 2026-08-15:**

| Arm | Pages crawled first | State written |
|---|---|---|
| SIGTERM, default `--saveState` | 6 | yes — one file, 7,500 bytes |
| SIGINT, default | 6 | yes |
| SIGTERM, `--saveState always --saveStateInterval 5` | 6 | yes — 4 snapshots |

So `--saveState` defaulting to `partial` already writes the file on the signal
Cairn sends, with no flag change: it had always been written into the job's
temp directory and deleted moments later along with it. Arm 3 shows periodic
snapshots work, which is what a pause would need to survive a crash rather than
only a clean stop. An interrupted crawl exits **11**, not 0.

The state itself is a real queue, not a marker — `finished:` held the six
completed URLs and `queued:` the pending ones with their depth and seed id.

**The negative control is the point.** A run that reached zero pages produces an
empty `crawls/` directory for reasons that have nothing to do with signals, and
looks exactly like "SIGTERM does not work". Every arm asserts it crawled real
pages first and refuses to draw a conclusion otherwise — twice earlier in this
project a fixture the container could not reach nearly proved the opposite of
the truth.

## `resume_probe2.py` — and does that state actually resume?

A state file that replays the whole crawl would make "pause" a lie: the archive
doubles and nothing is saved. So the proof is negative — pages already finished
must not be fetched again.

Run `resume_probe.py` first; this reads its output directory.

**Measured on the same image and day:**

```
pages this run       : 6
  already-done again : 0
  new pages          : 6   ← resumed at p6, exactly where it stopped
warcs before / after : 1 / 2
exit code            : 0
```

It picked up the queue and wrote a **second WARC beside the first** rather than
rewriting it, which is what makes resuming into the same capture directory the
simple option: replay indexes across WARCs and never merges them
([D2](../../docs/00-decisions.md)), so the two halves of an interrupted crawl
need no reconciling.

Command-line options are not persisted in the state file and had to be
reapplied alongside `--config` — costless here, since `_argv()` rebuilds them
from the scope on every run anyway.

## `pagination_probe.py` — what does replay serve for a URL the crawl rejected?

Every reject is a bet that the link pointing at that URL does not matter, and
replay is where the bet settles. pywb has a fuzzy matcher that rescues some
misses, so "not captured" and "404" are not the same thing — and which is which
decides whether a reject is free or leaves a dead link on every page. That is
the question that got Blogger's Older-posts trail un-rejected once already.

Three arms against a fixture collection, the first of which is the control.

**Measured on the pinned pywb 2.9.1 in `cairn:latest`, 2026-08-16:**

| Requested | Result |
|---|---|
| captured URL + `?utm_source=` | **200** — rescued |
| asset + different cache-buster | **200** — rescued |
| `/search?updated-max=…` never captured | **404** |
| `/search?updated-max=…&start=7&by-date=false` | **404** |
| `/2019/04/post.html?m=1` | **200** — replays the post |
| `/2019/04/post.html?showComment=` / `?replytocom=` | **200** — replays the post |
| `/p/about.html?m=1` | **200** — replays the page |
| `/?m=1` | **404** |
| `/search/label/X?m=1` and `?updated-max=` | **404** |

The rule, read out of `pywb/warcserver/index/fuzzymatcher.py` afterwards to
explain the table: the catch-all rule (`url_prefix: ''`, `match: '()'`) is not
custom, so every candidate must pass `match_general_fuzzy_query`, which accepts
only when the request path's last segment carries a **file extension** — then
any query resolves to that path — or when the two URLs differ by a known
cache-buster (`_`, `cb`, `uncache`, `utm_*`, `callback=`). Blogger posts and
pages end in `.html`; `/`, `/search` and `/search/label/X` do not.

So three of the Blogger preset's rejects are free, `?m=1` costs the footer's
mobile link on the homepage and label pages only, and a rejected pagination
trail **404s cleanly** rather than silently serving another page.

**The control is the point.** Arm 2's 404s would look identical if fuzzy
matching were simply switched off in the fixture, and the whole conclusion
would be an artefact. Arm 1 is two misses pywb is known to rescue; the probe
refuses to draw a conclusion if neither is. Arm 2 was also run with bare
`/search` present in the collection — a real page on every Blogger blog, and
exactly what a query-stripping fallback would substitute — and it still 404s.

The trap this rules *in*: a rule with a non-empty `url_prefix` sets
`is_custom`, which skips that check entirely and accepts whatever the prefix
search returns. `fuzzy_lookup: [updated-max, max-results]` for a blog would
serve an arbitrary pagination page for any pagination URL — a pager that looks
like it works and loops. It needs a patched `rules.yaml`; nothing here
generates one.

## `synthetic_record_probe.py` — could the gap be filled without crawling it?

If a rejected pagination URL 404s, the other way to have a working pager is to
generate the pages and write them as WARC records. This asks whether that is
mechanically possible: whether a hand-written record is indistinguishable to
the index from a crawled one, and how forgiving the key is about the spellings
Blogger emits.

Local only — no Docker, no pywb.

**Measured on the pinned surt 0.3.1 / warcio 1.8.1 / cdxj-indexer 1.4.6, 2026-08-16:**

One key covers parameter order, `%2B` versus literal `+`, encoded versus plain
colons, and a trailing `#fragment`:

```
com,blogspot,example)/search?max-results=7&updated-max=2019-12-09t22:33:00+01:00
```

`&start=7&by-date=false`, `&m=1` and a different timezone each key separately.
A hand-written response record indexes byte-identically to a crawled one and
reads back at the recorded offset with its `X-Cairn-Synthetic` header intact.

**The negative control is the last part of arm 1.** "Everything collides" would
be the convenient answer and is the wrong one — `&start=` *must* key
separately, because that is what forces a rebuild to mint each record under the
exact URL that links to it, and page 1's pager href is the one link a generator
does not control. A run where nothing distinguishes has a broken canonicaliser.

See [docs/07](../../docs/07-replay.md#rebuilding-a-pager-rather-than-crawling-it)
for what a rebuild would take, and why the fabrication has to be declared.

## `overlay_probe.py` — content, gate, or content under a gate?

The one probe here that runs against a *real capture* rather than a fixture,
because the thing it had to explain only happens with a real account on a real
gated blog. A capture reported `ready`, the profile test reported `real
content`, and replay showed a wall of content warnings. Both reports were
truthful about the wrong thing.

It sorts every 200 HTML response into clean / gate / **overlay** — the third
being a complete page with a gate drawn over it. Blogger answers 200 with the
whole post and injects an iframe plus `body * { visibility: hidden }`, so
nothing is missing and nothing displays.

**Measured on a gated Blogger blog, 2026-08-16:**

| Bucket | Pages |
|---|---|
| `bucket_overlay` | 442 — every real post |
| `bucket_classic_gate` | 149 — the framed gate, recorded at its own URL |
| `bucket_clean` | 0 |
| `DISAGREE` | 0 |

The pages were complete: title, body text, images, every asset. What made the
difference was `'interstitialAccepted': false` in the page's own config —
per-browser state, not an authentication failure. The cookies worked, which is
precisely how a complete page arrived to be drawn over, and why the old advice
("re-mint the profile") pointed away from the fix.

### Re-accepting the warning is not a durable fix either

The obvious next answer — "click through the warning again and save the
profile" — was measured across three captures of the same blog and does not
hold. The acceptance cookie was **present and sent** the whole time:

| Capture | Time | Posts clean | Posts curtained |
|---|---|---|---|
| `old-preset-test` | 03:15–03:27 | **70** | 0 |
| `new-preset-test-2` | 13:57–14:08 | 0 | **70** |

The same 70 posts, ten hours apart. Between them, byte-identical:

- the `INTERSTITIAL` cookie — one 63-char value across *every* capture and
  every site, sent on 499 of 500 requests to the blog
- the User-Agent, `Sec-Ch-Ua`, `Sec-Ch-Ua-Platform`, `Sec-Fetch-*`

So the profile did not change and the client did not change; the server
stopped honouring the same token. Within the earlier capture it was already
inconsistent — all 70 posts clean, while 254 of 364 `/search` pages were
curtained at the same moment.

**What that rules out.** Not expiry of the profile, not a mismatched user
agent (the documented suspect in [06](../../docs/06-access-profiles.md)), not
sign-in state — no Google auth cookie is sent to the blog at all, and none is
needed, because this gate is a content warning rather than a login.

**What it means.** Nothing the profile controls makes this stick, so any fix
that lives at capture time is a coin flip. The durable observation is the one
that held in every run: the content came back **complete every time**, curtain
or no curtain. The damage is presentational and it is in the archived bytes,
which puts the only reliable fix at replay.

**The negative control is the disagreement count.** Every verdict is scored
against a literal search for the two markers; a non-zero `DISAGREE` fails the
run. The fixtures in `test_postprocess.py` were written from these bytes, so a
detector checked only against them would be marking its own homework.

## `head_insert_probe.py` — can pywb's head insert be extended, not replaced?

The follow-on from the overlay finding above. If the fix has to live at replay,
replay needs to inject a script into every archived page — and the obvious way
to do that is to override pywb's `head_insert.html`, which is the wrong way.
That template carries wombat's bootstrap and is version-coupled to the pywb in
the image; a copy would drift on the next upgrade and replay would keep serving
pages with the URL rewriting quietly gone. That failure looks fine until every
link on a replayed page reaches the live site.

Under test: point `head_insert_html` at a *differently named* template that
does `{% include "head_insert.html" %}`. pywb resolves templates through a
ChoiceLoader over the filesystem directory and then its own package, so the
include should reach pywb's original rather than recursing.

**Measured on the pywb in `cairn:latest` (2.9.1), 2026-08-16:**

| | wombat bootstrap | gate iframe rewritten | cairn script |
|---|---|---|---|
| pywb default | yes | yes | no |
| cairn template | yes | yes | yes |

**Arm 1 is the control that matters.** "Our marker is present" proves nothing
on its own — if wombat vanished along with the override, the page would still
contain our script and replay would still be broken. It also proves the marker
was not already there, which would mean the arms were never isolated.

It uses the template `replay.py` really generates, not a stand-in, and its page
is synthetic, so it needs nothing outside the repo. What it does *not* answer
is whether the script behaves: that happens in the browser after pywb has
served the page. That half is covered by `test_replay.py` and by an in-browser
run against a real archived page, recorded in
[docs/07](../../docs/07-replay.md#uncovering-a-page-the-site-drew-a-warning-over)
— gate removed, hiding rule removed, post computed `visible`, and an ordinary
page left with all four of its `<style>` elements intact.

## `cookie_bridge_probe.py` — can wget use a browsertrix profile's cookies?

`docs/00` D4 says every auth mode ends as a cookie jar and the engine only
ever sees `--load-cookies`. The browser profile broke that: it was the one
producer with no jar, so choosing a browser profile silently also chose
browsertrix.

Whether a bridge is possible turns on one thing — where Chromium got the key
that encrypts `Default/Cookies`. With an OS keyring, from the keyring, and
there is none in a container. Without one, from a **hardcoded** password.

**Measured on `webrecorder/browsertrix-crawler:1.14.1`, 2026-08-16:**

| | |
|---|---|
| profile tarball | 41,308,619 bytes for one page visit |
| stored value | 67 bytes, prefix `v10` |
| domain hash | present — Chromium 130+ prepends SHA-256 of the host |
| recovered | equal to the known plaintext |

`v10` is the answer. `PBKDF2-HMAC-SHA1("peanuts", "saltysalt", 1, 16)`,
AES-128-CBC, IV of sixteen spaces.

**The check is equality, not absence of an exception.** A wrong key decrypts
to bytes just the same, so "it ran" proves nothing; the fixture serves a known
value and the probe compares against it. It also asserts that narrowing by
host *excludes* — a bridge that quietly copied the whole cookie store would
otherwise pass.

It runs the shipped `profiles.cookies_from_browser_profile`, not a copy, so it
cannot pass against a mock of the thing being shipped.

**The fixture is a login page on purpose.** `create-login-profile --automated`
hunts for username and password fields and waits indefinitely when a page has
none — one five-minute timeout to discover.

## Running them

`resume_probe.py`, `resume_probe2.py`, `pagination_probe.py`,
`head_insert_probe.py` and `cookie_bridge_probe.py` need Docker;
the last re-execs itself into `cairn:latest` (override with `CAIRN_IMAGE`).
`synthetic_record_probe.py` and `overlay_probe.py` run against the app's own
venv; the last takes a capture's `warc/` directory and is worth pointing at any
capture that replays as a gate.

```bash
python scripts/probes/resume_probe.py && python scripts/probes/resume_probe2.py
python scripts/probes/pagination_probe.py
python scripts/probes/synthetic_record_probe.py
python scripts/probes/overlay_probe.py /archives/Unfiled/blog/captures/<capture>/warc
python scripts/probes/head_insert_probe.py
python scripts/probes/cookie_bridge_probe.py
```
