# 16 — Troubleshooting

Every recovery path needs shell access to the container. That is deliberate and
it is the recovery boundary: there is no email reset, no forgot-password link,
and no way in that does not start with control of the host.

Commands below assume the container is named `cairn`. `docker run` without
`--name` assigns a random one like `optimistic_brahmagupta`, and none of these
will work against it — pass `--name cairn`.

---

## The page does not load at all

Check the container's state first. This failure has two very different shapes
and the `PORTS` column separates them.

```bash
docker ps -a --filter name=cairn --format "{{.Names}} {{.Status}} {{.Ports}}"
```

| `PORTS` shows | Meaning |
|---|---|
| `8080-8081/tcp` | **Not published.** The ports are exposed only inside Docker's network; nothing on your machine reaches them. The container still reports `healthy`, because the healthcheck runs *inside* it. Re-run with `-p 8080:8080 -p 8081:8081`. In Docker Desktop, expand **Optional settings** and fill in the host ports — it leaves them blank by default. |
| `0.0.0.0:8080->8080/tcp` | Published correctly. If it still fails, look at `STATUS`. |

| `STATUS` shows | Meaning |
|---|---|
| `Exited (78)` | A configuration error the app cannot fix itself. The logs print a banner naming the problem and the fix — most often a `CAIRN_SECRET_KEY` that does not match the one the database was created with. |
| `Up (healthy)` with ports published | The app is serving. Check the URL and any reverse proxy in front of it. |
| `Exited (0)` or restarting | Read the logs for the startup banner. |

If you set `CAIRN_SECRET_KEY` in Docker Desktop, note that it takes **Name** and
**Value** as two separate fields — the name is `CAIRN_SECRET_KEY` and the value
is the key alone, not `CAIRN_SECRET_KEY=…`.

## The master key

Changing `CAIRN_SECRET_KEY` is only fatal once something has actually been
sealed under the old one — 2FA secrets, recovery codes, cookie jars. Before
that, the new key is adopted and logged.

Which key is in use:

```bash
docker exec cairn cairn key-info
```

If you lost the old key and accept losing what it sealed:

```bash
docker exec cairn cairn reset-key --force
```

## Sign In appears instead of the setup screen

An account already exists — the app never skips setup on a genuinely empty
instance. Almost always the `/config` volume carried over from a previous run.

```bash
docker exec cairn cairn users
```

`No account exists yet` means you are talking to a different process than you
think; check nothing else is bound to that port. Otherwise use the recovery
commands below, or point the container at an empty config directory.

## Locked out

State of the account:

```bash
docker exec cairn cairn users
```

Reset the password. This also clears any lockout and signs out every session:

```bash
docker exec -it cairn cairn reset-password admin
```

If your console has no TTY — Unraid's browser terminal, or `docker exec`
without `-it` — pipe it instead:

```bash
docker exec -i cairn sh -c 'echo "your-new-passphrase" | cairn reset-password admin --stdin'
```

Locked out by failed attempts but you do know the password:

```bash
docker exec cairn cairn unlock admin
```

Lost the authenticator *and* the recovery codes:

```bash
docker exec cairn cairn disable-totp admin
```

## The folder or tag tree on the share looks wrong

Both are derived from the database and both rebuild from it. They also rebuild
at every boot, so this is only needed between restarts:

```bash
docker exec cairn cairn rebuild-symlinks
```

That is a real repair rather than a refresh — it remakes every link instead of
trusting the ones that look right. If a site under `by-tag` shows as a **0 KB
file** rather than a folder, this is the fix: the link was written before its
target directory existed, which types it as a file link. Linux resolves it
either way, so only a Windows client ever sees the difference.

## A site page freezes the browser when you scroll

Almost always the archived page in the replay panel, not the app.

