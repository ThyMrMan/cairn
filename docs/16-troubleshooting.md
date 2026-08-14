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
