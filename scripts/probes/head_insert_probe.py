"""Can pywb's head insert be extended without carrying a copy of it?

Replay needs to inject a script into every archived page (docs/07: uncovering
a page a site drew a content warning over). The obvious way is to override
`head_insert.html`, and the obvious way is wrong: that template carries
wombat's bootstrap and is version-coupled to the pywb in the image. A copy
would drift on the next upgrade and replay would keep serving pages with the
URL rewriting quietly gone -- which looks fine until every link on a replayed
page reaches the live site.

The alternative under test: point `head_insert_html` at a *differently named*
template that does `{% include "head_insert.html" %}`. pywb builds its Jinja
environment from a ChoiceLoader over the filesystem templates directory and
then its own package, so the include should resolve to pywb's original rather
than recursing into itself.

Two arms against a one-page collection, served by the real pywb in the image:

  1. **Baseline** -- pywb's default config. Records what pywb inserts alone.
  2. **Extended** -- `head_insert_html: cairn_head_insert.html`, using the
     template `replay.py` actually generates.

**Arm 1 is the control that matters.** "Our marker is present" proves nothing
on its own: if wombat's bootstrap vanished along with the override, the page
would still contain our script and replay would still be broken. Arm 1 also
proves the marker is not somehow present already, which would mean the two
arms were never isolated.

What this probe does *not* answer is whether the script behaves correctly --
it checks what pywb serves, and the uncovering happens in the browser after
the fact. That half is covered by the tests in `test_replay.py` and by the
in-browser verification recorded in docs/07.

Needs Docker and `cairn:latest` (override with `CAIRN_IMAGE`). The page is
synthetic, so nothing outside this repo is required.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

IMAGE = os.environ.get("CAIRN_IMAGE", "cairn:latest")
PAGE = "https://example.blogspot.com/2020/07/a-post.html"
PORT = int(os.environ.get("CAIRN_PROBE_PORT", "8973"))

# The shape a real Blogger page carries, reduced to the parts that matter: a
# gate-framed iframe made visible over a body whose every element is hidden.
OVERLAID_PAGE = (
    b"<html><head><title>A post</title></head>"
    b"<body class='loading'><iframe id=\"injected-iframe\" "
    b'src="https://www.blogger.com/interstitial/blog?u=' + PAGE.encode() + b'" '
    b'style="position:absolute; z-index:999; visibility:visible"></iframe>'
    b"<style>body { _height: 100%; } body * { visibility: hidden; }</style>"
    b"<div class='post-body'>" + b"the real post, archived in full. " * 400 + b"</div>"
    b"</body></html>"
)


def build_collection(work: Path) -> None:
    from cdxj_indexer.main import CDXJIndexer
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    archive = work / "collections" / "probe" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    warc = archive / "page.warc.gz"
    with warc.open("wb") as fh:
        writer = WARCWriter(fh, gzip=True)
        headers = StatusAndHeaders(
            "200 OK", [("Content-Type", "text/html; charset=UTF-8")], protocol="HTTP/1.1"
        )
        writer.write_record(
            writer.create_warc_record(
                PAGE, "response", payload=io.BytesIO(OVERLAID_PAGE), http_headers=headers
            )
        )

    # pywb serves from the index, not the WARC, and expects `indexes/` beside
    # `archive/` -- the same layout `replay.link_collection` builds.
    indexes = archive.parent / "indexes"
    indexes.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    CDXJIndexer(inputs=[str(warc)], output=buf).process_all()
    lines = sorted(buf.getvalue().splitlines())
    (indexes / "index.cdxj").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  {len(OVERLAID_PAGE)} byte page, {len(lines)} record(s) indexed")


def write_config(work: Path, *, extended: bool) -> None:
    """The same keys `replay.write_config` writes, minus the port."""
    lines = [
        "collections_root: collections",
        "framed_replay: true",
        "enable_cdx_api: true",
        "enable_memento: true",
        "enable_content_security_policy: true",
    ]
    if extended:
        import json

        from cairn.services import interstitial, replay

        lines.append(f"head_insert_html: {replay.HEAD_INSERT_FILE}")
        templates = work / replay.TEMPLATES_DIR
        templates.mkdir(parents=True, exist_ok=True)
        # The template the app really generates, not a stand-in: a probe that
        # passes against a mock of the thing being shipped has tested nothing.
        body = replay._HEAD_INSERT_TEMPLATE.replace(
            "__MARKERS__", json.dumps(list(interstitial.URL_MARKERS))
        )
        (templates / replay.HEAD_INSERT_FILE).write_text(body, encoding="utf-8")
    (work / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def serve_and_fetch(work: Path, label: str) -> str:
    name = f"cairn-head-insert-probe-{int(time.time() * 1000)}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-v",
            f"{work}:/probe",
            "-w",
            "/probe",
            "-p",
            f"{PORT}:{PORT}",
            "--entrypoint",
            "/opt/venv/bin/wayback",
            IMAGE,
            "--port",
            str(PORT),
            "--bind",
            "0.0.0.0",
        ],
        check=True,
        capture_output=True,
    )
    try:
        url = f"http://127.0.0.1:{PORT}/probe/mp_/{PAGE}"
        last = ""
        for _ in range(40):
            time.sleep(1)
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    return resp.read().decode("utf-8", "replace")
            except Exception as exc:
                last = str(exc)
        raise SystemExit(f"{label}: pywb never answered ({last})")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def facts_of(html: str) -> dict[str, bool]:
    return {
        "pywb wombat bootstrap": "wbinfo" in html and "wombat" in html.lower(),
        "pywb rewrote the gate iframe": bool(
            re.search(r'src="[^"]*/probe/[^"]*interstitial', html)
        ),
        "cairn uncover script": "data-cairn-overlay-removed" in html,
    }


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="cairn-head-insert-"))
    try:
        print(f"building a one-page collection in {work}")
        build_collection(work)

        results = {}
        for label, extended in (("arm 1: pywb default", False), ("arm 2: cairn template", True)):
            write_config(work, extended=extended)
            facts = facts_of(serve_and_fetch(work, label))
            results[label] = facts
            print(f"\n== {label}")
            for key, val in facts.items():
                print(f"   {'yes' if val else 'no ':4} {key}")

        base = results["arm 1: pywb default"]
        ext = results["arm 2: cairn template"]
        print("\n== verdict")
        problems = []
        if not base["pywb wombat bootstrap"]:
            problems.append("baseline has no wombat -- the probe itself is wrong")
        if base["cairn uncover script"]:
            problems.append("baseline already carries our marker -- the arms are not isolated")
        if not ext["pywb wombat bootstrap"]:
            problems.append("the include dropped pywb's own insert")
        if not ext["cairn uncover script"]:
            problems.append("our template did not render")
        for problem in problems:
            print(f"   FAIL: {problem}")
        if not problems:
            print("   pywb's insert survives and ours is added, without copying the template")
        return 1 if problems else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
