"""Downloading the video an archived post embedded.

Neither wget nor a browser crawler captures a video stream, so an archived
post with a YouTube embed is a page with a dead rectangle in it — and it is
the gap nobody discovers until years later, when the video is gone and the
archive turns out not to have contained it after all.

**Off by default, per site, with the number in front of you.** A blog's text
and images are megabytes; its embedded video is gigabytes. This is the one
feature here that can quietly fill a disk, so it is switched on per site and
bounded three ways — per item, per capture, and by count — and every refusal
is reported rather than skipped silently.

**No ffmpeg, deliberately.** yt-dlp merges separate video and audio streams
with ffmpeg, and Debian's ffmpeg is 481 MB across 200 packages — measured —
against yt-dlp's own 25 MB. On an image already at 1.7 GB that is a 28%
increase to raise an archived clip from a muxed 720p to a merged 1080p, and
the archival value is overwhelmingly in the clip existing at all. So the
default format asks for a single file that needs no merging. The format string
is configurable for anyone who disagrees; without ffmpeg, a format that
requires merging fails with yt-dlp saying exactly that.

**These URLs are not the user's.** Every other URL this application fetches
was typed by the person running it — a seed, a feed, a profile's verify URL.
These come out of archived HTML written by somebody else, which makes them the
one genuinely attacker-controlled fetch target in the system, so they are
checked against the private ranges docs/11 lists before anything connects.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cairn.config import Settings
from cairn.logging import get_logger
from cairn.services import storage

log = get_logger(__name__)

MEDIA_DIR = "media"
SETTING = "media.download"

DEFAULT_POLICY: dict[str, Any] = {
    "enabled": False,
    # Per item, per capture, and how many. All three, because any one alone
    # has an obvious way to be exceeded.
    "max_item_bytes": 256 * 1024 * 1024,
    "max_total_bytes": 2 * 1024 * 1024 * 1024,
    "max_items": 20,
    # Single-file formats only: see the module docstring on ffmpeg.
    "format": "best[ext=mp4]/best[ext=webm]/best",
    # Fetching a video from a host on your own LAN because an archived page
    # said to is the one thing here nobody asked for.
    "allow_private_hosts": False,
}

# Platforms whose embeds are worth following. Deliberately a list rather than
# "any iframe": an iframe is also how a page embeds a comment widget, a map
# and an advert, and handing all of those to a downloader is how a capture
# ends up fetching from thirty hosts nobody meant to involve.
EMBED_HOSTS = (
    "youtube.com",
    "youtube-nocookie.com",
    "youtu.be",
    "player.vimeo.com",
    "vimeo.com",
    "dailymotion.com",
    "archive.org",
    "soundcloud.com",
    "bandcamp.com",
    "twitch.tv",
    "ted.com",
)

_IFRAME = re.compile(r"""<iframe\b[^>]*?\bsrc\s*=\s*["']([^"'>]+)["']""", re.IGNORECASE)
_VIDEO = re.compile(r"""<(?:video|audio)\b[^>]*?\bsrc\s*=\s*["']([^"'>]+)["']""", re.IGNORECASE)
_SOURCE = re.compile(r"""<source\b[^>]*?\bsrc\s*=\s*["']([^"'>]+)["'][^>]*?>""", re.IGNORECASE)

MEDIA_EXTENSIONS = (".mp4", ".webm", ".m4v", ".mov", ".mp3", ".m4a", ".ogg", ".ogv", ".flv")


class MediaError(RuntimeError):
    """The media could not be fetched."""


@dataclass(slots=True)
class Item:
    url: str
    status: str  # downloaded | skipped | failed
    reason: str = ""
    filename: str = ""
    bytes: int = 0
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "reason": self.reason,
            "filename": self.filename,
            "bytes": self.bytes,
            "title": self.title,
        }


@dataclass(slots=True)
class MediaResult:
    found: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    items: list[Item] = field(default_factory=list)

    def add(self, item: Item) -> None:
        self.items.append(item)
        if item.status == "downloaded":
            self.downloaded += 1
            self.bytes += item.bytes
        elif item.status == "failed":
            self.failed += 1
        else:
            self.skipped += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "bytes": self.bytes,
            "items": [i.to_dict() for i in self.items],
        }


# ── finding it ───────────────────────────────────────────────────────────


def find_embeds(html: bytes | str, base_url: str) -> list[str]:
    """Media URLs an archived page refers to, in document order.

    `<video>` and `<source>` are unambiguous. An `<iframe>` is only followed
    when its host is one of `EMBED_HOSTS` — every page has iframes, and most
    of them are comment widgets and adverts.
    """
    from urllib.parse import urljoin

    from cairn.services.htmlrefs import unescape_css

    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    found: list[str] = []
    seen: set[str] = set()

    def offer(raw: str, *, require_host: bool) -> None:
        candidate = urljoin(base_url, unescape_css(raw.strip()))
        if candidate in seen:
            return
        parts = urlsplit(candidate)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return
        if require_host and not _is_embed_host(parts.hostname):
            return
        if not require_host and not candidate.split("?")[0].lower().endswith(MEDIA_EXTENSIONS):
            return
        seen.add(candidate)
        found.append(candidate)

    for match in _VIDEO.finditer(text):
        offer(match.group(1), require_host=False)
    for match in _SOURCE.finditer(text):
        offer(match.group(1), require_host=False)
    for match in _IFRAME.finditer(text):
        offer(match.group(1), require_host=True)
    return found


def _is_embed_host(host: str) -> bool:
    host = host.lower().removeprefix("www.")
    return any(host == known or host.endswith(f".{known}") for known in EMBED_HOSTS)


# ── the guard ────────────────────────────────────────────────────────────


def resolve_is_public(host: str) -> tuple[bool, str]:
    """Whether a hostname resolves entirely to public addresses.

    Every address is checked, not just the first: a name that resolves to one
    public and one private address is a name that can be connected to
    privately. docs/11 lists the ranges; this is where they are enforced.

    A name that does not resolve is refused as well. There is nothing to fetch
    and nothing to decide about, and reporting "could not resolve" is more
    useful than a connection error thirty seconds later.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return False, f"{host} does not resolve ({exc.strerror or exc})"

    # sockaddr[0] is the address for AF_INET and AF_INET6; the annotation
    # covers every family, including ones whose first field is an int.
    addresses = {str(info[4][0]) for info in infos}
    if not addresses:  # pragma: no cover — getaddrinfo raises instead
        return False, f"{host} does not resolve"

    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:  # pragma: no cover — getaddrinfo returned nonsense
            return False, f"{host} resolved to something that is not an address: {raw!r}"
        if not address.is_global or address.is_multicast:
            return False, f"{host} resolves to {address}, which is not a public address"
    return True, ""


