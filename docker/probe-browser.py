"""Build-time check that Chromium actually launches, sandboxed, as a normal user.

Run from the Dockerfile for the same reason the wget PCRE lookahead is checked
there: a browser that is present but cannot start turns every mint into a
runtime stack trace, and the build is the cheapest place to find out.

Two things are proven here, not assumed:

  - **The sandbox works.** docs/11 requires it stay on, and containers
    routinely deny the unprivileged user namespaces it needs. If that were
    untrue the honest answer would be `--no-sandbox` plus a warning, and the
    build should be where anyone notices.
  - **A non-root user can find and run it.** The container runs as `abc`, not
    root, and Playwright installs browsers under the *building* user's home
    unless `PLAYWRIGHT_BROWSERS_PATH` says otherwise. Checking as root would
    pass while the deployed container failed.

It also pins the launch arguments, which are less obvious than they look. The
image installs Chromium with `--no-shell`, and a plain `chromium.launch()`
then fails: Playwright's default headless mode runs the *headless shell*, a
separate 262 MB build. `channel="chromium"` asks for the real browser in new
headless mode, which is what interactive profiles need anyway — the shell is
a cut-down target and not worth carrying a second browser for.
"""

from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

MARKER = "cairn-browser-probe"


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chromium")
        try:
            page = browser.new_page()
            page.set_content(f"<h1>{MARKER}</h1>")
            if page.text_content("h1") != MARKER:  # pragma: no cover — build-time
                print("FATAL: Chromium started but rendered nothing.", file=sys.stderr)
                return 1
            print(f"chromium sandboxed launch: OK ({browser.version}, uid={os.getuid()})")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
