"""Can a pagination page nobody crawled be written, indexed and read back?

`pagination_probe.py` establishes that a rejected pagination URL 404s. This one
asks what it would take to fill that gap without crawling it: whether a WARC
record written by hand is indistinguishable, to the index, from one a crawler
produced — and how forgiving the index key is about the many spellings Blogger
emits for one page.

Two arms:

  1. **Which spellings collide?**  Blogger writes `updated-max` with `%2B` or a
     literal `+`, with encoded or plain colons, in varying parameter order, and
     with `&start=` and `&by-date=` on some themes. Every spelling that
     canonicalises to one key is a spelling a single record answers. Every one
     that does not is a link that would 404 against it.
  2. **Does a hand-written record index and read back?**  Written with warcio,
     indexed with cdxj-indexer, then read at the offset the index recorded —
     the same path `replay.read_record` takes.

**The negative control is arm 1's last case.** If everything collided, the
result would be "write one record, all links work", which is too good and would
be wrong: `&start=7&by-date=false` must come out as a *different* key, because
that is what forces a rebuild to mint each record under the exact URL that
links to it. A run where nothing distinguishes has a broken canonicaliser and
should not be believed.

Local only — no Docker, no pywb. Uses the same surt, warcio and cdxj-indexer
the app does.
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

B = "https://example.blogspot.com/search"
TS = "2019-12-09T22:33:00"

SPELLINGS = {
    "plain colon, %2B zone": f"{B}?updated-max={TS}%2B01:00&max-results=7",
    "parameters reordered": f"{B}?max-results=7&updated-max={TS}%2B01:00",
    "literal + not %2B": f"{B}?updated-max={TS}+01:00&max-results=7",
    "encoded colons": f"{B}?updated-max=2019-12-09T22%3A33%3A00%2B01%3A00&max-results=7",
    "trailing #fragment": f"{B}?updated-max={TS}%2B01:00&max-results=7#PageNo=2",
    # These three must NOT join the group above.
    "+ start & by-date": f"{B}?updated-max={TS}%2B01:00&max-results=7&start=7&by-date=false",
    "+ m=1": f"{B}?updated-max={TS}%2B01:00&max-results=7&m=1",
    "negative zone": f"{B}?updated-max={TS}-08:00&max-results=7",
}

# The spellings that must share one key, and the ones that must not.
MUST_COLLIDE = {
    "plain colon, %2B zone",
    "parameters reordered",
    "literal + not %2B",
    "encoded colons",
    "trailing #fragment",
}

TARGET = SPELLINGS["plain colon, %2B zone"]


def arm1() -> bool:
    import surt

    groups: dict[str, list[str]] = {}
    for name, url in SPELLINGS.items():
        groups.setdefault(str(surt.surt(url)), []).append(name)

    print("1. which spellings share an index key?\n")
    for key, names in groups.items():
        print(f"  {key}")
        for name in names:
            print(f"      = {name}")
        print()

    collided = next((set(n) for n in groups.values() if set(n) >= MUST_COLLIDE), set())
    ok = True
    if not collided >= MUST_COLLIDE:
        print(f"  FAIL: these should share one key: {sorted(MUST_COLLIDE - collided)}")
        ok = False
    for name in ("+ start & by-date", "+ m=1", "negative zone"):
        if name in collided:
            print(f"  FAIL: {name!r} collided with the group; a rebuild would mis-key")
            ok = False
    if ok:
        print("  as expected: encoding and order collapse, extra parameters do not.")
    return ok


def arm2(work: Path) -> bool:
    import surt
    from cdxj_indexer.main import write_cdx_index
    from warcio.archiveiterator import ArchiveIterator
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    body = b"<!doctype html><title>Page 2</title><body>SYNTHETIC-PAGE-TWO"
    path = work / "synthetic.warc.gz"

    with open(path, "wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        # Declared for what it is. A synthetic record that looks exactly like a
        # crawled one is the thing to avoid, not the goal.
        writer.write_record(
            writer.create_warcinfo_record(
                path.name,
                {
                    "software": "cairn synthetic-pagination (probe)",
                    "description": "reconstructed index page, not captured from the origin",
                },
            )
        )
        headers = StatusAndHeaders(
            "200 OK",
            [
                ("Content-Type", "text/html; charset=UTF-8"),
                ("Content-Length", str(len(body))),
                ("X-Cairn-Synthetic", "blogger-pagination"),
            ],
            protocol="HTTP/1.1",
        )
        writer.write_record(
            writer.create_warc_record(
                TARGET,
                "response",
                payload=io.BytesIO(body),
                http_headers=headers,
                warc_content_type="application/http; msgtype=response",
            )
        )

    buffer = io.StringIO()
    write_cdx_index(buffer, [str(path)], {"dir_root": str(work)})
    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]

    print("\n2. does a hand-written record index and read back?\n")
    for line in lines:
        print(f"  {line}")

    if not lines:
        print("\n  FAIL: the indexer produced nothing")
        return False

    key, _timestamp, blob = lines[0].split(" ", 2)
    import json

    payload = json.loads(blob)
    ok = True
    if key != str(surt.surt(TARGET)):
        print(f"\n  FAIL: indexed key {key} != surt({TARGET})")
        ok = False

    with open(path, "rb") as fh:
        fh.seek(int(payload["offset"]))
        record = next(iter(ArchiveIterator(fh)))
        read_back = record.content_stream().read()
        synthetic_header = record.http_headers.get_header("X-Cairn-Synthetic")

    print(f"\n  read back at offset {payload['offset']}:")
    print(f"    WARC-Target-URI  {record.rec_headers.get_header('WARC-Target-URI')}")
    print(f"    status           {record.http_headers.get_statuscode()}")
    print(f"    X-Cairn-Synthetic {synthetic_header}")
    print(f"    body             {read_back[:48]!r}")

    if read_back != body:
        print("\n  FAIL: the body read back is not the body written")
        ok = False
    if synthetic_header != "blogger-pagination":
        print("\n  FAIL: the provenance header did not survive the round trip")
        ok = False
    if ok:
        print("\n  indexes and reads back exactly as a crawled record would.")
    return ok


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        results = [arm1(), arm2(Path(tmp))]
    print()
    if all(results):
        print("PASS")
        return 0
    print("FAIL — see above")
    return 1


if __name__ == "__main__":
    sys.exit(main())
