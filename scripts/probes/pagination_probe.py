"""What does pywb serve for a URL the crawl rejected?

This decides whether a reject is free or leaves a dead link, which is the
question that got Blogger's Older-posts trail un-rejected once already. pywb
has a fuzzy matcher that rescues some misses, so "not captured" and "404" are
not the same thing, and reading the rules is not enough to say which is which.

Three arms, and the first one is the reason the other two mean anything:

  1. **Is fuzzy matching even on?**  Two misses it is known to rescue: a
     captured URL requested with a tracking parameter, and an asset requested
     with a different cache-buster. If these 404, fuzzy matching is off or
     misconfigured in this collection and arm 2 proves nothing at all.
  2. **Does it rescue Blogger pagination?**  An `updated-max` that was never
     captured — asked with bare `/search` present in the collection, because
     that is a real page on every Blogger blog and is exactly what a
     query-stripping fallback would substitute.
  3. **Which rejected shapes still replay?**  The preset rejects `?m=1`,
     `?showComment=` and `?replytocom=`. Whether those cost anything depends
     on the URL they ride on, and this is the table that says.

The expected result is that arm 1 rescues, arm 2 does not, and arm 3 splits on
whether the path has a file extension — pywb's catch-all rule accepts a
candidate only when the request path's last segment has one (any query then
matches that path), or when the two URLs differ by a known cache-buster.
Blogger posts end in `.html`; `/`, `/search` and `/search/label/X` do not.

Needs the cairn image, which is where pywb lives. Re-execs itself into it.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

IMAGE = os.environ.get("CAIRN_IMAGE", "cairn:latest")
WORK = "/tmp/pagination_probe"
PORT = 8090
B = "https://example.blogspot.com"

PAGE2 = f"{B}/search?updated-max=2019-12-09T22:33:00%2B01:00&max-results=7"

# Captured. Deliberately includes bare /search — the page a query-stripping
# fuzzy fallback would reach for — and one post, one static page, one label
# page, so arm 3 can compare paths with and without an extension.
CAPTURED = {
    f"{B}/": b"HOMEPAGE",
    f"{B}/plain.html": b"PLAIN-NO-QUERY",
    f"{B}/asset.js?v=1": b"ASSET-V1",
    f"{B}/2019/04/post.html": b"POST-BODY",
    f"{B}/p/about.html": b"STATIC-PAGE",
    f"{B}/search": b"BARE-SEARCH-INDEX",
    f"{B}/search/label/Recipes": b"LABEL-PAGE",
    PAGE2: b"PAGE-TWO",
}

MARKERS = {
    "PLAIN-NO-QUERY": "plain.html",
    "ASSET-V1": "asset.js?v=1",
    "POST-BODY": "the post",
    "STATIC-PAGE": "the page",
    "HOMEPAGE": "the homepage",
    "BARE-SEARCH-INDEX": "BARE /search",
    "LABEL-PAGE": "the label page",
    "PAGE-TWO": "page 2",
}

ARMS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "1. is fuzzy matching on at all?  (must rescue, or arms 2-3 mean nothing)",
        [
            ("captured URL + tracking param", f"{B}/plain.html?utm_source=x"),
            ("asset, different cache-buster", f"{B}/asset.js?v=2"),
        ],
    ),
    (
        "2. does it rescue Blogger pagination?  (bare /search IS in the collection)",
        [
            ("captured page 2, exact", PAGE2),
            (
                "same, params reordered",
                f"{B}/search?max-results=7&updated-max=2019-12-09T22:33:00%2B01:00",
            ),
            (
                "same, literal + not %2B",
                f"{B}/search?updated-max=2019-12-09T22:33:00+01:00&max-results=7",
            ),
            (
                "same, encoded colons",
                f"{B}/search?updated-max=2019-12-09T22%3A33%3A00%2B01%3A00&max-results=7",
            ),
            ("+ start & by-date (real theme link)", f"{PAGE2}&start=7&by-date=false"),
            (
                "un-captured updated-max",
                f"{B}/search?updated-max=2011-01-01T00:00:00%2B01:00&max-results=7",
            ),
        ],
    ),
    (
        "3. which rejected shapes still replay?  (canonical form captured only)",
        [
            ("post + ?m=1", f"{B}/2019/04/post.html?m=1"),
            ("post + ?showComment=", f"{B}/2019/04/post.html?showComment=123"),
            ("post + ?replytocom=", f"{B}/2019/04/post.html?replytocom=9"),
            ("static page + ?m=1", f"{B}/p/about.html?m=1"),
            ("homepage + ?m=1", f"{B}/?m=1"),
            ("label page + ?m=1", f"{B}/search/label/Recipes?m=1"),
            (
                "label + updated-max",
                f"{B}/search/label/Recipes?updated-max=2019-12-09T22:33:00%2B01:00",
            ),
        ],
    ),
]


def in_container() -> bool:
    try:
        import pywb  # noqa: F401
    except ImportError:
        return False
    return True


def reexec() -> int:
    """Run this same file inside the image, where pywb is."""
    here = os.path.abspath(__file__)
    print(f"pywb is not importable here; re-running inside {IMAGE}\n")
    return subprocess.call(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/opt/venv/bin/python",
            "-v",
            f"{here}:/probe.py:ro",
            IMAGE,
            "/probe.py",
        ]
    )


def build_warc(path: str) -> None:
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        for url, body in CAPTURED.items():
            mime = "application/javascript" if ".js" in url else "text/html; charset=UTF-8"
            headers = StatusAndHeaders(
                "200 OK",
                [("Content-Type", mime), ("Content-Length", str(len(body)))],
                protocol="HTTP/1.1",
            )
            writer.write_record(
                writer.create_warc_record(
                    url,
                    "response",
                    payload=io.BytesIO(body),
                    http_headers=headers,
                    warc_content_type="application/http; msgtype=response",
                )
            )


def get(url: str) -> tuple[int, bytes]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "probe"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # connection refused while pywb boots
        return 0, str(exc).encode()


def main() -> int:
    if not in_container():
        return reexec()

    os.makedirs(WORK, exist_ok=True)
    os.chdir(WORK)
    subprocess.run(["wb-manager", "init", "probe"], check=True, capture_output=True)
    build_warc(f"{WORK}/pages.warc.gz")
    subprocess.run(
        ["wb-manager", "add", "probe", f"{WORK}/pages.warc.gz"], check=True, capture_output=True
    )

    server = subprocess.Popen(
        ["wayback", "--port", str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    rescued_controls = 0
    try:
        for _ in range(40):
            if get(f"http://localhost:{PORT}/probe/")[0]:
                break
            time.sleep(0.5)

        import pywb

        print(f"pywb {pywb.__version__}, {len(CAPTURED)} records captured\n")

        for title, cases in ARMS:
            print(title)
            print(f"  {'requested':44} {'CDX':>4} {'HTTP':>5}  served")
            print("  " + "-" * 84)
            for name, target in cases:
                quoted = urllib.parse.quote(target, safe="")
                _, cdx_body = get(
                    f"http://localhost:{PORT}/probe/cdx?url={quoted}&output=json&limit=5"
                )
                hits = [line for line in cdx_body.decode().splitlines() if line.strip()]
                status, body = get(f"http://localhost:{PORT}/probe/id_/{target}")
                text = body.decode("utf-8", "replace")
                served = next((v for k, v in MARKERS.items() if k in text), None)
                if served is None:
                    served = "DEAD LINK (404)" if status >= 400 else f"?? {text[:28]!r}"
                print(f"  {name:44} {len(hits):>4} {status:>5}  {served}")
                if title.startswith("1.") and status == 200:
                    rescued_controls += 1
                if hits:
                    matched = json.loads(hits[0]).get("url", "")
                    if matched and matched != target:
                        print(f"  {'':44} {'':4} {'':5}  cdx -> {matched}")
            print()

        # The negative control, and the reason this probe is trustworthy. If
        # neither known-rescuable case was rescued then fuzzy matching is not
        # running here, and every 404 above is an artefact of the fixture
        # rather than a fact about pywb.
        if rescued_controls == 0:
            print("INCONCLUSIVE: arm 1 rescued nothing, so fuzzy matching is not")
            print("active in this collection. Arms 2 and 3 prove nothing — fix the")
            print("fixture before reading them.")
            return 1
        print(f"arm 1 rescued {rescued_controls}/2 controls, so fuzzy matching is live.")
        return 0
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
