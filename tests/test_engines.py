"""Engine manifests, config validation, and the NDJSON protocol (docs/05)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from cairn.config import Settings
from cairn.engines import registry
from cairn.engines.protocol import (
    EventWriter,
    JobSpec,
    ResultEvent,
    UrlEvent,
    parse_event,
)
from cairn.engines.wget import build_argv, parse_cdx_line, parse_log_error

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


def test_argv_defaults_to_discarding_the_mirror(tmp_path: Path) -> None:
    argv = build_argv(wget_spec(tmp_path), tmp_path / "out", tmp_path / "tmp", tmp_path / "tmp")
    assert "--delete-after" in argv
    assert "--directory-prefix" not in argv


def test_argv_keeps_the_mirror_when_asked(tmp_path: Path) -> None:
    spec = wget_spec(tmp_path)
    spec.config["keep_mirror"] = True
    argv = build_argv(spec, tmp_path / "out", tmp_path / "tmp", tmp_path / "tmp")
    assert "--delete-after" not in argv
    assert "--directory-prefix" in argv


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
