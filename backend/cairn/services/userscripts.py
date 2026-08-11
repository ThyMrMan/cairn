"""Reading a `.user.js` well enough to say why it will not work.

The failure this exists to prevent: a script is uploaded, the mint runs, no
cookies come out, and the report says "no cookies produced". That is true and
tells nobody anything. Almost always the cause is knowable before the browser
starts — the `@match` never covered the verify URL, or the script needs a
`@grant` the shim does not provide, or it pulls in a `@require` that is not
being fetched.

So the metadata block is parsed and checked up front, and every finding is
phrased as the thing the person has to do about it.

Match patterns are Chrome's, which Tampermonkey follows: `scheme://host/path`
where the scheme may be `*`, the host may lead with `*.`, and the path is a
glob. They are not regexes and not fnmatch — `*` in a host means "this domain
or any subdomain", while `*` in a path means "any characters". Treating them
as one thing is how you get a checker that passes everything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_SCRIPT_BYTES = 512 * 1024
SHIM_FILE = Path(__file__).resolve().parent.parent / "assets" / "gm_shim.js"

_BLOCK = re.compile(r"//\s*==UserScript==\s*\n(.*?)//\s*==/UserScript==", re.DOTALL | re.IGNORECASE)
_DIRECTIVE = re.compile(r"^//\s*@(\S+)\s*(.*)$")

# What the shim in assets/gm_shim.js actually provides. Anything else that is
# granted gets a warning naming it, rather than a runtime failure inside a
# page nobody is watching.
SUPPORTED_GRANTS = frozenset(
    {
        "none",
        "unsafeWindow",
        "GM_setValue",
        "GM_getValue",
        "GM_deleteValue",
        "GM_listValues",
        "GM_addStyle",
        "GM_log",
        "GM_xmlhttpRequest",
        "GM_info",
        "GM.setValue",
        "GM.getValue",
        "GM.deleteValue",
        "GM.listValues",
        "GM.addStyle",
        "GM.xmlHttpRequest",
    }
)


class UserscriptError(ValueError):
    """The upload is not a usable userscript."""


@dataclass(slots=True)
class Userscript:
    body: str
    name: str | None = None
    version: str | None = None
    namespace: str | None = None
    description: str | None = None
    run_at: str = "document-idle"
    matches: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    grants: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_metadata(self) -> bool:
        return bool(self.name or self.matches or self.includes or self.grants)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "run_at": self.run_at,
            "matches": self.matches,
            "includes": self.includes,
            "excludes": self.excludes,
            "grants": self.grants,
            "requires": self.requires,
            "resources": self.resources,
            "warnings": self.warnings,
        }


def parse(text: str) -> Userscript:
    if len(text.encode("utf-8", errors="ignore")) > MAX_SCRIPT_BYTES:
        raise UserscriptError("That file is larger than 512 KB; it is not a userscript.")
    if not text.strip():
        raise UserscriptError("The file is empty.")

    script = Userscript(body=text)
    block = _BLOCK.search(text)
    if block is None:
        # Not fatal. A bare snippet with no metadata block still runs, and
        # rejecting it would be this tool being stricter than Tampermonkey.
        script.warnings.append(
            "No ==UserScript== metadata block found. The script will still run, but "
            "nothing can be checked against the site before it does."
        )
        return script

    for line in block.group(1).splitlines():
        directive = _DIRECTIVE.match(line.strip())
        if directive is None:
            continue
        key, value = directive.group(1).lower(), directive.group(2).strip()
        if key == "name" and not script.name:
            script.name = value
        elif key == "version":
            script.version = value
        elif key == "namespace":
            script.namespace = value
        elif key == "description" and not script.description:
            script.description = value
        elif key == "run-at":
            script.run_at = value or "document-idle"
        elif key == "match":
            script.matches.append(value)
        elif key == "include":
            script.includes.append(value)
        elif key in ("exclude", "exclude-match"):
            script.excludes.append(value)
        elif key == "grant":
            script.grants.append(value)
        elif key == "require":
            script.requires.append(value)
        elif key == "resource":
            script.resources.append(value)

    _add_warnings(script)
    return script


def _add_warnings(script: Userscript) -> None:
    unsupported = [g for g in script.grants if g not in SUPPORTED_GRANTS]
    if unsupported:
        script.warnings.append(
            f"This script asks for {', '.join(sorted(set(unsupported))[:6])}, which the "
            "mint does not provide. Anything relying on those will not work — most "
            "interstitial scripts do not need them."
        )
    if script.requires:
        script.warnings.append(
            f"{len(script.requires)} @require library/libraries are not fetched. If the "
            "script depends on one, it will fail on its first call into it."
        )
    if script.resources:
        script.warnings.append(
            f"{len(script.resources)} @resource entry/entries are not fetched; "
            "GM_getResourceText and GM_getResourceURL are unavailable."
        )
    if "GM_xmlhttpRequest" in script.grants or "GM.xmlHttpRequest" in script.grants:
        script.warnings.append(
            "GM_xmlhttpRequest is provided over fetch, so unlike Tampermonkey it obeys "
            "the browser's cross-origin rules. A cross-origin call that works in your "
            "browser may be refused here."
        )
    if not script.matches and not script.includes:
        script.warnings.append(
            "No @match or @include, so there is nothing to check the site against. "
            "In Tampermonkey this script would run everywhere."
        )


# ── match patterns ───────────────────────────────────────────────────────


def matches_url(script: Userscript, url: str) -> tuple[bool, str]:
    """Whether the script would have run on `url`, and why not if it would not.

    Checked before spending a browser launch on it: if the patterns do not
    cover the verify URL, Tampermonkey would not have run it either, and
    saying that is far more useful than reporting an empty cookie jar.
    """
    if not script.matches and not script.includes:
        return True, ""

    for pattern in script.excludes:
        if _match_one(pattern, url) or _glob(pattern, url):
            return False, f"@exclude {pattern} covers {url}, so the script would be skipped."

    for pattern in script.matches:
        if _match_one(pattern, url):
            return True, ""
    # @include is the older, looser form: a plain glob over the whole URL.
    for pattern in script.includes:
        if _glob(pattern, url):
            return True, ""

    patterns = ", ".join((script.matches + script.includes)[:4])
    return False, (
        f"This script's @match/@include ({patterns}) does not cover {url}. It would not "
        "have run in Tampermonkey either — check the profile's verify URL."
    )


def _match_one(pattern: str, url: str) -> bool:
    """Chrome match-pattern semantics, which is not glob and not regex."""
    if pattern in ("<all_urls>", "*://*/*"):
        return True
    parsed = _split_pattern(pattern)
    if parsed is None:
        return False
    scheme, host, port, path = parsed

    target = urlsplit(url)
    if scheme != "*" and scheme != target.scheme:
        return False
    if scheme == "*" and target.scheme not in ("http", "https"):
        return False

    target_host = (target.hostname or "").lower()
    if host == "*":
        pass
    elif host.startswith("*."):
        base = host[2:]
        if target_host != base and not target_host.endswith(f".{base}"):
            return False
    elif host != target_host:
        return False

    # Chrome's own patterns have no port — the host part is a hostname and
    # nothing else. Tampermonkey accepts one, and people write them, so a
    # stated port is honoured and an unstated one matches anything. Reading
    # `host:port` as a hostname (which is what a naive split does) makes every
    # pattern with a port match nothing at all.
    if port is not None:
        target_port = target.port or (443 if target.scheme == "https" else 80)
        if port != "*" and str(target_port) != port:
            return False

    return _glob(path, target.path or "/")


@cache
def _split_pattern(pattern: str) -> tuple[str, str, str | None, str] | None:
    match = re.match(r"^(\*|https?|file|ftp):(?://)?([^/]*)(/.*)?$", pattern.strip())
    if match is None:
        return None
    authority = (match.group(2) or "").lower()
    port: str | None = None
    # Rightmost colon only, so an IPv6 literal is not chopped in half.
    if ":" in authority and not authority.endswith("]"):
        head, _, tail = authority.rpartition(":")
        if head and (tail.isdigit() or tail == "*"):
            authority, port = head, tail
    return match.group(1), authority, port, match.group(3) or "/*"


def _glob(pattern: str, value: str) -> bool:
    """`*` means any characters. Not fnmatch — `?` and `[]` are literals here."""
    expression = "^" + ".*".join(re.escape(part) for part in pattern.split("*")) + "$"
    return re.match(expression, value) is not None


# ── injection ────────────────────────────────────────────────────────────


@cache
def shim() -> str:
    try:
        return SHIM_FILE.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover — packaging error
        raise UserscriptError(f"the GM_* shim is missing from the install: {exc}") from exc


def init_script(script: Userscript) -> str:
    """What gets handed to `add_init_script`: the shim, then the script.

    One `add_init_script` rather than two, so ordering cannot be a question —
    the shim has to be in place before the first line of the userscript runs,
    and two registrations only guarantee that by convention.

    The userscript is wrapped so a syntax error or an early throw is reported
    rather than silently ending injection for the page.
    """
    return (
        f"{shim()}\n"
        "(function () {\n"
        "  try {\n"
        f"{script.body}\n"
        "  } catch (e) {\n"
        "    console.error('[cairn] userscript threw:', e && e.message ? e.message : e);\n"
        "  }\n"
        "})();\n"
    )
