# 15 — Feature Tour

What using Cairn is actually like, in the order you meet it rather than the
order it was built. Every section says what the feature is *for* and what it
deliberately does not do; the design documents beside this one say why.

This is the long version of the README's summary. If you want the reasoning
behind any decision here, [00 — Decisions](00-decisions.md) is the index.

---

## Adding a site

Point Cairn at a URL and press **Index**. It reads `robots.txt`, sitemaps
(including paginated and index sitemaps), feeds, and a bounded sample crawl,
then shows the **domain picker**: every host the site touches, grouped by
registrable domain, counted by how often pages link to it and how often they
pull files from it, each with a guessed role.

Two checkboxes per host — *crawl its pages*, and *fetch its files* — because
they are different questions. An image CDN wants the second and not the first;
a comment iframe wants neither. On a Blogger blog the answer arrives already
correct: `*.bp.blogspot.com` preselected as assets-only, analytics excluded,
and the `?m=1` reject applied that otherwise archives every post twice.

The grouping comes from the Public Suffix List's *private* section, which is
what makes `foo.blogspot.com` and `bar.blogspot.com` separate sites while all
four `N.bp.blogspot.com` are one CDN.

**Discovery through a browser** is a checkbox on the same screen, off by
default. It exists for the hosts that only JavaScript names — and it reads the
browser's **network log** rather than the rendered DOM, because
`new Image().src = "//cdn/pixel.gif"` fetches without ever entering the
document. Rendering is capped at 40 pages, and each run reports whether it
found anything the plain HTML did not already name, which is what tells you
whether to bother next time.

**Multi-seed sites.** A blog that moved domain, or one that lives at a custom
address and a blogspot original, is one site with one scope, one index and one
replay collection. Each seed's origin is enumerated separately — its own
`robots.txt`, its own sitemap — because a second domain reached only by
link-following is a second domain half-archived.

## Capturing

Press **Capture** and URLs stream past in a live log. What lands on disk is a
WARC, a `manifest.json` with a SHA-256 of every file, the URL list with its
failures, and a CDXJ index merged across every capture this site has ever had.

One capture covers the whole domain as a unit. There is no "snapshot of a
page" concept to reassemble later.

**Access profiles** are how you get past a content warning or a login. Three
modes, all ending in the same place, because the crawler never runs JavaScript
and so can only ever use cookies:

- Upload a `cookies.txt` exported from your browser.
- Upload a **Tampermonkey userscript**, which runs once in a real Chromium and
  keeps whatever it earns.
- Press **Open a browser and sign in** and click through it yourself, in a live
  Chromium streamed into the page over CDP.

**Test** then fetches the gated URL exactly the way the crawler will, so a jar
that has stopped working is a five-second check rather than a six-hour one.
Profiles now keep the whole browser session — localStorage and IndexedDB as
well as cookies — so a re-mint no longer quietly downgrades one. The UI says
the thing that had no home before: wget takes `--load-cookies` and nothing
else, so a login kept in localStorage passes the profile test and fails the
capture.

> **An archive contains the cookies that fetched it.** A WARC records requests
> as well as responses, `Cookie:` header included. That is unavoidable and
> worth knowing before sharing one — use a jar holding only what the gate
> needs. Cairn warns before any capture whose profile carries full account
> session cookies. ([11](11-security.md))

**A capture that was turned away says so.** A gated blog does not serve a
content warning to a crawler with no cookie — it *redirects* to one, on a host
the crawl may not follow. So the archive holds a single 302 and nothing else.
That capture is marked `partial` and explains itself, rather than leaving you
to meet pywb reporting that a URL you never heard of is not in this collection.

**A second engine.** wget cannot run JavaScript, which on a modern blog theme
means it misses a gallery built by script, images whose `src` is set on scroll,
and links that only exist after the page runs. **browsertrix** runs a real
browser and is chosen per site; the form under the picker is generated from the
engine's own schema. It runs as a sibling container, which needs the Docker
socket — **that grants root-equivalent control of the host**, so read
[11](11-security.md) first. Without the socket the engine simply shows as
unavailable.

browsertrix genuinely cannot use a cookie jar — it runs Brave, and a profile
built with our Chrome for Testing is accepted and silently ignored — so a site
behind a content warning still wants wget. The picker says so before the
capture, not after.

Both engines write into the same site folder and the same replay collection, so
switching engines does not fork the archive.

Writing your own is two files: copy [`examples/engine-template/`](../examples/engine-template/),
then `cairn engines test ./my-engine` runs it against a fixture site and checks
it honours the protocol. Cairn never imports engine code — it spawns a command
and reads NDJSON — so an engine can be written in anything.

