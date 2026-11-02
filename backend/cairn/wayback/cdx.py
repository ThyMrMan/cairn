"""Pulling the Wayback CDX listing for a host, one page at a time.

Paged with `showNumPages` + `page=N`, which is what worked on the first real
survey (fictionmania: 98 pages of ~6,000 rows across a restart, docs/17).
`collapse=urlkey` over a whole prefix timed out, and it keeps the *oldest* row
anyway, so every row comes down and is reduced locally (`survey.py`).

Each page is saved gzipped the moment it arrives, so a job that is cancelled,
crashes or is blocked resumes where it stopped instead of re-asking archive.org
for pages it already answered.
"""

from __future__ import annotations

import gzip
import json
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairn.net.polite import PoliteClient
from cairn.services import storage

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
FIELDS = ("urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length")
META_FILE = "meta.json"
# A CDX page is heavier for archive.org than a snapshot; the survey that worked
# kept ~10 s between them.
CDX_MIN_GAP_S = 10.0


class CdxError(Exception):
    pass


@dataclass(slots=True, frozen=True)
class CdxRow:
    urlkey: str
    timestamp: str
    original: str
    mimetype: str
    statuscode: str
    digest: str
    length: int | None

    @classmethod
    def parse(cls, line: str) -> CdxRow | None:
        parts = line.rstrip("\r\n").split(" ")
        if len(parts) < len(FIELDS):
            return None
        key, ts, original, mime, status, digest, length = parts[: len(FIELDS)]
        if not ts.isdigit():
            return None
        return cls(
            urlkey=key,
            timestamp=ts,
            original=original,
            mimetype=mime.split(";")[0].lower(),
            statuscode=status,
            digest=digest,
            length=int(length) if length.isdigit() else None,
        )

    @property
    def good(self) -> bool:
        """A real 2xx capture, not a revisit pointer or an error."""
        return self.statuscode.startswith("2")


def host_prefix(host: str) -> str:
    return f"{host.lower().strip('/')}/"


def surt_host(host: str) -> str:
    """The host as the CDX server keys it: `www.` is not part of the key."""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def _params(prefix: str) -> dict[str, str | int]:
    return {"url": prefix, "matchType": "prefix"}


def num_pages(client: PoliteClient, prefix: str) -> int:
    response = client.get(
        CDX_ENDPOINT, params={**_params(prefix), "showNumPages": "true"}, reject_html=True
    )
    text = response.body.decode("utf-8", "replace").strip()
    if response.status != 200 or not text.isdigit():
        raise CdxError(
            f"archive.org did not say how many pages {prefix} has (HTTP {response.status})"
        )
    return int(text)


def page_path(directory: Path, page: int) -> Path:
    return directory / f"page-{page:04d}.txt.gz"


def _read_meta(directory: Path) -> dict[str, Any]:
    try:
        meta = json.loads((directory / META_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def pull(
    client: PoliteClient,
    prefix: str,
    directory: Path,
    *,
    refresh: bool = False,
    progress: Callable[[int, int, str], None] = lambda _d, _t, _m: None,
) -> int:
    """Every CDX page for `prefix` into `directory`. Returns the page count.

    Pages already on disk from an earlier pull of the same prefix are kept
    unless `refresh`. A different prefix or field list starts over, because
    rows from two queries cannot be mixed.
    """
    meta = _read_meta(directory)
    if refresh or meta.get("prefix") != prefix or meta.get("fields") != list(FIELDS):
        shutil.rmtree(directory, ignore_errors=True)
        meta = {}
    directory.mkdir(parents=True, exist_ok=True)

    total = int(meta["pages"]) if isinstance(meta.get("pages"), int) else num_pages(client, prefix)
    if not meta:
        storage.write_atomic(
            directory / META_FILE,
            json.dumps({"prefix": prefix, "fields": list(FIELDS), "pages": total}),
        )

    for page in range(total):
        target = page_path(directory, page)
        if target.exists():
            progress(page + 1, total, f"{prefix} page {page + 1} of {total} (saved earlier)")
            continue
        response = client.get(
            CDX_ENDPOINT,
            params={**_params(prefix), "fl": ",".join(FIELDS), "page": page},
            reject_html=True,
        )
        if response.status != 200:
            raise CdxError(f"{prefix} page {page}: HTTP {response.status}")
        storage.write_atomic(target, gzip.compress(response.body))
        rows = response.body.count(b"\n")
        progress(page + 1, total, f"{prefix} page {page + 1} of {total}: {rows:,} rows")
    return total


def complete(directory: Path) -> bool:
    meta = _read_meta(directory)
    pages = meta.get("pages")
    return isinstance(pages, int) and all(page_path(directory, p).exists() for p in range(pages))


def iter_rows(directory: Path) -> Iterator[CdxRow]:
    for path in sorted(directory.glob("page-*.txt.gz")):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                row = CdxRow.parse(line)
                if row is not None:
                    yield row
