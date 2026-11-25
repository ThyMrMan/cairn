"""Memory that must not grow with the archive.

A Wayback import is one capture that grows a segment at a time, and its text
is extracted again after every segment. Extraction held every page in memory
at once, so on fictionmania (a 2 GB text file) the app process reached 12 GB
and stayed there. And every job ever run kept its live-log buffer until
somebody deleted the job.
"""

from __future__ import annotations

import random
import tracemalloc
from collections import Counter
from pathlib import Path

from cairn.config import Settings
from cairn.services import storage, textextract
from cairn.services.events import EV_LOG, EV_STATUS, EventBus
from tests.test_search import plain_page, write_warc

# ── finding the boilerplate without counting every block ──────────────────


def exact_common(keysets: list[set[str]]) -> set[str]:
    pages = len(keysets)
    if pages < textextract.REPEAT_MIN_PAGES:
        return set()
    threshold = max(textextract.REPEAT_MIN_PAGES, int(pages * textextract.REPEAT_SHARE))
    counts: Counter[str] = Counter()
    for keys in keysets:
        counts.update(keys)
    return {k for k, n in counts.items() if n >= threshold}


def test_the_bounded_count_finds_exactly_what_counting_everything_does() -> None:
    rng = random.Random(7)  # noqa: S311 — test data, not security
    for trial in range(200):
        pages = rng.randint(1, 400)
        # Blocks on about 45%, 50%, 55% and 100% of the pages, plus a long
        # tail of blocks unique to one page — the shape of a real capture.
        shares = {"near": 0.45, "half": 0.5, "over": 0.55, "all": 1.0}
        keysets = []
        for n in range(pages):
            keys = {f"unique-{trial}-{n}-{i}" for i in range(rng.randint(0, 120))}
            keys |= {name for name, share in shares.items() if rng.random() < share}
            keysets.append(keys)
        rng.shuffle(keysets)
        found = textextract._common_keys(
            lambda ks=keysets: iter(ks), pages=len(keysets), keys=sum(map(len, keysets))
        )
        assert found == exact_common(keysets), (trial, pages)


# ── extraction stays flat ─────────────────────────────────────────────────


def capture_of(settings: Settings, slug: str, pages: int) -> tuple[str, str]:
    archive_path = f"Unfiled/{slug}"
    storage.ensure_site_dirs(settings, archive_path)
    capture_dir = "20261103T005153Z-wayback-wayback"
    out = storage.ensure_capture_dirs(settings, archive_path, capture_dir)
    story = " ".join(f"word{n}" for n in range(1500))
    # Several WARCs, as a resumed import leaves them.
    per_warc = 250
    for part in range(0, pages, per_warc):
        write_warc(
            out / storage.WARC_DIR / f"wayback-{part:05d}.warc.gz",
            {
                f"https://fm.test/story?id={n}": plain_page(f"s{n}", f"Story {n}", f"{n} {story}")
                for n in range(part, min(part + per_warc, pages))
            },
        )
    return archive_path, capture_dir


def peak_of_extraction(settings: Settings, slug: str, pages: int) -> tuple[int, int]:
    archive_path, capture_dir = capture_of(settings, slug, pages)
    tracemalloc.start()
    try:
        result = textextract.extract_capture(settings, archive_path, capture_dir)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.pages == pages
    assert result.path is not None
    return peak, result.path.stat().st_size


def test_extraction_memory_does_not_grow_with_the_capture(settings: Settings) -> None:
    small_peak, small_size = peak_of_extraction(settings, "small", 250)
    big_peak, big_size = peak_of_extraction(settings, "big", 2000)
    assert big_size > 7 * small_size
    # Eight times the text, and the peak barely moves: what is left is the
    # URL-to-offset map, tens of bytes per page.
    assert big_peak < small_peak + 2_000_000, (small_peak, big_peak)
    assert big_peak < big_size / 4, (big_peak, big_size)


def test_extraction_still_drops_the_template_and_keeps_the_stories(settings: Settings) -> None:
    archive_path, capture_dir = capture_of(settings, "kept", 300)
    result = textextract.extract_capture(settings, archive_path, capture_dir)
    assert result.dropped_blocks > 0
    pages = list(result.iter_pages())
    assert len(pages) == 300 == result.pages
    assert all("About this blog" not in p.text for p in pages)
    assert any(p.text.startswith("Story 7") or "7 word0" in p.text for p in pages)
    assert result.words == sum(len(p.text.split()) for p in pages)
    # Offsets point at each page's own line, and no spool is left behind.
    for page in pages[:20]:
        again = textextract.read_page_at(result.path, page.offset, page.length)  # type: ignore[arg-type]
        assert again is not None and again.url == page.url
    assert not list(result.path.parent.glob("*.spool"))  # type: ignore[union-attr]


def test_the_later_copy_of_a_page_wins(settings: Settings, tmp_path: Path) -> None:
    archive_path = "Unfiled/twice"
    storage.ensure_site_dirs(settings, archive_path)
    out = storage.ensure_capture_dirs(settings, archive_path, "20261103T005153Z-wayback-wayback")
    url = "https://fm.test/story?id=1"
    old = plain_page("a", "Old", "the first text of the story")
    new = plain_page("b", "New", "the second text of the story")
    write_warc(out / storage.WARC_DIR / "a.warc.gz", {url: old})
    write_warc(out / storage.WARC_DIR / "b.warc.gz", {url: new})
    result = textextract.extract_capture(settings, archive_path, out.name)
    (page,) = list(result.iter_pages())
    assert page.title == "New" and "the second text of the story" in page.text


# ── finished jobs let go of their live log ────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_finished_jobs_buffer_is_released_after_a_while() -> None:
    clock = Clock()
    bus = EventBus(retain_s=600, clock=clock)
    for job in (1, 2, 3):
        bus.publish(job, EV_LOG, {"msg": "hello"})
    bus.publish(1, EV_STATUS, {"status": "ok"})
    bus.publish(2, EV_STATUS, {"status": "paused"})

    # Still there for a tab that reconnects to see how it ended.
    clock.now += 300
    bus.publish(None, EV_LOG, {"msg": "tick"})
    assert bus.history(1) and bus.history(2)

    clock.now += 400
    bus.publish(None, EV_LOG, {"msg": "tick"})
    assert not bus.history(1) and not bus.history(2)
    assert bus.history(3), "a job still running keeps its buffer"


def test_a_job_that_starts_again_is_not_released() -> None:
    clock = Clock()
    bus = EventBus(retain_s=600, clock=clock)
    bus.publish(5, EV_STATUS, {"status": "interrupted"})
    bus.publish(5, EV_STATUS, {"status": "running"})
    clock.now += 3600
    bus.publish(None, EV_LOG, {"msg": "tick"})
    assert bus.history(5)