## Reading the archive

**Browse the archive** puts the captured site back on screen, served from the
WARCs by pywb on its own origin. Click through it, type a different archived
URL, switch between captures of the same page from the dropdown. The controls
live outside the iframe on purpose: archived CSS cannot restyle a capture
selector it never receives, and archived JavaScript cannot fake one it cannot
reach.

**Reader view** sits beside replay, never instead of it. It renders the
extracted text of an archived page — no CSS, no JavaScript, no pywb — which
makes it fast, accessible, and the view that still works when the collection
will not load. It names the capture it read, so it is never mistaken for the
live page.

**Annotations** live on the reader view and anchor to a **quotation** rather
than to a position. Anchoring to replayed content is not merely hard here, it
is unavailable: replay is a separate origin precisely so archived JavaScript
cannot reach the app, which means the app cannot read a selection out of the
iframe either. A quote is the better anchor anyway — re-extraction rewrites
every byte offset, and a later capture has different ones again. A note whose
sentence is gone is reported, never moved to a sentence it did not mark.

**Search** reads the text extracted from every captured page, so "which of my
archives mentioned this?" is one query, and a result opens the archived page at
the version that matched.

The hard part is not the search engine. A blog's sidebar lists every post title
on every page, so indexing what was served makes one post title match the whole
blog. Cairn drops the furniture two ways: by recognising what templates call
their nav, sidebar and footer, and — for a template that names nothing usefully
— by noticing which blocks of text appear on most of a capture's pages. Nothing
but the standard library does the parsing.

**Site cards carry a picture of the archive.** Taken through replay, not off
the live web: the archive of a blog that closed would otherwise show whatever
its parked domain serves today, and the archive of a gated blog would show the
content warning. It is refreshed only when a capture actually changed the page
it shows, so an incremental capture of one new post does not start a browser.

## Organising

The folder tree in the UI *is* the directory tree under `/data/archives`, so
the structure you build there is the structure you browse over SMB. Renaming or
dragging a folder moves one directory and carries everything under it.

Tags cut across folders and get their own tree of relative symlinks under
`/data/by-tag`, so the same grouping works from a file manager. Relative, not
absolute: an absolute `/data/…` link works perfectly inside the container and
resolves to nothing on a client that mounted the share.

The filter bar combines folders, tags, status, errors and dates, and any filter
can be saved as a named view. A view is nothing more than the query string —
which is why the URL of a filtered list is shareable, and why there is only one
definition of what a filter means.

## Staying current

Add a **feed** — or press *Find feeds* and pick from what the site actually
publishes — and new posts are archived into that same site's folder, seeded
from the feed alone and deduplicated against everything already stored. A new
post costs a few hundred kilobytes rather than another full crawl.

The first poll is deliberately a baseline: it records what the blog already has
and captures none of it, because watching a blog should not mean re-fetching
its archive one post at a time.

A **sitemap** can be watched too, and it is the only thing that will tell you a
page *disappeared* — the moment the archive paid for itself, and the one
notification on by default. Absence means opposite things in the two: a feed
carries the most recent N entries, so an entry leaving one is the feed working
correctly.

For a site with **no feed at all**, watch a page. It is fetched on a schedule
and captured when its readable text changes — text, not markup, because a visit
counter or a rotating advert changes the response on every single fetch.

Every poll is recorded: what it fetched, what it parsed, what was new, and what
it did about it. Notifications go to ntfy, any webhook, or any Apprise URL.

**The periodic digest** reports what has *not* happened, which is the half
nothing else shows: sites nothing has captured in a month, feeds that poll
successfully and return nothing because the URL now serves a sign-in page,
credentials expiring next week. It is readable on the dashboard as well as
pushed, and silent when there is nothing to say.

**Site health** checks whether the originals are still there. All of the work
is in not crying wolf: a state changes only after two checks agree, a 500 is a
site failing rather than ending, a DNS failure says more about this end than
theirs, a 403 is about our user agent, and a redirect off the registrable
domain is a *move* — which is actionable, because the new address wants adding
as a second seed.

## Getting things in

**Bulk URL import** takes a pasted list — a Netscape bookmarks export, a
markdown list, a spreadsheet column, anything with http(s) URLs in it — groups
it by registrable domain, and archives exactly the pages listed. A pasted URL
is a *page*, not a site, so each group's site is seeded at the origin and the
capture does not crawl: fifty bookmarks across fifty domains each triggering a
full crawl is a plausible way to get an IP address blocked. Crawling is a tick
box.

