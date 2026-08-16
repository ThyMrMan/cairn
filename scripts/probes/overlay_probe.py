"""What shape is each archived page: content, gate, or content under a gate?

Written to settle a capture that looked fine and replayed as a wall of content
warnings. The finished capture said `ready`, the profile test said `real
content`, and both were reporting truthfully about the wrong thing — so the
only way left was to read the archived bytes.

It walks a capture's WARCs and sorts every 200 HTML response into three
buckets:

  - **clean** — a page, nothing over it.
  - **gate** — an interstitial served *instead of* a page. `looks_blocked`
    has always caught this: the URL gives it away, or the body is short and
    says the words.
  - **overlay** — a *complete* page with a gate drawn on top. Blogger answers
    200 with the whole post and injects an iframe over it plus
    `body * { visibility: hidden }`, so nothing is missing and nothing
    displays. Both older checks are blind to it by construction: the URL is
    the blog's own, and the body is far past `MAX_INTERSTITIAL_BYTES`.

**The negative control is the third bucket against ground truth.** The probe
scores `overlay_blocked` against a literal substring search for the two
markers, and disagreement is a failure. A detector that agrees with its own
fixtures and misses the bytes it was written for is worth nothing, and the
fixtures for this one were written from these bytes.

Measured on the capture that prompted it, a gated Blogger blog:

    html_200               591
    bucket_overlay         442      <- every real post
    bucket_classic_gate    149      <- the framed gate, recorded at its own URL
    bucket_clean             0
    DISAGREE                 0

442 of 442 posts carried it. The pages were complete — title, body text,
images — under `'interstitialAccepted': false`, which is per-browser state and
not an authentication failure: accepting the warning once inside the browser
profile makes the server stop injecting it.

Local only, no Docker. Point it at any capture's `warc/` directory.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from warcio.archiveiterator import ArchiveIterator  # noqa: E402

from cairn.services import interstitial  # noqa: E402

# What postprocess reads per record, so the probe cannot be more generous.
BODY_LIMIT = 512 * 1024


def classify(warc_dir: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for warc in sorted(warc_dir.glob("*.warc.gz")):
        # browsertrix writes a text-*.warc.gz of extracted text beside the
        # captures; it holds no responses and would only slow this down.
        if warc.name.startswith("text-"):
            continue
        with warc.open("rb") as fh:
            for rec in ArchiveIterator(fh):
                if rec.rec_type != "response" or not rec.http_headers:
                    continue
                if not (rec.http_headers.get_statuscode() or "").startswith("2"):
                    continue
                if "html" not in (rec.http_headers.get_header("Content-Type") or "").lower():
                    continue
                url = rec.rec_headers.get_header("WARC-Target-URI") or ""
                body = rec.content_stream().read(BODY_LIMIT)
                counts["html_200"] += 1

                overlay = interstitial.overlay_blocked(body).blocked
                # Ground truth: the two markers, looked for literally.
                truth = b'id="injected-iframe"' in body and b"visibility: hidden" in body
                if truth != overlay:
                    counts["DISAGREE"] += 1
                    print(f"  !! truth={truth} detected={overlay} {url[:110]}")

                if overlay:
                    counts["bucket_overlay"] += 1
                elif interstitial.looks_blocked(body, url).blocked:
                    counts["bucket_classic_gate"] += 1
                else:
                    counts["bucket_clean"] += 1
    return counts


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <capture>/warc", file=sys.stderr)
        return 2
    warc_dir = Path(argv[1])
    if not warc_dir.is_dir():
        print(f"not a directory: {warc_dir}", file=sys.stderr)
        return 2

    counts = classify(warc_dir)
    if not counts["html_200"]:
        print("no 200 HTML responses found — is this a capture's warc/ directory?")
        return 1
    for key in (
        "html_200",
        "bucket_overlay",
        "bucket_classic_gate",
        "bucket_clean",
        "DISAGREE",
    ):
        print(f"  {key:22} {counts[key]}")

    if counts["DISAGREE"]:
        print("\nFAIL: the detector and a literal marker search disagree.")
        return 1
    if counts["bucket_overlay"]:
        share = counts["bucket_overlay"] / counts["html_200"]
        print(
            f"\n{counts['bucket_overlay']} page(s), {share:.0%} of this capture, are complete "
            "but drawn over.\nAccept the warning once inside the browser profile and capture "
            "again; the profile\nitself is fine, which is why the content arrived at all."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
