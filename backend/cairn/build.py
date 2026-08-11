"""Which build is actually running.

`__version__` cannot answer that. It reads "0.1.0" on every commit, so a
container built from a tree three milestones old reports exactly what a
current one does. That is not hypothetical: a capture was diagnosed against
the wrong code for a full round of testing because both said "0.1.0", and the
only clue that the running image predated the fix was an `&amp;` left
undecoded in a gap report.

The build stamp is the part that changes. It comes from, in order:

  1. `CAIRN_BUILD` / `CAIRN_BUILT_AT` in the environment — what the Docker
     image bakes in, and what CI sets to the commit it built.
  2. `BUILD_INFO` beside the package, written during the image build, so a
     plain `docker build` with no arguments still yields a distinct stamp.
  3. The git checkout, for a development tree run from source.
  4. Nothing, reported as "source" rather than as a lie.

Reading git shells out, so it happens once, lazily, and never in the request
path of a packaged install: `.git` does not exist in the image.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from cairn import __version__

# Written into the image by the Dockerfile, beside the package.
BUILD_INFO_PATH = Path(__file__).resolve().parent / "BUILD_INFO"
GIT_TIMEOUT_S = 3.0

# What a build with no stamp at all reports. Deliberately not a version-like
# string: "source" reads as "whatever is in the working tree right now".
UNKNOWN_BUILD = "source"


@dataclass(frozen=True, slots=True)
class BuildInfo:
    version: str
    build: str
    built_at: str | None = None

    @property
    def label(self) -> str:
        """`0.1.0 (a9873aa)` — what the UI shows."""
        return f"{self.version} ({self.build})"


@cache
def build_info() -> BuildInfo:
    build = (os.environ.get("CAIRN_BUILD") or "").strip()
    built_at = (os.environ.get("CAIRN_BUILT_AT") or "").strip() or None

    if not build or not built_at:
        from_file = _read_build_file()
        build = build or from_file[0]
        built_at = built_at or from_file[1]

    if not build:
        build = _git_describe() or UNKNOWN_BUILD

    return BuildInfo(version=__version__, build=build, built_at=built_at)


def _read_build_file() -> tuple[str, str | None]:
    """`BUILD_INFO` is two lines: the build id, then when it was built."""
    try:
        lines = BUILD_INFO_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "", None
    build = lines[0].strip() if lines else ""
    built_at = lines[1].strip() if len(lines) > 1 else ""
    return build, built_at or None


def _git_describe() -> str | None:
    """The working tree's commit, with `+` when it has uncommitted changes.

    Only attempted when a `.git` really is above the package — otherwise this
    would run git in whatever directory the server happens to have been
    started from and report a completely unrelated repository.
    """
    root = _repo_root()
    if root is None:
        return None
    try:
        # Resolved from PATH on purpose: a developer's git is wherever their
        # toolchain put it, and this path never runs in the image, which has
        # no .git for `_repo_root` to find.
        result = subprocess.run(
            ["git", "describe", "--always", "--dirty=+", "--abbrev=7"],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    described = result.stdout.strip()
    return described if result.returncode == 0 and described else None


def _repo_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    return None