**The bookmarklet** is one click from any page you are reading. It carries no
credential and cannot — a `javascript:` bookmark runs on somebody else's
origin, so an authenticated call would need a token in the URL, and therefore
in browser history, the referrer, and every proxy log on the way. It opens a
Cairn page instead and lets the session cookie already in that browser do the
work. Server-side it is the URL importer with one URL.

**Already running ArchiveBox?** Mount its data directory and Cairn reads the
index, brings each domain across as a site, carries the tags, and indexes the
WARCs it already made. Your archive is copied, never moved or written to — the
index is opened read-only. One bad entry is reported and skipped rather than
abandoning the import.

## Keeping it

**Export** packages a site into a single `.wacz`: WARCs, index, page list and
checksums in one file that [ReplayWeb.page](https://replayweb.page/) opens with
no server at all. It is the format for handing an archive to somebody and for
an offsite copy that outlives this tool.

**Archive health** re-reads every archived byte and compares it to the checksum
taken when it was written, weekly by default. Bit rot on an array is real and
WARCs are cold data nobody opens for years. It never repairs anything — a WARC
cannot be corrected, only restored or captured again — so it names the file,
the capture and the site, and leaves the decision to you.

**Checking a backup** is the same walk against a different root. Making the
copy is `rsync`'s job, or `restic`'s, and both are years ahead of anything
worth writing here; what none of them can answer is whether every capture this
instance knows about is present in the copy, and whether each file still hashes
to what was recorded. Mount the copy read-only, point Cairn at it, and it says.
A path inside `/data` is refused, because checking the archive against itself
would pass and mean nothing.

**Changes and retention** answers the question that decides how much disk this
costs: was the last full recapture worth it? The diff names the pages that
changed and, inside them, the sentences — from the extracted text, so page
furniture does not report the whole site as changed every month.

Retention is off by default and its dry run works before you switch it on,
because that is how you decide whether to. It never deletes the first capture,
the newest ones, the last capture holding a page that is gone from the live
site, or **a capture that a later one deduplicates against** — the last of
which matters more than it sounds: prune it and the newer capture replays 503
for a page whose own files are perfectly intact.

**Media.** Neither wget nor a browser captures a video stream, so an archived
post with a YouTube embed is a page with a dead rectangle in it. Switch media
download on for a site and `yt-dlp` goes back for what the page embedded,
bounded per item, per capture and by count — off by default, because it is the
one thing here that turns a megabyte capture into a gigabyte one. The image
carries no ffmpeg: it is 481 MB and only merges separate video and audio
streams, so the default asks for a single file instead.

Media URLs are the one genuinely attacker-controlled fetch target in the
system — they come out of archived HTML somebody else wrote — so the
private-range block in [11](11-security.md) is enforced there.

It lives under **Embedded video and audio** on a site's page: the switch, the
three limits, and a list of what each capture collected — playable in place —
alongside what it refused and why. That last part is the half that matters
years later, when the question is not "where is the video" but "was it ever
there". Anything downloaded and since deleted, by a retention sweep or by hand,
still shows its record; it just stops offering a link.

## Watching it

**Prometheus** can scrape `/api/metrics`, off by default. It carries counts and
nothing else: no site name, URL, host, folder or tag appears in it, because a
scraper cannot log in and an exporter tends to be reachable more widely than
the app.

The running build is shown at the bottom of the sidebar and in **Settings →
About**. The version on its own is not enough — it changes on a release and not
on a commit — so the build id beside it is what answers "am I testing the
update?".

---

## Deliberately not built

Recorded so the question does not come up repeatedly. The full list with
reasoning is in [13 — Feature backlog](13-feature-backlog.md#anti-features).

| | Why |
|---|---|
| **Public share links** | Low value, high effort, and the feature most likely to introduce a security hole — it punches a hole in the auth boundary on the origin that replays untrusted JavaScript |
| **Storage tiering** | Measured and it does not fit: replay serves a symlinked WARC fine, but the containment check that stops an engine escaping the archive tree refuses one — and on Unraid the share's cache setting already does this transparently |
| **A third capture engine** | The interface is proven by a second engine that exercises every part of it; a third exercising the same parts proves nothing further, and each is a real maintenance cost |
| **Multi-user with permissions** | The requirement is explicitly single-user. Roles and quotas would touch every endpoint for no benefit here |
| **Its own WARC replay implementation** | pywb exists and is good |
| **Rewriting archives in place** | WARCs are immutable. Every "fix the archive" feature is really a "capture again" feature |