def check_url(url: str, *, allow_private: bool) -> str:
    """`""` if it may be fetched, otherwise why not."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return f"{parts.scheme or 'that'} is not a scheme this fetches"
    if not parts.hostname:
        return "no host in that URL"
    if allow_private:
        return ""
    ok, reason = resolve_is_public(parts.hostname)
    return "" if ok else reason


# ── fetching it ──────────────────────────────────────────────────────────


def media_dir(settings: Settings, archive_path: str, capture_dir: str) -> Path:
    root = storage.site_dir(settings, archive_path) / storage.DERIVED_DIR / MEDIA_DIR
    return storage.resolve_within(root, capture_dir)


def available() -> tuple[bool, str]:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return False, "yt-dlp is not installed in this image"
    return True, ""


def download(
    urls: list[str],
    into: Path,
    policy: dict[str, Any],
    *,
    progress: Any = None,
) -> MediaResult:
    """Fetch what the policy permits, and say what it refused and why."""
    result = MediaResult(found=len(urls))
    ok, reason = available()
    if not ok:
        for url in urls:
            result.add(Item(url=url, status="failed", reason=reason))
        return result

    import yt_dlp

    max_items = int(policy.get("max_items") or 0)
    max_item = int(policy.get("max_item_bytes") or 0)
    max_total = int(policy.get("max_total_bytes") or 0)
    allow_private = bool(policy.get("allow_private_hosts"))
    into.mkdir(parents=True, exist_ok=True)

    for url in urls:
        if max_items and result.downloaded >= max_items:
            result.add(Item(url=url, status="skipped", reason=f"past the limit of {max_items}"))
            continue
        if max_total and result.bytes >= max_total:
            result.add(
                Item(url=url, status="skipped", reason="this capture's media budget is spent")
            )
            continue
        refusal = check_url(url, allow_private=allow_private)
        if refusal:
            result.add(Item(url=url, status="skipped", reason=refusal))
            continue

        if progress is not None:
            progress(url)
        result.add(_one(yt_dlp, url, into, policy, max_item))
    return result


def _one(yt_dlp: Any, url: str, into: Path, policy: dict[str, Any], max_item: int) -> Item:
    options: dict[str, Any] = {
        "outtmpl": str(into / "%(extractor)s-%(id)s.%(ext)s"),
        "format": str(policy.get("format") or DEFAULT_POLICY["format"]),
        # One embed is one video. Without this, a link to a video that happens
        # to sit in a playlist fetches the playlist.
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 2,
        "socket_timeout": 30,
        "ignoreerrors": False,
        # No archive should be written by an inherited proxy setting.
        "proxy": "",
    }
    if max_item:
        options["max_filesize"] = max_item

    before = {p.name for p in into.iterdir()} if into.is_dir() else set()
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        return Item(url=url, status="failed", reason=_short(str(exc)))

    written = sorted(p for p in into.iterdir() if p.name not in before)
    if not written:
        # yt-dlp reports success for a download it declined on max_filesize.
        return Item(
            url=url,
            status="skipped",
            reason="nothing was written — most likely larger than the per-item limit",
            title=str((info or {}).get("title") or ""),
        )
    path = written[0]
    return Item(
        url=url,
        status="downloaded",
        filename=path.name,
        bytes=path.stat().st_size,
        title=str((info or {}).get("title") or ""),
    )


def _short(message: str) -> str:
    cleaned = " ".join(message.replace("ERROR:", "").split())
    return cleaned[:200]


def policy_for(session: Any, site: Any) -> dict[str, Any]:
    """The site's media policy, over the instance default, over the built-in."""
    from cairn.services import settings_store

    instance: dict[str, Any] = settings_store.get(session, SETTING, {}) or {}
    policy = dict(DEFAULT_POLICY)
    policy.update(instance)
    override = (site.scope_settings or {}).get("media")
    if isinstance(override, dict):
        policy.update(override)
    return policy


