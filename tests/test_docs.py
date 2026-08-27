"""The documentation, checked against the thing it documents.

Prose cannot be tested and does not need to be. What rots silently is the part
that is really a list — the endpoint tables in [09](../docs/09-api.md), the
cross-references between documents — because it is right on the day it is
written and nothing says a word when it stops being.

It had stopped being. An audit found 22 endpoints serving traffic with no row
in the reference, and 14 rows describing endpoints that had not existed for
some time — including four that had *moved*, which is worse than missing,
because a wrong address reads as an answer.

So the list half is asserted here and the prose half is left alone. A new
endpoint now fails a test until it is written down, which is the only
mechanism that has ever kept a reference current.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"
API_DOC = DOCS / "09-api.md"
MARKDOWN = sorted([*DOCS.glob("*.md"), DOCS.parent / "README.md", DOCS.parent / "SECURITY.md"])

# Paths served but deliberately absent from the reference. Each one needs a
# reason, and "we forgot" is not one — that is what this test exists to catch.
UNDOCUMENTED_ON_PURPOSE = {
    # FastAPI's own, and only when dev_mode is on.
    "/api/openapi.json",
    "/api/docs",
}


def normalize(route: str) -> str:
    """One route, however its path parameter happens to be spelled.

    The reference writes `{id}` where the handler writes `{site_id}`, and both
    are the same endpoint. Comparing the spelling rather than the route would
    make this test about naming conventions instead of coverage.
    """
    route = route.split("?")[0].rstrip("/")
    return re.sub(r"\{[^}]*\}", "{}", route)


def served_routes() -> set[tuple[str, str]]:
    """Every API route the app actually serves, from its own OpenAPI schema.

    The schema rather than a regex over the routers: it is what FastAPI will
    really route, so a decorator this test cannot parse cannot hide an
    endpoint from it.
    """
    from cairn.app import create_app

    spec = create_app().openapi()
    found: set[tuple[str, str]] = set()
    for path, operations in spec["paths"].items():
        if not path.startswith("/api") or path in UNDOCUMENTED_ON_PURPOSE:
            continue
        for method in operations:
            if method.upper() in ("HEAD", "OPTIONS"):
                continue
            found.add((method.upper(), normalize(path)))
    return found


def documented_routes() -> set[tuple[str, str]]:
    """Every route named in an endpoint table in docs/09.

    Rows whose path does not start with `/api/` are prose continuations —
    `…/record?…&download=true` restating the row above with different query
    parameters — and are not routes.
    """
    text = API_DOC.read_text(encoding="utf-8")
    found: set[tuple[str, str]] = set()
    for match in re.finditer(r"^\|\s*`(GET|POST|PUT|PATCH|DELETE)`\s*\|\s*`([^`]+)`", text, re.M):
        route = match.group(2)
        if route.startswith("/api/"):
            found.add((match.group(1), normalize(route)))
    return found


def test_every_endpoint_is_in_the_reference() -> None:
    missing = sorted(served_routes() - documented_routes())
    assert not missing, "endpoints with no row in docs/09-api.md:\n" + "\n".join(
        f"  {verb:6} {route}" for verb, route in missing
    )


def test_the_reference_describes_no_endpoint_that_is_gone() -> None:
    """The half that misleads rather than omits.

    A missing row sends somebody to read the code. A row for an endpoint that
    moved sends them to a 404 they will assume is their own mistake.
    """
    phantom = sorted(documented_routes() - served_routes())
    assert not phantom, "docs/09-api.md describes endpoints that do not exist:\n" + "\n".join(
        f"  {verb:6} {route}" for verb, route in phantom
    )


# ── cross-references ─────────────────────────────────────────────────────


def slug(heading: str) -> str:
    """GitHub's anchor for a heading.

    Each space becomes a hyphen and runs are *not* collapsed, which is why a
    heading with an em dash in it anchors to a double hyphen. Getting this
    wrong makes the test report every such link as broken.
    """
    text = re.sub(r"[`*_\[\]()]", "", heading.strip().lower())
    return re.sub(r"[^\w\s-]", "", text).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {
        slug(line.lstrip("#"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    }


def links(path: Path) -> list[str]:
    """Markdown links, ignoring anything inside a code span or fence.

    A regex in a table cell — `` `[?&](order\\|ascending)=` `` — is link syntax
    to a regex and prose to a reader. Stripping code first is what keeps this
    test reporting real problems.
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return re.findall(r"\]\(([^)\s]+)\)", text)


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: p.name)
def test_internal_links_resolve(path: Path) -> None:
    broken: list[str] = []
    for target in links(path):
        if target.startswith(("http://", "https://", "mailto:", "#!")):
            continue
        file_part, _, fragment = target.partition("#")
        destination = (path.parent / file_part).resolve() if file_part else path
        if file_part and not destination.exists():
            broken.append(f"{target}  (no such file)")
            continue
        if fragment and destination.suffix == ".md" and fragment not in anchors(destination):
            broken.append(f"{target}  (no such heading)")
    assert not broken, f"{path.name} links to nothing:\n" + "\n".join(f"  {b}" for b in broken)


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: p.name)
def test_referenced_source_files_exist(path: Path) -> None:
    """Paths named in prose, which move when code is reorganised."""
    root = DOCS.parent
    broken = []
    for match in re.finditer(
        r"`((?:backend|frontend|scripts|tests|docs|examples|unraid|docker)/[\w./-]+)`",
        path.read_text(encoding="utf-8"),
    ):
        reference = match.group(1)
        if not reference.endswith("/") and not (root / reference).exists():
            broken.append(reference)
    assert not broken, f"{path.name} names files that do not exist:\n" + "\n".join(
        f"  {b}" for b in sorted(set(broken))
    )


# ── the other two lists that rot ─────────────────────────────────────────


def test_every_table_is_in_the_schema_document() -> None:
    """docs/03 is the schema, so a table missing from it is a table nobody
    reading that document knows exists. Three were — `annotations`,
    `login_attempts` and `site_health`, all added after it was written."""
    from cairn.db.models import Base

    schema = (DOCS / "03-data-model-and-storage.md").read_text(encoding="utf-8")
    missing = sorted(name for name in Base.metadata.tables if name not in schema)
    assert not missing, "tables with no entry in docs/03:\n" + "\n".join(
        f"  {name}" for name in missing
    )


def test_every_environment_variable_is_documented() -> None:
    """Deployment settings need a restart to change, which makes finding out
    one exists the whole difficulty. Eleven were undocumented."""
    from cairn.config import Settings

    reference = (DOCS / "10-deployment-unraid.md").read_text(encoding="utf-8")
    missing = sorted(
        f"CAIRN_{field.upper()}"
        for field in Settings.model_fields
        if f"CAIRN_{field.upper()}" not in reference
    )
    assert not missing, "settings with no row in docs/10:\n" + "\n".join(
        f"  {name}" for name in missing
    )
