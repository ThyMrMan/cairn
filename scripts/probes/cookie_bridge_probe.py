"""Can a cookie be read back out of a browsertrix profile, for wget to use?

The two engines are fed differently: wget takes `--load-cookies <netscape
jar>`, browsertrix takes `--profile <tar.gz>`, and until this nothing
converted between them. `docs/00` D4 says every auth mode ends as a jar and
the engine only ever sees `--load-cookies` -- which stopped being true when
the browser profile arrived, because it was the one producer with no jar. So
choosing a browser profile silently also chose the engine.

The blocker is that a profile is a Brave user-data-dir whose `Default/Cookies`
has plaintext `host_key` and `name` but encrypted values. Whether that is a
wall depends entirely on where Chromium got its key:

  - with an OS keyring, from the keyring, and there is none in this container
  - without one, from a **hardcoded** password, and then the file is readable

Which of those browsertrix's Brave actually does decides whether the bridge is
a hundred lines or impossible, and it is not something to recall.

**The arms are the real tool against the real image.** A fixture serves a
known cookie, browsertrix's own `create-login-profile` writes a genuine
tarball, and `profiles.cookies_from_browser_profile` -- the shipped function,
not a copy of it -- reads it back. The check is equality with the known
plaintext: a wrong key decrypts to bytes just the same, so "no exception" is
not a result.

**Measured on `webrecorder/browsertrix-crawler:1.14.1`, 2026-08-16:**

    profile tarball: 41,308,030 bytes
    stored for host 'host.docker.internal', 67 encrypted bytes, prefix b'v10'
    (stripped the domain hash Chromium 130+ prepends)
    recovered == expected

`v10` is the answer: no keyring, hardcoded password. Note the tarball size --
41 MB for one visit, which is why a profile is stored on disk rather than in a
column, and why a jar derived from one is narrowed to the site's own hosts
rather than copied whole.

The fixture is a login page because `--automated` hunts for username and
password fields and waits indefinitely when there are none. That cost one
five-minute timeout to discover and is the sort of thing worth writing down.

Needs Docker. Nothing outside this repo: the fixture is served from here.
"""

from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

IMAGE = os.environ.get("CAIRN_BROWSERTRIX_IMAGE", "webrecorder/browsertrix-crawler:1.14.1")
PORT = int(os.environ.get("CAIRN_PROBE_PORT", "8899"))
COOKIE_NAME = "CAIRN_PROBE"
COOKIE_VALUE = "the-known-plaintext-9f3a2b"
HOST_FROM_CONTAINER = "host.docker.internal"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Set-Cookie", f"{COOKIE_NAME}={COOKIE_VALUE}; Path=/; Max-Age=86400")
        # A login page on purpose: automated mode looks for username and
        # password fields and waits forever if the page has none.
        body = (
            b"<html><body><h1>fixture</h1><form method=post action=/in>"
            b'<input type="text" name="username">'
            b'<input type="password" name="password">'
            b"<button type=submit>Sign in</button></form></body></html>"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def make_profile(work: Path) -> Path | None:
    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{work}:/crawls",
            "--add-host", f"{HOST_FROM_CONTAINER}:host-gateway",
            "--entrypoint", "create-login-profile",
            IMAGE,
            "--url", f"http://{HOST_FROM_CONTAINER}:{PORT}/",
            "--automated", "--headless",
            "--user", "probe", "--password", "probe",
            "--postLoadDelay", "2",
            "--filename", "/crawls/profile.tar.gz",
        ],
        capture_output=True, text=True, timeout=600,
    )  # fmt: skip
    tarball = work / "profile.tar.gz"
    if not tarball.is_file():
        print("  create-login-profile wrote no tarball")
        print("  stderr:", (result.stderr or "")[-600:])
        return None
    print(f"  profile tarball: {tarball.stat().st_size:,} bytes")
    return tarball


def main() -> int:
    from cairn.services import profiles

    work = Path(tempfile.mkdtemp(prefix="cairn-cookie-bridge-"))
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        print(f"fixture on :{PORT}, setting {COOKIE_NAME}")
        tarball = make_profile(work)
        if tarball is None:
            return 1

        raw = tarball.read_bytes()
        report = profiles.describe_browser_profile(raw)
        print(f"  readout: {report['cookies']} cookie(s) over {report['host_count']} host(s)")

        cookies = profiles.cookies_from_browser_profile(raw)
        found = next((c for c in cookies if c.name == COOKIE_NAME), None)
        if found is None:
            print(f"\n  {COOKIE_NAME} did not come back out of {len(cookies)} cookie(s)")
            return 1
        print(f"\n  recovered: {found.value!r}")
        print(f"  expected:  {COOKIE_VALUE!r}")
        if found.value != COOKIE_VALUE:
            print("\nThe value does NOT come back -- the key is not the hardcoded one.")
            return 1

        # And the narrowing, which is what keeps a Google session out of a
        # blog's temp directory.
        narrowed = profiles.cookies_from_browser_profile(raw, ["nothing.example"])
        if any(c.name == COOKIE_NAME for c in narrowed):
            print("\nHost narrowing does not work -- the whole store would be copied.")
            return 1

        print("\nThe value comes back, and narrowing holds. A jar can be built.")
        return 0
    finally:
        server.shutdown()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
