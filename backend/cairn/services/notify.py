"""Telling somebody something happened (docs/08).

New content appearing in an archive is exactly the kind of thing worth a push,
and a scheduler nobody hears from is one nobody trusts. Three transports, each
chosen for a different reason:

**ntfy**, natively, because it is what a self-hoster on Unraid most likely
already runs and it needs no more than an HTTP POST.

**A generic webhook**, because it reaches Discord, Slack, Gotify and Home
Assistant without this module knowing anything about them.

**Any Apprise URL**, optional at import time like Playwright and pywb, because
Apprise is the de-facto notation for "send this to the thing I use" and
re-implementing eighty services would be absurd. A checkout without it says so
rather than failing at the moment something goes wrong — which is precisely the
moment a notification layer must not add a second failure.

Every event is opt-in per event, and the defaults follow docs/08: the ones that
mean something is broken are on, the ones that fire on every successful poll
are off.

**A notification is never allowed to fail the thing that triggered it.** Every
send is best-effort and swallows its errors into the log. A capture that
finished must not be reported as failed because a webhook was down.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session, sessionmaker

from cairn.logging import get_logger
from cairn.services import settings_store

log = get_logger(__name__)

TIMEOUT_S = 10.0
MAX_BODY_CHARS = 4000

# ── the events, and whether they are on by default ───────────────────────

CAPTURE_FAILED = "capture_failed"
FEED_DISABLED = "feed_disabled"
PROFILE_EXPIRING = "profile_expiring"
INTERSTITIAL_DETECTED = "interstitial_detected"
DISK_LOW = "disk_low"
INTEGRITY_MISMATCH = "integrity_mismatch"
ITEMS_CAPTURED = "items_captured"
NEW_HOSTS = "new_hosts"
URLS_DISAPPEARED = "urls_disappeared"
DIGEST = "digest"

EVENTS: dict[str, tuple[str, bool]] = {
    CAPTURE_FAILED: ("A capture failed", True),
    FEED_DISABLED: ("A feed was turned off after repeated failures", True),
    PROFILE_EXPIRING: ("An access profile expires within 24 hours", True),
    INTERSTITIAL_DETECTED: ("An interstitial was detected during a crawl", True),
    DISK_LOW: ("Free disk space fell below the floor", True),
    INTEGRITY_MISMATCH: ("An integrity check found a mismatch", True),
    # Noisy by nature: a busy blog would push several times a day and the
    # information is already on the site's page.
    ITEMS_CAPTURED: ("New items were captured", False),
    NEW_HOSTS: ("Discovery found new hosts", False),
    # The one that matters. It is the moment the archive paid for itself, and
    # a reason to protect that capture from any retention policy.
    URLS_DISAPPEARED: ("Archived URLs disappeared from the site", True),
    # On by default and the only *periodic* event here. Everything else fires
    # when something happens; this one fires when nothing has, which is the
    # failure an unattended archiver actually has (docs/13).
    DIGEST: ("A periodic summary of what happened, and what quietly did not", True),
}

SETTING_PREFIX = "notify.on_"
TARGETS_SETTING = "notify.targets"


def event_setting(event: str) -> str:
    return f"{SETTING_PREFIX}{event}"


def defaults() -> dict[str, object]:
    return {event_setting(event): on for event, (_label, on) in EVENTS.items()}


# ── targets ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Target:
    """One place to send to.

    `url` carries the whole configuration, which is what makes a target one
    string in the UI rather than a form per transport:

      - `ntfy://…` or an `https://ntfy.sh/topic` URL → ntfy
      - any other `http(s)://` → a JSON POST
      - anything else with a scheme Apprise knows → Apprise
    """

    url: str
    enabled: bool = True
    label: str = ""

    @property
    def kind(self) -> str:
        scheme = urlsplit(self.url).scheme.lower()
        if scheme in ("ntfy", "ntfys"):
            return "ntfy"
        if scheme in ("http", "https"):
            return "ntfy" if _looks_like_ntfy(self.url) else "webhook"
        return "apprise"


def _looks_like_ntfy(url: str) -> bool:
    """An https URL is ntfy when it says so; guessing would be wrong.

    A bare `https://host/topic` is indistinguishable from a webhook, and
    sending ntfy's header protocol to a webhook produces a 400 nobody can
    explain. So the marker is explicit: the `ntfy://` scheme, or a host that
    is literally ntfy.sh.
    """
    return (urlsplit(url).hostname or "").lower() in ("ntfy.sh", "www.ntfy.sh")


def targets(session: Session) -> list[Target]:
    raw: Any = settings_store.get(session, TARGETS_SETTING, [])
    out: list[Target] = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, str):
            out.append(Target(url=entry))
        elif isinstance(entry, dict) and entry.get("url"):
            out.append(
                Target(
                    url=str(entry["url"]),
                    enabled=bool(entry.get("enabled", True)),
                    label=str(entry.get("label") or ""),
                )
            )
    return out


def set_targets(session: Session, values: list[dict[str, Any]]) -> list[Target]:
    cleaned = [
        {
            "url": str(entry["url"]).strip(),
            "enabled": bool(entry.get("enabled", True)),
            "label": str(entry.get("label") or "")[:80],
        }
        for entry in values
        if str(entry.get("url") or "").strip()
    ]
    settings_store.put(session, TARGETS_SETTING, cleaned)
    return targets(session)


def wants(session: Session, event: str) -> bool:
    fallback = EVENTS.get(event, ("", False))[1]
    return bool(settings_store.get(session, event_setting(event), fallback))


# ── sending ──────────────────────────────────────────────────────────────


async def send(
    sessions: sessionmaker[Session],
    event: str,
    *,
    title: str,
    body: str = "",
    priority: str = "default",
) -> int:
    """Deliver one event to every enabled target. Returns how many succeeded.

    Takes a session factory rather than a session: this is called from async
    code that has no session open, and reading the configuration is a database
    hit that must not happen on the event loop.
    """
    configured = await asyncio.to_thread(_configuration, sessions, event)
    if configured is None:
        return 0

    delivered = 0
    for target in configured:
        try:
            if await _deliver(target, title=title, body=body, priority=priority):
                delivered += 1
        except Exception as exc:
            log.warning(
                "notification could not be delivered",
                extra={"event": event, "kind": target.kind, "err": str(exc)[:200]},
            )
    if delivered:
        log.info("notification sent", extra={"event": event, "targets": delivered})
    return delivered


def _configuration(sessions: sessionmaker[Session], event: str) -> list[Target] | None:
    with sessions() as session:
        if not wants(session, event):
            return None
        enabled = [t for t in targets(session) if t.enabled]
        return enabled or None


async def _deliver(target: Target, *, title: str, body: str, priority: str) -> bool:
    text = body[:MAX_BODY_CHARS]
    if target.kind == "ntfy":
        return await _send_ntfy(target.url, title, text, priority)
    if target.kind == "webhook":
        return await _send_webhook(target.url, title, text, priority)
    return await _send_apprise(target.url, title, text)


async def _send_ntfy(url: str, title: str, body: str, priority: str) -> bool:
    import httpx

    endpoint = url
    scheme = urlsplit(url).scheme.lower()
    if scheme in ("ntfy", "ntfys"):
        endpoint = ("https://" if scheme == "ntfys" else "http://") + url.split("://", 1)[1]

    async with httpx.AsyncClient(timeout=TIMEOUT_S, trust_env=False) as client:
        response = await client.post(
            endpoint,
            content=(body or title).encode("utf-8"),
            headers={
                # Latin-1 only: ntfy puts these in HTTP headers, and a smart
                # quote in a post title would otherwise make the request itself
                # invalid rather than the notification merely ugly.
                "Title": _header_safe(title),
                "Priority": priority,
                "Tags": "package",
            },
        )
    return response.status_code < 400


async def _send_webhook(url: str, title: str, body: str, priority: str) -> bool:
    import httpx

    async with httpx.AsyncClient(timeout=TIMEOUT_S, trust_env=False) as client:
        response = await client.post(
            url,
            json={
                "title": title,
                "message": body,
                "priority": priority,
                "source": "cairn",
                # Discord and Slack both read `content`/`text`; including them
                # makes their default webhook URLs work with no adapter.
                "content": f"{title}\n{body}".strip(),
                "text": f"{title}\n{body}".strip(),
            },
        )
    return response.status_code < 400


async def _send_apprise(url: str, title: str, body: str) -> bool:
    try:
        import apprise
    except ImportError:
        log.warning(
            "a notification target needs Apprise, which is not installed. It ships "
            "in the Docker image; a source checkout needs `pip install apprise`.",
            extra={"scheme": urlsplit(url).scheme},
        )
        return False

    def run() -> bool:
        sender = apprise.Apprise()
        if not sender.add(url):
            log.warning("Apprise did not recognise that target URL")
            return False
        return bool(sender.notify(title=title, body=body or title))

    # Apprise is synchronous and uses requests; off the loop it goes.
    return await asyncio.to_thread(run)


def _header_safe(value: str) -> str:
    return value.encode("ascii", errors="replace").decode("ascii")[:200]


async def send_test(configured: list[Target]) -> tuple[int, list[str]]:
    """Send one message to each target and report what happened.

    Not gated on any event's opt-in. The question being asked is "does my
    configuration work", and answering it with silence because the event
    happened to be switched off would be the worst possible reply.
    """
    delivered = 0
    problems: list[str] = []
    for target in configured:
        try:
            if await _deliver(
                target,
                title="Cairn test notification",
                body="If you can read this, notifications are working.",
                priority="default",
            ):
                delivered += 1
            else:
                problems.append(f"{target.kind}: the target rejected the message")
        except Exception as exc:
            problems.append(f"{target.kind}: {str(exc)[:150]}")
    return delivered, problems


def apprise_available() -> bool:
    try:
        import apprise  # noqa: F401
    except ImportError:
        return False
    return True