# ── what a site has actually collected ───────────────────────────────────
#
# Read back from the capture manifests rather than from the directory, and the
# difference is the entire point: the files on disk are only the successes. A
# capture that found six embeds and was refused five of them leaves one file
# and five explanations, and the explanations are what somebody needs — "the
# video is not here" is the thing worth finding out now rather than in five
# years, which is the reason this feature exists at all.


ALLOWED_EXTENSIONS = frozenset(
    {".mp4", ".webm", ".m4v", ".mov", ".mp3", ".m4a", ".ogg", ".ogv", ".opus", ".wav", ".flac"}
)

# What a browser may be told a file is. Keyed on extension, and deliberately
# short: yt-dlp names the file from `%(ext)s`, which comes from the remote, so
# the extension is not ours. An allowlist means the worst a hostile extension
# can do is fail to be served.
CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}


def content_type(filename: str) -> str:
    """The one type this will claim a file is, or "" to refuse it."""
    return CONTENT_TYPES.get(Path(filename).suffix.lower(), "")


def file_path(settings: Settings, archive_path: str, capture_dir: str, filename: str) -> Path:
    """A downloaded file's path, refusing anything outside the media directory.

    `filename` reaches here from a URL. `resolve_within` is what stops `..`
    and a planted symlink; the extension check is what stops the file being
    served as something a browser would execute.
    """
    if not content_type(filename):
        raise MediaError(f"{filename!r} is not a media file this will serve")
    return storage.resolve_within(media_dir(settings, archive_path, capture_dir), filename)


def library(settings: Settings, site: Any, captures: list[Any]) -> dict[str, Any]:
    """Every item these captures downloaded or refused, newest capture first."""
    import json

    items: list[dict[str, Any]] = []
    total = 0
    for capture in captures:
        path = storage.manifest_path(settings, site.archive_path, capture.dir_name)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        block = ((manifest.get("stats") or {}).get("media")) or {}
        for raw in block.get("items") or []:
            if not isinstance(raw, dict):  # pragma: no cover — defensive
                continue
            entry = dict(raw)
            entry["capture_id"] = capture.id
            entry["capture_dir"] = capture.dir_name
            name = str(entry.get("filename") or "")
            # A file recorded in a manifest and since deleted — by a retention
            # sweep, or by hand — must not be offered as a link that 404s.
            entry["playable"] = bool(
                name
                and content_type(name)
                and (media_dir(settings, site.archive_path, capture.dir_name) / name).is_file()
            )
            if entry.get("status") == "downloaded":
                total += int(entry.get("bytes") or 0)
            items.append(entry)
    return {"items": items, "total_bytes": total}