The panel embeds a real website at its real weight. A photo blog's front page
is tens of megabytes of full-resolution JPEGs, and a browser decodes those into
bitmaps several times larger again — one 3000×2000 photo is 24 MB decoded, and
a page of them will stall a tab. It shows up **on scroll** because that is when
a browser decodes the images approaching the viewport, and it takes the app's
own UI with it because replay on the same hostname shares a process with the
app ([07](07-replay.md#the-same-trap-costs-performance-too)).

**Turn off `Scripts` in the replay panel.** That is the fix in most cases, and
it is one checkbox beside the capture selector.

The dialog Firefox shows — *"This page is slowing down Firefox"* — is its
**slow-script** warning, so the usual cause is not the page's weight but its
JavaScript. An archived page's scripts run years after whatever they expected
to talk to stopped answering: a retry loop never succeeds, an infinite-scroll
handler never gets its next page, an analytics beacon never resolves. Live,
those finish. Replayed, some of them spin forever.

Confirmed by opening the same archive through pywb directly, on its own port,
with none of this application involved — it hung identically. That is what
places the fault in the page rather than anywhere in cairn.

There is no way to ask pywb not to run a page's scripts, so the panel drops
`allow-scripts` from the iframe sandbox instead. The archived bytes are
untouched; you are choosing not to execute them. What goes missing is whatever
the page built as it loaded — lazily-inserted images, infinite scroll, embedded
players.

Also worth knowing:

- The archived page now loads only when you ask for it (**Load the archived
  page**), so a site page costs nothing to visit even when its archive is one
  of these.
- **Give replay its own hostname.** Browsers isolate processes by site, not by
  origin, so on one hostname a struggling archived page stalls the app's UI as
  well as itself ([07](07-replay.md#the-same-trap-costs-performance-too)).
- **The reader view** is unaffected: it reads extracted text and renders no
  scripts and no images at all.

If turning scripts off does *not* help, the page is genuinely heavy rather than
looping — a front page of full-resolution photographs decodes to far more
memory than it occupies on disk. Open a single post rather than the front page.

## The replay tab is blank, and you changed the replay port

Set `CAIRN_REPLAY_PUBLIC_PORT` to the port you published, and reload.

`CAIRN_REPLAY_PORT` is the port pywb **binds to inside the container**. The port
your browser needs is the **host** side of the mapping, and those are the same
number only when the container port is published unchanged. `-p 9081:8081` —
which is precisely what changing "Replay Port" in the Unraid template produces —
leaves pywb on 8081 inside while the world reaches it on 9081.

Nothing inside the container can see the published port; a request to the *app*
says nothing about how *replay* was mapped. So the app has to be told:

```yaml
environment:
  - CAIRN_REPLAY_PUBLIC_PORT=9081
```

The failure is silent because the iframe is cross-origin — the browser refuses
to say why it did not load, and the only trace is in the developer console. The
replay tab now warns when it can tell that ports are being remapped, which it
infers from the app itself being reached on a port other than the one it binds.

Behind a reverse proxy, set `CAIRN_REPLAY_PUBLIC_URL` instead; a full URL wins
over the port, because there the hostname changes too.

## Replay 404s after a restore or a move

Re-point the collections. pywb picks the change up on the next request, with no
restart:

```bash
docker exec cairn cairn replay-init
```

## A job says running, but its crawl finished long ago

The signs: the job has read `running` for hours or days, the capture's URL
count stopped rising well before that, and the capture's `crawl.log` ends with
`FINISHED` and a `Total wall clock time` line. Cancel may have done nothing.

Another job held the database's write lock — usually while post-processing a
large capture — and the task watching this crawl gave up on its next write. The
crawl carried on unwatched, finished, and nothing recorded that it had.
[05](05-capture-engines.md#a-database-that-says-no-must-not-end-a-capture) has
the measurements and what changed.

**Now:** the supervisor finds such a job within about three minutes, stops its
engine if it is still running, and marks it `interrupted` with a reason that
says whether the crawl or only its post-processing was lost. Cancel works on it
too. While a capture is being post-processed the job list says so, and its row
cannot be deleted until that is done.

**A capture stranded before that change** reads `interrupted` with no URLs,
but its WARCs are whole and on disk under `captures/<dir>/warc/`. Rebuilding
the site's index makes them replayable, and so does the site's next capture,
whose post-processing indexes every WARC the index has not seen yet. **Rebuild index** on
the Replay tab does it inside a web request, which on a site with tens of
gigabytes of WARCs outlasts any browser's patience; run it in the container
instead:

```bash
docker exec cairn cairn reindex <site-slug>
```

The capture's URL list and counts do not come back: they were the rows that
were never written.

## A feed capture takes most of an hour to add one post

The signs: every feed capture of a site takes about as long as the last,
whatever was published; its URL count runs to the thousands for one new post;
and nearly all of those URLs are revisits — fetched, and found to be archived
already.

Feed captures ran at depth 1, and one link from a Blogger post is every monthly
archive and label in its sidebar. wget then fetched every image on each of those
pages. On a real blog that was 83 pages and about 1,560 images each time, 42
minutes to add one post. [08](08-feeds-and-scheduling.md#incremental-captures)
has the measurements.

**Now:** a feed capture fetches the new posts, what they display and the files
they link to — the full-size images included — and no other page. Expect tens
of URLs rather than thousands. A feed job that was already queued when you
updated runs the new way too. A pasted list of URLs behaves the same, and had
been crawling each listed page's entire site.

**What that gives up:** the archived home page, labels and monthly archives no
longer change with each new post, so browsing the replay from the front page
shows the site as of the last capture that fetched those pages. New posts are
in the capture list and in search, and a full capture refreshes the rest.

A capture whose warnings mention files "on this site's own host" that were not
fetched hit the one thing this costs: a file the site serves at a URL that
does not end in a type like `.jpg` or `.css` reads to wget exactly like a page. The warning names them, and a full
capture fetches them.

## Resume says nothing records what a capture was for

A paused capture that is not a full crawl — a feed capture, a pasted list —
was paused before captures kept a note of what they were asked to fetch, and
the job that knew has since been cleared from the job list. Resuming it could
only continue as a crawl of the whole site, which is what resuming used to do
by mistake, so it is refused instead.

Delete the paused capture. Its feed items are pending again the moment it is
gone, and the next scheduled pass captures them; or press **Capture pending**
on the feed. A capture paused from now on keeps the note with it, and clearing
the job list no longer matters.

## An infinite-scroll page spins forever in replay

The page replays, you reach the bottom, and a loading wheel turns and never
stops. Two different things cause it, and they can both be true at once.

**The next page was never captured.** browsertrix only runs its autoscroll
behaviour if the page passes a test first: it smooth-scrolls to 98% of the page
and gives it 500 ms to grow. On a long page the smooth scroll has barely
started by then, so the page cannot have grown, and the behaviour reports
*"page seems to not be responsive to scrolling events"* and skips — it does not
check again. Nothing was scrolled, so the request the spinner waits for was
never made. Look for that line in the capture's log.

Nothing in the crawl's settings changes this; waiting longer does not help,
because the test is not racing the page's scripts. What usually saves you is
that the same posts are reachable another way: WordPress and Blogger both put a
real *Older posts* link in the HTML and the crawler follows it, so `/page/2/`
and `/page/3/` are in the archive even when the scroll is not. Turn **Scripts**
off in the replay viewer: the infinite scroll never initialises, so it never
hides that link.

**The request was captured but cannot be looked up.** An infinite scroll is
usually a `POST`, and a POST replays only if the index was built with
`post_append` ([07](07-replay.md#indexing)). Indexes written before Cairn
passed it hold that record under a key pywb never asks for. **Rebuild index**
on the site, or `cairn reindex <slug>`, writes it under the right one — and any
capture taken since then is indexed that way already.

**If instead it scrolls and repeats itself**, the capture holds some of the
scroll and not all of it. pywb does not answer 404 for a request body it has no
record of; it falls back to the nearest record it holds, so the same posts
arrive again each time. The pages themselves are usually still there under
`/page/2/` and so on — turn **Scripts** off and use the links.

## The feeds panel is full of feeds nobody asked for

Indexing attaches every feed it finds, and a blog platform publishes one per
post's comments: Blogger as `/feeds/<post id>/comments/default`, WordPress as
`/<post>/feed/`. A site indexed once can come back watching dozens. They arrive
switched off, so they poll nothing and capture nothing — the cost is the panel,
with the two rows that matter at the bottom of forty that do not.

**Unwatch all**, in the panel header, removes the lot in one press. Captures
already made are kept: what goes is the schedule and each feed's memory of
which entries it has seen. Then add back the one you want with **+ Add a feed
→ Find feeds**, which probes live and saves nothing until you press *Watch it*.

The first poll of a re-added feed is a baseline, not a backlog: it records what
the feed holds today and captures none of it, so putting the posts feed back
does not re-fetch the archive.

**Indexing again re-attaches them.** Discovery has no memory of what you
removed, so a later index brings back whatever the site still publishes. Unwatch
after indexing, not before.

## A crawl runs for hours and never finishes

Open the capture and read *what it fetched*. A crawl that will not end is
almost always spending itself on one of two things, and both are named there.

**URLs that are not URLs.** A widget that builds its links in JavaScript —
Blogger's random-posts one is the common case — leaves the unevaluated text in
the markup: `'<a href="' + randompostsurl + '">'`. wget reads script text for
anything shaped like a link, so `' + randompostsurl + '` becomes a *relative*
URL and resolves against every folder it is seen in. Measured on one reported
crawl: 160 such URLs, asked for 21,758 times, 61.5% of everything the crawl
did in thirteen hours, every one a 404.

Those rows now offer **"wherever it appears"** beside the ordinary Skip, and
that box is what you want. The row's own pattern is anchored to the row's own
path, which is right for a real place and wrong here: the same widget string
turns up under `/`, `/p/`, `/2026/` and `/2026/08/`, so the report shows four
rows and skipping all four still leaves two fetching.

**The pagination trail.** On Blogger, `/search?updated-max=…` is one chain per
arrival context, and the count per post *rises* with the post count — 71.9 per
post on a 2,855-post blog. The standard preset keeps it; the **lean Blogger
preset** rejects it. That was a third of the same reported crawl.

**Check the pattern actually saved.** A site's *Domains and crawl scope* panel
has its own **Save scope** button and says *Unsaved changes* until you press
it — and the match count beside a pattern is computed from the draft, so a
pattern can show thousands of matches and never have been saved.
*Settings → Skip these URLs everywhere* saves the moment you add. Either way a
pattern added while a crawl is running does nothing to that crawl.

## The same page is fetched over and over

The capture warns when it happens, and the job page says the same thing while
the crawl is still running — both read `crawlhealth`, so they cannot disagree
about one capture. wget remembers what it has already fetched by **the files
it left on disk**, so anything that writes no file gets asked for again every
time something links it. A 404 writes no file, which is why the widget URLs
above are re-requested rather than remembered.

The bar is **3.0 requests per distinct URL**, not 2.0, and the difference is
deliberate: a site reachable under two names maps two URLs to one file on
disk, which costs exactly one extra fetch each — measured flat at 2.0x on 6, 30
and 90 pages. A warning that fires there is one nobody reads.

It is also why `--delete-after` is never used, even though the WARC already
holds everything: measured on a six-seed site whose ideal result is eight
records, deleting each file as it arrived gave 38 records from 8 distinct URLs.

Nothing is wrong with the archive when this happens — the bytes are right, and
a repeated fetch of a page that *does* exist is deduplicated in the WARC. What
it costs is the crawl's time and the origin's patience.

## A skip pattern is saved but nothing was skipped

Look at the count beside it. **matches nothing** means it is inert.

The usual cause is that it was copied out of the *what it fetched* report. That
report writes `#` for a numeric segment and `*` for a varying one — its own
shorthand, not a regular expression. As a regex `#` is a literal `#`, which no
fetched URL contains, because fragments are stripped before a request is made.
So `/feeds/#/comments/default` compiles, saves, appears in the list, and matches
nothing.

Use **Skip** on the report row instead of copying it: that generates the pattern
the row means — `/feeds/[0-9]+/comments/default` — and checks it against that
row's own example before offering it.

**A pattern added mid-crawl does not affect the crawl that is running.** The
engine is given its rules on the command line when it starts. Cancel and start
again if the saving matters now.

## A capture is marked partial and looks complete

**Settings → Storage → recompute capture status.** It re-decides every partial
from what that capture recorded about itself, and can only ever clear one,
never create one.

The reason it exists is that the rules for "incomplete" have been corrected
more than once — a content warning drawn over a complete page used to count
against the capture that held it, and 147 gate documents beside 147 complete
pages read as 147 failures. A capture that was fine all along should not carry
the mark forever.

If it comes back partial, the capture's `partial_reasons` says why:
`interstitial-pages` (the cookies really were not accepted), `overlay-pages`
(a content warning drawn over a page, and only a fault when replay is not
lifting it), `gate-redirect`, `nothing-archived`, or `step-failed`. Gate
documents are counted separately and never downgrade a capture on their own —
they are the frame, not the page.

## Replay says a URL you never visited is not in this collection

Something like *the url https://www.blogger.com/interstitial/blog?u=… could not
be found in this collection*.

That URL is a content warning the site drew **over** a page it had already sent
in full. The page is in the archive; the frame on top of it is not, because the
crawl was told to skip it. Replay lifts the covering rather than fetching the
gate — if you are seeing pywb's 404 instead, `CAIRN_REPLAY_UNCOVER_OVERLAYS` is
off, or the index predates it. Rebuild the index for that site and reload.

## Reclaiming space from deleted sites

Deleted sites keep their archive until they are purged, and the sweep runs at
boot and on the daily ticker. To reclaim now:

```bash
docker exec cairn cairn purge-trash
```

## Search returns nothing for an old capture

Search covers what has been extracted, and extraction runs after a capture.
Captures made before the search index existed are absent from it until
**Rebuild search index** reads their WARCs again. The search page says so, with
a button.

## Sites have no thumbnail

Thumbnails are taken through replay, so they need the pywb sidecar running.
**Settings → Site thumbnails → Take the missing ones** backfills every site
that has none; the job fails with one sentence rather than two hundred if
replay or Chromium is unavailable.

A site whose archive holds no page replay could show — a capture that was
redirected to a content warning, for instance — gets no thumbnail, which is the
correct outcome rather than a picture of an error page.

## The test suite behaves differently in the shipped image

Run it in `cairn:dev` ([`docker/Dockerfile.dev`](../docker/Dockerfile.dev)),
not in `cairn:latest`. The runtime image's entrypoint is `/init`, so anything
run through it starts s6 — which starts the app on 8080 and pywb on 8081
alongside the tests. That is not a neutral environment: a suite that binds a
fixed port finds it taken, and the failure mode is a test that passes against
the wrong server rather than one that errors.

The dev image sets `ENTRYPOINT []` for exactly this reason.

**Rebuild `cairn:dev` whenever you rebuild `cairn:latest`.** It is built `FROM`
the runtime image, which pins nothing, so a stale dev image keeps whatever
tooling the runtime image had on the day it was built. Suites skip rather than
fail when a tool is missing, so the symptom is a skip count that looks
deliberate. `pytest -rs` prints the reason for every skip and is the way to
tell "opted out" from "quietly not testing this any more".

## Starting over

Wiping the database wipes the archives' bookkeeping too, so prefer the commands
above. If you truly want a clean slate, stop the container and delete
`cairn.db` from the config volume.
