"""One blog, several hostnames.

Blogger publishes every blog on `<label>.blogspot.com` **and** on a country
domain — `.co.uk`, `.de`, `.com.au`, dozens of them. Until 2018 Google sent
each visitor to their own country's, so posts written in that era contain
links an author typed while looking at the ccTLD version of their own blog.
The redirection is long gone; those links are still in the HTML.

A crawler sees a different hostname and stops, because `--domains` says so.
Nothing is wrong with the capture, the page is archived under the canonical
host, and replay answers a URL key that nobody links to. Measured on a real
archive of 7,654 pages: **every single page** carried at least one alias link,
54 distinct alias URLs across 67,246 occurrences, and 52 of those 54 had their
canonical twin already in the archive. One of the two that did not —
`/p/blog-page.html` — was linked *only* through the alias, so the alias did
not merely break a link, it cost a page.

**No TLD list.** Enumerating Blogger's country domains means a list that is
wrong the moment Google adds one, and every entry is a guess about somebody
else's product. The structure is the evidence instead: two hosts are the same
blog when their label matches and only the part after `blogspot.` differs.

**And a name is never enough on its own.** `is_alias` says two hosts *look*
like one blog; whether they are is settled by asking the site, because a name
that looks like an alias and answers with its own content is a second site and
crawling it as a duplicate would be wrong in the expensive direction. See
`fetch.probe`.
"""

from __future__ import annotations

import re

# `label.blogspot.<anything>` — the suffix is whatever Google is using this
# year, and it is deliberately not checked against a list.
_BLOGSPOT = re.compile(r"^(?P<label>[a-z0-9][a-z0-9-]*)\.blogspot\.(?P<suffix>[a-z.]+)$")

CANONICAL_SUFFIX = "com"


def alias_key(host: str) -> tuple[str, str] | None:
    """`("blogspot", "emilystg")` for any host in a known alias family.

    None when the host belongs to no family this knows about, which is almost
    every host — the families are named platforms, not a general rule.
    """
    match = _BLOGSPOT.match(host.strip().lower())
    if match is None:
        return None
    return ("blogspot", match.group("label"))


def is_alias(candidate: str, canonical: str) -> bool:
    """Whether `candidate` is another address for the same blog as `canonical`.

    False for a host equal to the canonical one: an alias is a *different*
    name, and callers use this to decide whether to add something.
    """
    if candidate.strip().lower() == canonical.strip().lower():
        return False
    left, right = alias_key(candidate), alias_key(canonical)
    return left is not None and left == right


def canonical_form(host: str) -> str | None:
    """The `.com` address of a blog named by any of its aliases."""
    key = alias_key(host)
    if key is None:
        return None
    return f"{key[1]}.blogspot.{CANONICAL_SUFFIX}"


def aliases_among(hosts: list[str], canonical_hosts: set[str]) -> dict[str, str]:
    """Which of `hosts` are aliases of a host already being crawled.

    Returns alias → the crawled host it stands for, so a caller can say *why*
    it is offering to add something rather than presenting a bare hostname
    somebody has to recognise.
    """
    found: dict[str, str] = {}
    for host in hosts:
        for canonical in canonical_hosts:
            if is_alias(host, canonical):
                found[host.strip().lower()] = canonical
                break
    return found
