"""Engine manifests, config validation, and the NDJSON protocol (docs/05)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from sqlalchemy.orm import Session

from cairn.config import Settings
from cairn.db.models import Capture, Site
from cairn.db.types import utcnow
from cairn.discovery.platform import BLOGGER_PRESET
from cairn.engines import registry
from cairn.engines.protocol import (
    EventWriter,
    JobSpec,
    ResultEvent,
    UrlEvent,
    parse_event,
)
from cairn.engines.wget import build_argv, parse_cdx_line, parse_log_error
from cairn.services import storage
from cairn.services.scope import CSS_ESCAPE_REJECT

GOOD_MANIFEST = {
    "apiVersion": "cairn.engine/v1",
    "id": "demo",
    "name": "Demo",
    "version": "1.0.0",
    "runtime": {"type": "subprocess", "command": ["python", "-m", "demo"]},
    "capabilities": {"javascript": False},
    "config_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"tries": {"type": "integer", "minimum": 0, "default": 3}},
    },
}


def write_engine(root: Path, manifest: dict[str, object], name: str = "demo") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "engine.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return directory


# ── built-in ─────────────────────────────────────────────────────────────


def test_builtin_wget_engine_loads(settings: Settings) -> None:
    engines, errors = registry.discover(settings)
    assert "wget-warc" in engines
    assert errors == {}
    assert engines["wget-warc"].capabilities["javascript"] is False


def test_builtin_defaults_are_complete(settings: Settings) -> None:
    """The engine reads config["tries"] without defending against absence, so
    every schema property with a default must actually produce one."""
    engine = registry.discover(settings)[0]["wget-warc"]
    defaults = engine.defaults()
    for key in ("wait_s", "rate_limit", "tries", "timeout_s", "warc_max_size", "user_agent"):
        assert key in defaults


# ── manifest validation ──────────────────────────────────────────────────


def test_manifest_id_must_match_its_directory(tmp_path: Path) -> None:
    """Otherwise 'which directory is this engine in?' is unanswerable from
    the UI."""
    directory = write_engine(tmp_path, {**GOOD_MANIFEST, "id": "other"})
    with pytest.raises(registry.EngineError, match="does not match its directory"):
        registry.load_manifest(directory, source="dropin")


def test_unknown_api_version_is_refused(tmp_path: Path) -> None:
    directory = write_engine(tmp_path, {**GOOD_MANIFEST, "apiVersion": "cairn.engine/v99"})
    with pytest.raises(registry.EngineError, match="apiVersion"):
        registry.load_manifest(directory, source="dropin")


@pytest.mark.parametrize("missing", ["id", "name", "version", "runtime"])
def test_missing_required_keys_are_named(tmp_path: Path, missing: str) -> None:
    manifest = {k: v for k, v in GOOD_MANIFEST.items() if k != missing}
    directory = write_engine(tmp_path, manifest)
    with pytest.raises(registry.EngineError, match=missing):
        registry.load_manifest(directory, source="dropin")


def test_subprocess_runtime_needs_a_command(tmp_path: Path) -> None:
    directory = write_engine(tmp_path, {**GOOD_MANIFEST, "runtime": {"type": "subprocess"}})
    with pytest.raises(registry.EngineError, match="command"):
        registry.load_manifest(directory, source="dropin")


def test_invalid_json_schema_is_caught_at_load_not_at_capture(tmp_path: Path) -> None:
    broken = {**GOOD_MANIFEST, "config_schema": {"type": "not-a-type"}}
    directory = write_engine(tmp_path, broken)
    with pytest.raises(registry.EngineError, match="JSON Schema"):
        registry.load_manifest(directory, source="dropin")


def test_malformed_yaml_is_reported_not_raised_through_discover(settings: Settings) -> None:
    """One bad drop-in must not stop every other engine from loading."""
    settings.engines_dir.mkdir(parents=True, exist_ok=True)
    broken = settings.engines_dir / "broken"
    broken.mkdir()
    (broken / "engine.yaml").write_text("this: [is: not: valid", encoding="utf-8")

    engines, errors = registry.discover(settings)
    assert "wget-warc" in engines
    assert "broken" in errors


# ── config validation ────────────────────────────────────────────────────


def test_config_is_merged_over_defaults(settings: Settings) -> None:
    engine = registry.discover(settings)[0]["wget-warc"]
    merged = engine.validate_config({"tries": 7})
    assert merged["tries"] == 7
    assert merged["rate_limit"] == "2m"


def test_out_of_range_config_is_rejected_with_a_field_name(settings: Settings) -> None:
    engine = registry.discover(settings)[0]["wget-warc"]
    with pytest.raises(registry.EngineConfigError) as excinfo:
        engine.validate_config({"tries": 999})
    assert any("tries" in p for p in excinfo.value.problems)


def test_unknown_config_keys_are_rejected(settings: Settings) -> None:
    """additionalProperties: false is what keeps unvalidated values out of a
    subprocess argument list."""
    engine = registry.discover(settings)[0]["wget-warc"]
    with pytest.raises(registry.EngineConfigError):
        engine.validate_config({"rate_limit": "2m", "evil": "$(whoami)"})


def test_rate_limit_pattern_is_enforced(settings: Settings) -> None:
    engine = registry.discover(settings)[0]["wget-warc"]
    with pytest.raises(registry.EngineConfigError):
        engine.validate_config({"rate_limit": "; rm -rf /"})


# ── NDJSON protocol ──────────────────────────────────────────────────────


def test_parses_each_event_type() -> None:
    line = '{"type":"url","url":"https://a/","status":200,"mime":"text/html"}'
    event = parse_event(line)
    assert isinstance(event, UrlEvent)
    assert event.status == 200


@pytest.mark.parametrize(
    "line",
    ["", "   ", "not json at all", "{broken", '{"type":"nonesuch"}', '{"no":"type"}', "[]"],
)
def test_malformed_lines_are_skipped_not_fatal(line: str) -> None:
    """A stray print() in an engine must not kill a six-hour crawl."""
    assert parse_event(line) is None


def test_result_status_is_constrained() -> None:
    assert isinstance(parse_event('{"type":"result","status":"partial"}'), ResultEvent)
    assert parse_event('{"type":"result","status":"invented"}') is None


def test_event_writer_emits_one_flushed_line_per_event() -> None:
    import io

    stream = io.StringIO()
    writer = EventWriter(stream)
    writer.started(tool_version="GNU Wget 1.25.0")
    writer.url("https://a/", status=200)
    writer.result("ok", {"urls": 1})

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [item["type"] for item in lines] == ["started", "url", "result"]
    assert all("ts" in item for item in lines)


def test_job_spec_roundtrips_through_disk(tmp_path: Path) -> None:
    spec = {
        "protocol": "cairn.engine/v1",
        "job_id": 7,
        "site": {"id": 1, "slug": "s", "title": "S"},
        "output_dir": str(tmp_path / "out"),
        "temp_dir": str(tmp_path / "tmp"),
        "seeds": ["https://example.com/"],
        "scope": {"hosts": []},
        "config": {"tries": 2},
    }
    path = tmp_path / "job.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    loaded = JobSpec.load(str(path))
    assert loaded.job_id == 7
    assert loaded.site.slug == "s"
    assert loaded.config["tries"] == 2


# ── wget output parsing ──────────────────────────────────────────────────


def test_parses_a_real_cdx_record() -> None:
    """Space-separated, 11 fields — taken from actual wget 1.25.0 output."""
    line = (
        "http://127.0.0.1:8098/i.html 20260810203702 http://127.0.0.1:8098/i.html "
        "text/html 200 ZHZVNSD7CHQFFQFY74ZTUDD5DD3YBW77 - - 898 /tmp/o.warc.gz "
        "<urn:uuid:98b09daf-a3be-497f-a933-4fbd6d0cab1f>"
    )
    record = parse_cdx_line(line)
    assert record is not None
    assert record["url"] == "http://127.0.0.1:8098/i.html"
    assert record["status"] == 200
    assert record["mime"] == "text/html"
    assert record["digest"] == "ZHZVNSD7CHQFFQFY74ZTUDD5DD3YBW77"
    assert record["redirect"] is None


def test_parses_a_redirect_record() -> None:
    line = (
        "http://h/redir 20260810203702 http://h/redir - 302 "
        "3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ /i.html - 2949 /tmp/o.warc.gz <urn:uuid:x>"
    )
    record = parse_cdx_line(line)
    assert record is not None
    assert record["status"] == 302
    assert record["redirect"] == "/i.html"
    assert record["mime"] is None


def test_cdx_header_and_short_lines_are_ignored() -> None:
    assert parse_cdx_line(" CDX a b a m s k r M V g u") is None
    assert parse_cdx_line("too few fields") is None
    assert parse_cdx_line("") is None


def test_parses_log_errors() -> None:
    assert parse_log_error("2026-08-09 14:25:35 ERROR 404: Not Found.") == (
        "error",
        "404: Not Found.",
    )
    assert parse_log_error('2026-08-09 14:25:35 URL:http://a/ [1/1] -> "x" [1]') is None


# ── argv construction ────────────────────────────────────────────────────


def wget_spec(tmp_path: Path, **overrides: object) -> JobSpec:
    base: dict[str, object] = {
        "protocol": "cairn.engine/v1",
        "job_id": 1,
        "site": {"id": 1, "slug": "blog", "title": "Blog"},
        "output_dir": str(tmp_path / "out"),
        "temp_dir": str(tmp_path / "tmp"),
        "seeds": ["https://example.blogspot.com/"],
        "scope": {
            "seeds": ["https://example.blogspot.com/"],
            "hosts": [
                {"host": "example.blogspot.com", "crawl_pages": True, "fetch_assets": True},
                {"host": "1.bp.blogspot.com", "crawl_pages": False, "fetch_assets": True},
            ],
        },
        "config": {
            "wait_s": 1.0,
            "rate_limit": "2m",
            "tries": 3,
            "timeout_s": 30,
            "warc_max_size": "1G",
            "content_on_error": True,
        },
    }
    base.update(overrides)
    return JobSpec.model_validate(base)


def test_argv_has_the_warc_essentials(tmp_path: Path) -> None:
    argv = build_argv(wget_spec(tmp_path), tmp_path / "out", tmp_path / "tmp", tmp_path / "tmp")
    assert "--warc-cdx" in argv
    assert "--warc-max-size=1G" in argv
    # No extension: wget appends the segment number and .warc.gz itself.
    warc_file = argv[argv.index("--warc-file") + 1]
    assert not warc_file.endswith(".warc.gz")
    # Otherwise wget writes ~/.wget-hsts, and $HOME may be read-only.
    assert "--hsts-file" in argv


def test_never_uses_delete_after(tmp_path: Path) -> None:
    """--delete-after destroys wget's memory of what it has already fetched.

    The mirror on disk is that memory. Deleting each file immediately makes
    every additional seed re-crawl the whole site: measured at 4.8x the records
    for six seeds, against 1.0x when the files are kept. Since discovery hands
    the crawler one seed per post, that multiplies the work by the post count
    with nothing in the log to explain it.
    """
    argv = build_argv(wget_spec(tmp_path), tmp_path / "out", tmp_path / "tmp", tmp_path / "tmp")
    assert "--delete-after" not in argv
    assert "--directory-prefix" in argv


def test_the_discarded_mirror_goes_to_the_temp_directory(tmp_path: Path) -> None:
    """So it is removed with the job rather than left in the archive."""
    argv = build_argv(wget_spec(tmp_path), tmp_path / "out", tmp_path / "tmp", tmp_path / "tmp")
    prefix = argv[argv.index("--directory-prefix") + 1]
    assert str(tmp_path / "tmp") in prefix
    assert str(tmp_path / "out") not in prefix


def test_argv_keeps_the_mirror_in_the_archive_when_asked(tmp_path: Path) -> None:
    spec = wget_spec(tmp_path)
    spec.config["keep_mirror"] = True
    argv = build_argv(spec, tmp_path / "out", tmp_path / "tmp", tmp_path / "tmp")
    prefix = argv[argv.index("--directory-prefix") + 1]
    assert str(tmp_path / "out") in prefix


def test_cookies_always_arrive_with_keep_session_cookies(tmp_path: Path) -> None:
    """Interstitial cookies are frequently session cookies; without this flag
    they are dropped and the bypass fails silently."""
    jar = str(tmp_path / "job-1" / "cookies.txt")
    spec = wget_spec(tmp_path, auth={"cookies_file": jar})
    argv = build_argv(spec, tmp_path / "out", tmp_path / "tmp", tmp_path / "tmp")
    assert "--load-cookies" in argv
    assert "--keep-session-cookies" in argv


def test_scope_translation_is_included(tmp_path: Path) -> None:
    argv = build_argv(wget_spec(tmp_path), tmp_path / "out", tmp_path / "tmp", tmp_path / "tmp")
    assert "--regex-type=pcre" in argv
    assert any(a.startswith("--reject-regex=") for a in argv)


def test_dedup_cdx_is_ignored_when_the_file_is_gone(tmp_path: Path) -> None:
    """A deleted prior capture must not make every later one fail."""
    spec = wget_spec(tmp_path, incremental={"dedup_cdx": str(tmp_path / "missing.cdx")})
    argv = build_argv(spec, tmp_path / "out", tmp_path / "tmp", tmp_path / "tmp")
    assert "--warc-dedup" not in argv


def test_every_argument_is_a_separate_list_item(tmp_path: Path) -> None:
    """shell=False with a list is what keeps user-controlled URLs, hosts and
    regexes as data rather than syntax."""
    spec = wget_spec(tmp_path)
    spec.seeds = ["https://example.com/; rm -rf /"]
    argv = build_argv(spec, tmp_path / "out", tmp_path / "tmp", tmp_path / "tmp")
    assert "https://example.com/; rm -rf /" in argv
    assert all(isinstance(a, str) for a in argv)


# ── browsertrix browser profiles ─────────────────────────────────────────
#
# The bridge M7 concluded did not exist. It does, but only through the
# crawler's own `create-login-profile`, which drives the same browser — so
# what has to be right here is the *container-side* path, since the file the
# supervisor writes is at one path for us and another for the crawler.
#
# Proven against browsertrix 1.14.1 with a real 41 MB tarball: mounted at
# /cairn/auth, passed as --profile, the crawl logged "With Browser Profile"
# and archived a 200. These tests hold the shape that made that work.


def browsertrix_runner(tmp_path: Path, **auth: object):
    from cairn.engines.browsertrix import Runner
    from cairn.engines.protocol import EventWriter

    spec = wget_spec(tmp_path, auth=auth, config={})
    return Runner(spec, EventWriter())


def test_a_browser_profile_is_passed_at_the_path_the_crawler_will_see(tmp_path: Path) -> None:
    """Not the path we wrote it to — the crawler is in another container."""
    from cairn.engines.browsertrix import PROFILE_MOUNT

    tarball = tmp_path / "tmp" / "profile.tar.gz"
    tarball.parent.mkdir(parents=True, exist_ok=True)
    tarball.write_bytes(b"\x1f\x8b" + b"\x00" * 32)

    argv = browsertrix_runner(tmp_path, profile_file=str(tarball))._argv()
    assert "--profile" in argv
    assert argv[argv.index("--profile") + 1] == f"{PROFILE_MOUNT}/profile.tar.gz"
    assert str(tarball) not in argv, "our own path means nothing inside the crawler"


def test_no_profile_flag_when_the_file_is_missing(tmp_path: Path) -> None:
    """A `--profile` pointing at nothing is worse than none at all.

    browsertrix does not fall back — it quits with `Profile setup failed`, or
    on an older path starts clean and archives the login page. Measured: the
    fatal is real, so the flag has to be earned by the file existing.
    """
    runner = browsertrix_runner(tmp_path, profile_file=str(tmp_path / "gone.tar.gz"))
    assert runner._profile_tarball() is None
    assert "--profile" not in runner._argv()


def test_a_cookie_jar_alone_still_warns(tmp_path: Path) -> None:
    """The M7 finding stands where no tarball is attached: this engine has no
    cookie option, so a jar means the gate gets archived."""
    runner = browsertrix_runner(tmp_path, cookies_file=str(tmp_path / "cookies.txt"))
    warnings: list[tuple[str, str]] = []
    runner.events.warning = lambda code, message: warnings.append((code, message))  # type: ignore[method-assign]
    runner._warn_about_auth()
    assert warnings and warnings[0][0] == "auth_unsupported"


def test_a_browser_profile_replaces_that_warning(tmp_path: Path) -> None:
    tarball = tmp_path / "tmp" / "profile.tar.gz"
    tarball.parent.mkdir(parents=True, exist_ok=True)
    tarball.write_bytes(b"\x1f\x8b" + b"\x00" * 32)

    runner = browsertrix_runner(
        tmp_path, cookies_file=str(tmp_path / "cookies.txt"), profile_file=str(tarball)
    )
    warnings: list[tuple[str, str]] = []
    runner.events.warning = lambda code, message: warnings.append((code, message))  # type: ignore[method-assign]
    runner._warn_about_auth()
    assert warnings == []


# ── the Docker socket preflight ──────────────────────────────────────────


def test_a_socket_that_cannot_be_opened_fails_the_preflight(tmp_path, monkeypatch) -> None:
    """Existing is not the same as usable.

    Reported from a real Unraid install: the socket was mounted, the preflight
    passed, and the capture died with `[Errno 13] Permission denied` at the
    bottom of an httpx traceback — from a check whose only job was to say this
    in one line beforehand.
    """
    from cairn.services import containers

    socket = tmp_path / "docker.sock"
    socket.write_bytes(b"")
    monkeypatch.setattr(containers, "SOCKET_PATH", str(socket))
    monkeypatch.setattr(containers.os, "access", lambda *_args, **_kw: False)

    ok, reason = containers.available()
    assert not ok
    assert "cannot open it" in reason
    # It must *not* send anybody to `--group-add`. That is the obvious fix and
    # it does nothing on its own: Docker puts the gid on PID 1, and
    # `s6-setuidgid abc` rebuilds the group list from /etc/group and discards
    # it. Measured in the shipped image — PID 1 has groups 0,281 and the app
    # has 1000. The image joins the group itself in init-perms instead.
    assert "group-add" not in reason
    assert "update it and restart" in reason


def test_a_missing_socket_still_says_it_is_missing(tmp_path, monkeypatch) -> None:
    """The three failures have different fixes and must not be merged."""
    from cairn.services import containers

    monkeypatch.setattr(containers, "SOCKET_PATH", str(tmp_path / "absent.sock"))
    ok, reason = containers.available()
    assert not ok
    assert "not mounted" in reason


def test_a_socket_that_cannot_be_stat_ed_is_not_reported_as_missing(monkeypatch) -> None:
    """`Path.exists()` catches OSError broadly, so a socket this process is not
    allowed to stat came back as "not mounted" — sending somebody to fix a
    mount that was already correct. The reason has to survive to the message.
    """
    from cairn.services import containers

    def denied(_path: str) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(containers.os, "stat", denied)
    ok, reason = containers.available()
    assert not ok
    assert "not mounted" not in reason
    assert "not allowed to look at it" in reason


def test_an_openable_socket_passes(tmp_path, monkeypatch) -> None:
    from cairn.services import containers

    socket = tmp_path / "docker.sock"
    socket.write_bytes(b"")
    monkeypatch.setattr(containers, "SOCKET_PATH", str(socket))
    monkeypatch.setattr(containers.os, "access", lambda *_args, **_kw: True)
    assert containers.available() == (True, "")


def test_browsertrix_obeys_robots_when_the_scope_says_so(tmp_path: Path) -> None:
    """A scope setting that binds one engine and is inert in another is worse
    than no setting.

    wget obeys robots.txt unless told not to; browsertrix ignores it unless
    told to, and this engine never told it. Reported as a 43-post Blogger blog
    that hit its page cap, 115 of the crawled pages being `/search/label/*` —
    which Blogger disallows in robots.txt and the preset deliberately leaves
    to robots rather than rejecting by pattern.
    """
    from cairn.engines.browsertrix import Runner
    from cairn.engines.protocol import EventWriter

    spec = wget_spec(tmp_path, config={})
    assert spec.scope.get("obey_robots") in (None, True)
    argv = Runner(spec, EventWriter())._argv()
    assert "--useRobots" in argv


def test_browsertrix_leaves_robots_alone_when_the_scope_overrides_it(tmp_path: Path) -> None:
    from cairn.engines.browsertrix import Runner
    from cairn.engines.protocol import EventWriter

    scope = {
        "seeds": ["https://example.blogspot.com/"],
        "hosts": [{"host": "example.blogspot.com", "crawl_pages": True, "fetch_assets": True}],
        "obey_robots": False,
    }
    spec = wget_spec(tmp_path, scope=scope, config={})
    assert "--useRobots" not in Runner(spec, EventWriter())._argv()


def test_reject_patterns_reach_the_network_layer_not_just_the_page_queue(tmp_path: Path) -> None:
    """`--exclude` is documented as "regex of **page URLs**" and that is the
    whole problem.

    It filters the crawl queue. A beacon fired by the page's own JavaScript is
    never queued as a page, so no exclude rule can touch it — which is how
    `/b/stats` stayed at 26% of every fetch across three real captures with an
    exclude pattern that matched it perfectly. Measured against browsertrix
    1.14.1 on a fixture whose page fetches a beacon from script:

        --exclude only          4 records, beacon archived
        --exclude + --blockRules 3 records, beacon gone

    It also makes the engines agree. wget's `--reject-regex` has always applied
    to everything it fetches, so "skip URLs matching" meant one thing on one
    engine and something much weaker on the other.
    """
    from cairn.engines.browsertrix import Runner
    from cairn.engines.protocol import EventWriter

    scope = {
        "seeds": ["https://b.blogspot.com/"],
        "hosts": [{"host": "b.blogspot.com", "crawl_pages": True, "fetch_assets": True}],
        "reject_patterns": [r"/b/stats\?", r"[?&]m=1"],
    }
    argv = Runner(wget_spec(tmp_path, scope=scope, config={}), EventWriter())._argv()

    assert "--exclude" in argv
    assert "--blockRules" in argv
    assert argv[argv.index("--exclude") + 1] == argv[argv.index("--blockRules") + 1]
    assert r"/b/stats\?" in argv[argv.index("--blockRules") + 1]


def test_the_block_regex_is_never_empty(tmp_path: Path) -> None:
    """An empty regex would block everything.

    This used to assert the flags were absent when the user had rejected
    nothing, which was true only because the engine read `scope.reject_patterns`
    rather than building the full set. Some rejects are always generated — the
    CSS-escape guard unconditionally, the asset-only fence for every host in
    that role — so "nothing is rejected" is not a state a scope can be in.
    """
    from cairn.engines.browsertrix import Runner
    from cairn.engines.protocol import EventWriter

    scope = {
        "seeds": ["https://b.blogspot.com/"],
        "hosts": [{"host": "b.blogspot.com", "crawl_pages": True, "fetch_assets": True}],
        "reject_patterns": [],
    }
    argv = Runner(wget_spec(tmp_path, scope=scope, config={}), EventWriter())._argv()

    combined = argv[argv.index("--blockRules") + 1]
    assert combined.strip(), "an empty regex matches everything"
    assert CSS_ESCAPE_REJECT in combined


def test_browsertrix_enforces_the_asset_only_fence(tmp_path: Path) -> None:
    """The regression this test exists for, and it was silent for two engines.

    An assets-only host is "fetch this host's images, do not crawl it as a
    website", and the half that says *do not crawl it* is a generated reject
    rather than anything a preset lists. This engine used to read the scope's
    own pattern list, which does not contain it — so on a Blogger blog wget
    rejected every non-asset path on www.blogger.com while browsertrix rejected
    only the four the preset happens to name.

    Measured on a real capture: 254 requests to
    `/$rpc/…onegoogle…getasyncdata`, on a host that was in scope for its images
    alone. Nothing reported it, because a fetch that should not have happened
    looks exactly like one that should.
    """
    from cairn.engines.browsertrix import Runner
    from cairn.engines.protocol import EventWriter

    scope = {
        "seeds": ["https://b.blogspot.com/"],
        "hosts": [
            {"host": "b.blogspot.com", "crawl_pages": True, "fetch_assets": True},
            {"host": "www.blogger.com", "crawl_pages": False, "fetch_assets": True},
        ],
        "reject_patterns": [r"/b/stats\?"],
    }
    argv = Runner(wget_spec(tmp_path, scope=scope, config={}), EventWriter())._argv()
    combined = re.compile(argv[argv.index("--blockRules") + 1])

    # The non-asset path on the assets-only host: rejected.
    assert combined.search("https://www.blogger.com/$rpc/google.internal.onegoogle/getasyncdata")
    assert combined.search("https://www.blogger.com/navbar/123?usegapi=1")
    # Its images: still fetched, or the fence would cost the page its appearance.
    assert not combined.search("https://www.blogger.com/img/logo.png")
    # And the host being crawled is untouched by the fence.
    assert not combined.search("https://b.blogspot.com/2019/05/post.html")


def test_both_engines_enforce_the_same_reject_set(tmp_path: Path) -> None:
    """A crawl has one boundary, whichever engine walks it.

    This is the invariant that was missing. browsertrix built its regex from
    `scope.reject_patterns` and wget from `build_reject_patterns(scope)`, so the
    two engines disagreed about the scope they were both handed — and the
    difference only showed up as a bigger crawl, never as an error. Comparing
    the compiled regex rather than the flag spelling, because the two tools take
    different flags and it is the boundary that has to match, not the CLI.
    """
    from cairn.engines.browsertrix import Runner
    from cairn.engines.protocol import EventWriter

    scope_dict = {
        "seeds": ["https://b.blogspot.com/"],
        "hosts": [
            {"host": "b.blogspot.com", "crawl_pages": True, "fetch_assets": True},
            {"host": "www.blogger.com", "crawl_pages": False, "fetch_assets": True},
            {
                "host": "lh3.googleusercontent.com",
                "crawl_pages": False,
                "fetch_assets": True,
                "allow_extensionless": True,
            },
        ],
        "reject_patterns": [p for p, _note in BLOGGER_PRESET.reject_patterns],
    }
    spec = wget_spec(tmp_path, scope=scope_dict, config={})

    bt = Runner(spec, EventWriter())._argv()
    browsertrix_regex = bt[bt.index("--blockRules") + 1]

    wget = build_argv(spec, tmp_path / "out", tmp_path / "tmp", tmp_path / "tmp")
    wget_regex = next(a.split("=", 1)[1] for a in wget if a.startswith("--reject-regex="))

    assert browsertrix_regex == wget_regex

    # Not just equal strings — equal answers, on the URLs the difference was
    # actually costing.
    compiled = re.compile(browsertrix_regex)
    for url in (
        "https://www.blogger.com/$rpc/google.internal.onegoogle/getasyncdata",
        "https://www.blogger.com/navbar/123?usegapi=1",
        "https://b.blogspot.com/2019/05/post.html?m=1",
        "https://b.blogspot.com/b/stats?style=BLACK",
    ):
        assert compiled.search(url), url
    for url in (
        "https://b.blogspot.com/2019/05/post.html",
        "https://lh3.googleusercontent.com/blogger_img_proxy/AEn0k_abc",
        "https://www.blogger.com/img/logo.png",
    ):
        assert not compiled.search(url), url


def test_browsertrix_progress_says_it_is_counting_pages(tmp_path: Path) -> None:
    """The two engines do not count the same thing under the same label.

    browsertrix's stdout carries no per-URL record — the archived list only
    exists in the CDXJ it writes at the end — so its live counter is pages
    crawled, while wget's is URLs fetched. Both rendered as "URLs", which made
    a capture look 20x slower than the previous one when what had actually
    changed was that a few hundred label pages stopped being crawled.
    """
    import io
    import json as jsonlib

    from cairn.engines.browsertrix import Runner
    from cairn.engines.protocol import EventWriter

    stream = io.StringIO()
    runner = Runner(wget_spec(tmp_path, config={}), EventWriter(stream))
    runner._handle(
        jsonlib.dumps(
            {"context": "crawlStatus", "message": "s", "details": {"crawled": 51, "total": 60}}
        ),
        1,
    )
    events = [jsonlib.loads(line) for line in stream.getvalue().splitlines()]
    progress = next(e for e in events if e["type"] == "progress")
    assert progress["done"] == 51
    assert progress["unit"] == "pages"


def test_wget_warns_when_the_profile_is_a_browser_profile_it_cannot_read(
    tmp_path: Path,
) -> None:
    """The mirror of the browsertrix warning, and it was missing.

    A profile holding only a browsertrix tarball hands wget nothing:
    `--load-cookies` is not passed, the crawl runs signed out, and the archive
    fills with the gate. Reported that way — a second blog set up with a
    browser profile, captured with this engine by mistake, stuck at the
    interstitial with no explanation anywhere.
    """
    from cairn.engines.protocol import EventWriter
    from cairn.engines.wget import Runner

    spec = wget_spec(tmp_path, auth={"profile_file": str(tmp_path / "profile.tar.gz")})
    runner = Runner(spec, EventWriter())
    seen: list[tuple[str, str]] = []
    runner.events.warning = lambda code, msg: seen.append((code, msg))  # type: ignore[method-assign]
    runner._warn_about_auth()

    assert seen and seen[0][0] == "auth_unsupported"
    assert "signed out" in seen[0][1]


def test_wget_is_quiet_when_it_has_the_jar_it_needs(tmp_path: Path) -> None:
    from cairn.engines.protocol import EventWriter
    from cairn.engines.wget import Runner

    spec = wget_spec(tmp_path, auth={"cookies_file": str(tmp_path / "cookies.txt")})
    runner = Runner(spec, EventWriter())
    seen: list[tuple[str, str]] = []
    runner.events.warning = lambda code, msg: seen.append((code, msg))  # type: ignore[method-assign]
    runner._warn_about_auth()
    assert seen == []


# ── pause and resume ─────────────────────────────────────────────────────
#
# The mechanism was measured against the real crawler before any of it was
# written; see scripts/probes/resume_probe.py, which is what established that
# state survives SIGTERM — the signal Cairn actually sends — and
# resume_probe2.py, which established that resuming from it fetches only the
# queued pages. These tests cover the wiring around that, not the crawler.


def _btrix_spec(tmp_path: Path, **overrides: object) -> JobSpec:
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    base: dict[str, object] = {
        "job_id": 9,
        "site": {"id": 1, "slug": "blog", "title": "Blog"},
        "output_dir": str(out),
        "temp_dir": str(tmp_path),
        "seeds": ["https://blog.example/"],
        "scope": {
            "seeds": ["https://blog.example/"],
            "hosts": [{"host": "blog.example", "crawl_pages": True, "fetch_assets": True}],
        },
        "config": {},
    }
    return JobSpec.model_validate({**base, **overrides})


def test_the_newest_crawl_state_is_kept_in_the_capture(tmp_path: Path) -> None:
    """browsertrix saves state into the job's temp tree, which is deleted the
    moment the job ends — so the file it has always written was always thrown
    away. This is the step that keeps it."""
    from cairn.engines.browsertrix import COLLECTION, STATE_DIR_NAME, Runner
    from cairn.engines.protocol import RESUME_STATE_FILE

    runner = Runner(_btrix_spec(tmp_path), EventWriter())
    states = tmp_path / "work" / "collections" / COLLECTION / STATE_DIR_NAME
    states.mkdir(parents=True)
    (states / "20260101000000-aaa-capture.yaml").write_text("state:\n  id: old\n", encoding="utf-8")
    (states / "20260102000000-aaa-capture.yaml").write_text("state:\n  id: new\n", encoding="utf-8")

    assert runner._keep_resume_state(tmp_path / "work") is True

    kept = Path(runner.spec.output_dir) / RESUME_STATE_FILE
    # The newest, under a fixed name — the crawler's own is a timestamp plus a
    # run id, and a resume should not have to go looking.
    assert "id: new" in kept.read_text(encoding="utf-8")


def test_no_state_directory_is_not_an_error(tmp_path: Path) -> None:
    """A crawl that died before writing anything still has to finalize."""
    from cairn.engines.browsertrix import Runner

    runner = Runner(_btrix_spec(tmp_path), EventWriter())
    assert runner._keep_resume_state(tmp_path / "work") is False


def test_a_resume_passes_the_state_and_keeps_every_other_flag(tmp_path: Path) -> None:
    """The crawler's docs are explicit that command-line options are not
    persisted in the state file, so `--config` alone would silently lose the
    scope, the rejects and the profile."""
    from cairn.engines.browsertrix import Runner

    state = tmp_path / "resume-state.yaml"
    state.write_text("state:\n  id: x\n", encoding="utf-8")
    spec = _btrix_spec(
        tmp_path,
        resume={"state_file": str(state)},
        scope={
            "seeds": ["https://blog.example/"],
            "hosts": [{"host": "blog.example", "crawl_pages": True, "fetch_assets": True}],
            "reject_patterns": [r"[?&]m=1"],
        },
    )
    argv = Runner(spec, EventWriter())._argv()

    assert "--config" in argv
    assert argv[argv.index("--config") + 1].endswith("resume-state.yaml")
    # The scope survives alongside it.
    assert "--include" in argv
    assert "--exclude" in argv


def test_a_missing_state_file_starts_fresh_rather_than_pointing_at_nothing(
    tmp_path: Path,
) -> None:
    """`--config` at a path that does not exist does not fail loudly — it
    starts a clean crawl, which would re-archive everything the paused capture
    already holds while looking like a resume."""
    from cairn.engines.browsertrix import Runner

    spec = _btrix_spec(tmp_path, resume={"state_file": str(tmp_path / "gone.yaml")})
    assert "--config" not in Runner(spec, EventWriter())._argv()


def test_the_manifest_says_browsertrix_can_resume_and_wget_cannot(settings: Settings) -> None:
    """The capability is what gates the Pause button, so the two engines
    disagreeing here is the whole point."""
    engines, _errors = registry.discover(settings)
    assert engines["browsertrix"].capabilities["resumable"] is True
    assert engines["wget-warc"].capabilities["resumable"] is False


# ── the dedup CDX, and who gets one ──────────────────────────────────────

CDX_LINE = (
    "http://blog.test/harris.html 20260801090000 http://blog.test/harris.html "
    "text/html 200 QW3RTY - - 1834 0 part-00000.warc.gz"
)


def _site_with_a_captured_cdx(db: Session, settings: Settings) -> Site:
    """A site carrying one prior capture with a real `part.cdx` on disk.

    Enough for `_dedup_cdx` to have something to merge — without which every
    test below passes for the wrong reason.
    """
    site = Site(
        folder_id=1,
        slug="coast",
        title="Coast & Light",
        seed_url="http://blog.test/",
        primary_host="blog.test",
        archive_path="Unfiled/coast",
    )
    db.add(site)
    db.flush()
    storage.ensure_site_dirs(settings, site.archive_path)

    dir_name = "20260801T090000Z-full-wget"
    db.add(
        Capture(
            site_id=site.id,
            kind="full",
            engine_id="wget-warc",
            dir_name=dir_name,
            status="ok",
            started_at=utcnow(),
        )
    )
    db.flush()
    capture_dir = storage.ensure_capture_dirs(settings, site.archive_path, dir_name)
    (capture_dir / storage.WARC_DIR / "part.cdx").write_text(
        f" CDX a b a m s k r M V g u\n{CDX_LINE}\n", encoding="utf-8"
    )
    return site


def _fake_engine(**capabilities: object) -> registry.Engine:
    return registry.Engine(
        id="demo",
        name="Demo",
        version="1.0.0",
        source="dropin",
        path=Path("."),
        manifest={"capabilities": capabilities},
    )


def test_an_engine_that_can_deduplicate_gets_the_merged_cdx(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    from cairn.services.jobs import _dedup_cdx

    site = _site_with_a_captured_cdx(db, settings)
    engine = registry.discover(settings)[0]["wget-warc"]
    assert engine.capabilities["incremental"] is True

    merged = _dedup_cdx(settings, db, site, "feed", tmp_path, engine)
    assert merged is not None
    assert CDX_LINE in Path(merged).read_text(encoding="utf-8")


def test_an_engine_that_declares_it_cannot_is_not_handed_one(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """browsertrix ignores the field, so building the file is pure waste.

    Not merely "the path is None": the assertion that matters is that no file
    was written, because the cost being removed is walking every prior capture
    and merging up to 400,000 CDX lines on every feed capture. A site captured
    only by browsertrix hides this — it writes no `part.cdx`, so the walk finds
    nothing either way. This one has a wget history, which is what a site that
    switched engines looks like.
    """
    from cairn.services.jobs import _dedup_cdx

    site = _site_with_a_captured_cdx(db, settings)
    engine = registry.discover(settings)[0]["browsertrix"]
    assert engine.capabilities["incremental"] is False

    assert _dedup_cdx(settings, db, site, "feed", tmp_path, engine) is None
    assert not (tmp_path / "dedup.cdx").exists()


def test_an_engine_that_never_declared_the_capability_still_gets_one(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    """Absent is not the same as false, matching the engine picker.

    Only an engine that has actively said it cannot use the file is skipped.
    A drop-in that supports dedup but forgot to declare it would otherwise
    lose the saving silently, which is the failure that costs storage with no
    way to notice.
    """
    from cairn.services.jobs import _dedup_cdx

    site = _site_with_a_captured_cdx(db, settings)
    assert _dedup_cdx(settings, db, site, "feed", tmp_path, _fake_engine()) is not None


def test_a_full_capture_is_never_deduplicated_whatever_the_engine_says(
    db: Session, settings: Settings, tmp_path: Path
) -> None:
    from cairn.services.jobs import _dedup_cdx

    site = _site_with_a_captured_cdx(db, settings)
    engine = _fake_engine(incremental=True)
    assert _dedup_cdx(settings, db, site, "full", tmp_path, engine) is None
