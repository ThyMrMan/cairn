"""The addon boundary: manifests, the conformance harness, and mount planning.

Nothing here needs Docker or a browser. The end-to-end proof that a second
engine actually captures what wget cannot lives in `test_browsertrix_e2e.py`.
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cairn.config import Settings
from cairn.engines import conformance
from cairn.engines.registry import Engine, EngineError, discover, load_manifest
from cairn.services import containers
from tests.conftest import XHR

needs_wget = pytest.mark.skipif(shutil.which("wget") is None, reason="needs wget")

MANIFEST = """
apiVersion: cairn.engine/v1
id: {id}
name: "Test engine"
version: "0.1.0"
description: "A fixture."
runtime:
  type: subprocess
  command: ["python3", "engine.py"]
capabilities:
  outputs: [warc]
  javascript: false
config_schema:
  type: object
  additionalProperties: false
  properties:
    depth:
      type: integer
      minimum: 0
      maximum: 5
      default: 1
"""


def _engine_dir(root: Path, engine_id: str, *, manifest: str | None = None) -> Path:
    directory = root / engine_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "engine.yaml").write_text(
        manifest if manifest is not None else MANIFEST.format(id=engine_id), encoding="utf-8"
    )
    (directory / "engine.py").write_text("print('hi')\n", encoding="utf-8")
    return directory


# ── manifests ────────────────────────────────────────────────────────────


def test_a_manifest_loads(tmp_path: Path) -> None:
    engine = load_manifest(_engine_dir(tmp_path, "good"), source="dropin")

    assert engine.id == "good"
    assert engine.defaults() == {"depth": 1}
    assert engine.validate_config({}) == {"depth": 1}


def test_a_config_outside_the_schema_is_refused(tmp_path: Path) -> None:
    """Server-side always. The generated form applies the constraints in the
    browser, and a request that skips the form must not be able to put
    anything it likes into a subprocess argument list."""
    engine = load_manifest(_engine_dir(tmp_path, "good"), source="dropin")

    with pytest.raises(EngineError) as caught:
        engine.validate_config({"depth": 99})
    assert "depth" in str(getattr(caught.value, "problems", ""))

    with pytest.raises(EngineError):
        engine.validate_config({"unknown": True})


def test_an_id_that_does_not_match_its_directory_is_refused(tmp_path: Path) -> None:
    """The directory name is what the operator sees; letting them differ makes
    "which directory is this engine in?" unanswerable from the UI."""
    with pytest.raises(EngineError, match="does not match"):
        load_manifest(
            _engine_dir(tmp_path, "mismatch", manifest=MANIFEST.format(id="something-else")),
            source="dropin",
        )


def test_a_docker_engine_needs_an_image(tmp_path: Path) -> None:
    manifest = MANIFEST.format(id="dock").replace(
        'type: subprocess\n  command: ["python3", "engine.py"]', "type: docker"
    )
    with pytest.raises(EngineError, match="needs an image"):
        load_manifest(_engine_dir(tmp_path, "dock", manifest=manifest), source="dropin")


def test_one_broken_addon_does_not_hide_the_others(tmp_path: Path, settings: Settings) -> None:
    settings.engines_dir.mkdir(parents=True, exist_ok=True)
    _engine_dir(settings.engines_dir, "fine")
    _engine_dir(
        settings.engines_dir, "broken", manifest="apiVersion: cairn.engine/v1\nid: broken\n"
    )

    engines, errors = discover(settings)

    assert "fine" in engines
    assert "broken" in errors
    assert "wget-warc" in engines, "the built-ins are still there"


def test_a_command_naming_a_file_in_the_engine_directory_is_absolutized(tmp_path: Path) -> None:
    """`command: ["python3", "engine.py"]` is the obvious thing to write, and
    could never work otherwise: an engine runs with the *job's* temp directory
    as its working directory, so a relative path resolves there. Found by the
    conformance harness on its first run against the template."""
    directory = _engine_dir(tmp_path, "relative")
    engine = load_manifest(directory, source="dropin")

    assert engine.command == ["python3", str(directory / "engine.py")]


def test_arguments_that_are_not_files_are_left_alone(tmp_path: Path) -> None:
    manifest = MANIFEST.format(id="modular").replace(
        '["python3", "engine.py"]', '["python3", "-m", "somepackage"]'
    )
    engine = load_manifest(_engine_dir(tmp_path, "modular", manifest=manifest), source="dropin")

    assert engine.command == ["python3", "-m", "somepackage"]


# ── the conformance harness ──────────────────────────────────────────────


def _fake_engine(tmp_path: Path, body: str) -> Engine:
    """An engine whose whole behaviour is the lines it prints."""
    directory = tmp_path / "fake"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "engine.yaml").write_text(MANIFEST.format(id="fake"), encoding="utf-8")
    (directory / "engine.py").write_text(textwrap.dedent(body), encoding="utf-8")
    engine = load_manifest(directory, source="dropin")
    # The fixture's interpreter, not whatever `python3` resolves to on PATH.
    engine.manifest["runtime"]["command"] = ["python", "engine.py"]
    engine.source = "builtin"
    return engine


def test_the_harness_passes_a_well_behaved_engine(tmp_path: Path) -> None:
    engine = _fake_engine(
        tmp_path,
        """
        import json, sys
        from pathlib import Path
        spec = json.loads(Path(sys.argv[1]).read_text())
        out = Path(spec["output_dir"]) / "warc"
        out.mkdir(parents=True, exist_ok=True)
        (out / "part.warc.gz").write_bytes(b"not really a warc")
        print(json.dumps({"type": "started"}), flush=True)
        print(json.dumps({"type": "url", "url": spec["seeds"][0], "status": 200}), flush=True)
        print(json.dumps({"type": "artifact", "kind": "warc", "path": "warc/part.warc.gz"}))
        print(json.dumps({"type": "result", "status": "ok", "stats": {}}), flush=True)
        """,
    )

    report = conformance.run(engine, tmp_path / "work", timeout_s=60)

    assert report.ok, [str(c) for c in report.checks if not c.passed]


def test_an_engine_that_never_reports_a_result_fails(tmp_path: Path) -> None:
    """Core treats a missing result as failure whatever the exit code, because
    an engine that stopped without saying how it went is indistinguishable
    from one that crashed."""
    engine = _fake_engine(
        tmp_path,
        """
        import json
        print(json.dumps({"type": "started"}), flush=True)
        """,
    )

    report = conformance.run(engine, tmp_path / "work", timeout_s=60)

    assert not report.ok
    failed = {c.name for c in report.checks if not c.passed}
    assert "emits exactly one result" in failed


def test_an_artifact_escaping_the_output_directory_fails(tmp_path: Path) -> None:
    """Engine output is data, not instruction. A relative path with enough
    `..` in it would otherwise have core checksum — and later serve — a file
    anywhere on disk."""
    engine = _fake_engine(
        tmp_path,
        """
        import json, sys
        from pathlib import Path
        spec = json.loads(Path(sys.argv[1]).read_text())
        out = Path(spec["output_dir"]) / "warc"
        out.mkdir(parents=True, exist_ok=True)
        (out / "part.warc.gz").write_bytes(b"x")
        print(json.dumps({"type": "started"}), flush=True)
        print(json.dumps({"type": "url", "url": "http://x/", "status": 200}), flush=True)
        print(json.dumps({"type": "artifact", "kind": "warc",
                          "path": "../../../../etc/passwd"}), flush=True)
        print(json.dumps({"type": "result", "status": "ok", "stats": {}}), flush=True)
        """,
    )

    report = conformance.run(engine, tmp_path / "work", timeout_s=60)

    assert not report.ok
    assert "artifact paths stay inside output_dir" in {
        c.name for c in report.checks if not c.passed
    }


def test_an_exit_code_disagreeing_with_the_result_fails(tmp_path: Path) -> None:
    engine = _fake_engine(
        tmp_path,
        """
        import json, sys
        from pathlib import Path
        spec = json.loads(Path(sys.argv[1]).read_text())
        out = Path(spec["output_dir"]) / "warc"
        out.mkdir(parents=True, exist_ok=True)
        (out / "part.warc.gz").write_bytes(b"x")
        print(json.dumps({"type": "started"}), flush=True)
        print(json.dumps({"type": "url", "url": "http://x/", "status": 200}), flush=True)
        print(json.dumps({"type": "artifact", "kind": "warc", "path": "warc/part.warc.gz"}))
        print(json.dumps({"type": "result", "status": "ok", "stats": {}}), flush=True)
        sys.exit(1)
        """,
    )

    report = conformance.run(engine, tmp_path / "work", timeout_s=60)

    assert not report.ok
    assert "the exit code agrees with the result" in {c.name for c in report.checks if not c.passed}


def test_stray_output_is_reported_but_not_fatal_to_the_run(tmp_path: Path) -> None:
    """Core counts and skips malformed lines rather than ending a six-hour
    crawl over one of them — but it is still a bug, so the harness says so."""
    engine = _fake_engine(
        tmp_path,
        """
        import json, sys
        from pathlib import Path
        spec = json.loads(Path(sys.argv[1]).read_text())
        out = Path(spec["output_dir"]) / "warc"
        out.mkdir(parents=True, exist_ok=True)
        (out / "part.warc.gz").write_bytes(b"x")
        print("about to start")
        print(json.dumps({"type": "started"}), flush=True)
        print(json.dumps({"type": "url", "url": "http://x/", "status": 200}), flush=True)
        print(json.dumps({"type": "artifact", "kind": "warc", "path": "warc/part.warc.gz"}))
        print(json.dumps({"type": "result", "status": "ok", "stats": {}}), flush=True)
        """,
    )

    report = conformance.run(engine, tmp_path / "work", timeout_s=60)

    assert not report.ok
    assert "stdout is NDJSON" in {c.name for c in report.checks if not c.passed}
    assert report.events.get("result") == 1, "the rest was still read"


@needs_wget
def test_the_shipped_wget_engine_conforms(tmp_path: Path, settings: Settings) -> None:
    """The contract is only real if the engine that defined it also obeys it."""
    engines, _errors = discover(settings)

    report = conformance.run(engines["wget-warc"], tmp_path / "work", timeout_s=300)

    assert report.ok, [str(c) for c in report.checks if not c.passed]


def test_the_shipped_template_conforms(tmp_path: Path, settings: Settings) -> None:
    """The example somebody copies has to pass the test they will run on it."""
    template = Path(__file__).resolve().parents[1] / "examples" / "engine-template"
    engine = load_manifest(template, source="dropin")
    # Run it with the interpreter that has warcio, rather than whatever
    # `python3` happens to be on this machine.
    engine.manifest["runtime"]["command"] = ["python", str(template / "engine.py")]
    engine.source = "builtin"

    report = conformance.run(engine, tmp_path / "work", timeout_s=120)

    assert report.ok, [str(c) for c in report.checks if not c.passed]


# ── mounting a sibling container ─────────────────────────────────────────


def test_a_path_inside_a_named_volume_becomes_a_subpath_mount() -> None:
    """The daemon resolves paths on the host, so our own `/data/...` means
    nothing to it. Asking for a subpath of the volume we were given is what
    keeps `/config` — the database and the master key — out of the engine."""
    ours = [
        {"Type": "volume", "Name": "cairn-data", "Destination": "/data", "Source": "/var/lib/x"},
        {"Type": "bind", "Destination": "/config", "Source": "/host/config"},
    ]

    mount = containers.mount_for(Path("/data/archives/blog/captures/x"), ours, "/cairn/out")

    assert mount["Type"] == "volume"
    assert mount["Source"] == "cairn-data"
    assert mount["VolumeOptions"]["Subpath"] == "archives/blog/captures/x"
    assert mount["Target"] == "/cairn/out"


def test_a_path_inside_a_bind_becomes_a_composed_host_path() -> None:
    ours = [{"Type": "bind", "Destination": "/data", "Source": "/mnt/user/archive"}]

    mount = containers.mount_for(Path("/data/archives/blog"), ours, "/cairn/out")

    assert mount == {
        "Type": "bind",
        "Source": "/mnt/user/archive/archives/blog",
        "Target": "/cairn/out",
        "ReadOnly": False,
    }


def test_the_longest_matching_mount_wins() -> None:
    """A nested mount is the specific one. Choosing the shorter prefix would
    hand the engine a path from the wrong volume — which exists, so nothing
    would report an error."""
    ours = [
        {"Type": "bind", "Destination": "/data", "Source": "/host/data"},
        {"Type": "bind", "Destination": "/data/archives", "Source": "/host/big-array"},
    ]

    mount = containers.mount_for(Path("/data/archives/blog"), ours, "/x")

    assert mount["Source"] == "/host/big-array/blog"


def test_a_path_outside_every_mount_is_refused() -> None:
    """A directory only this process can see is one the daemon cannot mount,
    and saying so beats handing over a path that silently resolves to
    something else on the host."""
    ours = [{"Type": "bind", "Destination": "/data", "Source": "/host/data"}]

    with pytest.raises(containers.ContainerError, match="not inside any mounted volume"):
        containers.mount_for(Path("/somewhere/else"), ours, "/x")


def test_the_job_spec_is_rewritten_into_the_containers_namespace() -> None:
    """An image cannot be written against paths that depend on how somebody
    mounted their array, so it always sees the same two."""
    spec = {
        "output_dir": "/data/archives/blog/captures/x",
        "temp_dir": "/data/tmp/job-7",
        "auth": {"cookies_file": "/data/tmp/job-7/cookies.txt", "user_agent": "x"},
        "incremental": {"dedup_cdx": "/data/tmp/job-7/dedup.cdx"},
    }

    rewritten = containers.rewrite_spec(spec)

    assert rewritten["output_dir"] == containers.CONTAINER_OUT
    assert rewritten["temp_dir"] == containers.CONTAINER_JOB
    assert rewritten["auth"]["cookies_file"] == f"{containers.CONTAINER_JOB}/cookies.txt"
    assert rewritten["incremental"]["dedup_cdx"] == f"{containers.CONTAINER_JOB}/dedup.cdx"
    assert rewritten["auth"]["user_agent"] == "x", "anything that is not a path is untouched"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2g", 2 * 1024**3), ("512m", 512 * 1024**2), ("1024", 1024), ("", 0), ("nonsense", 0)],
)
def test_dockers_size_notation_is_understood(value: str, expected: int) -> None:
    assert containers._bytes(value) == expected


# ── the API's view ───────────────────────────────────────────────────────


def test_engines_report_whether_they_can_actually_run(authed: TestClient) -> None:
    """`enabled` is whether the manifest loaded; `available` is whether the
    environment can run it. A container engine on a host with no Docker socket
    is valid and unusable, and the picker has to be able to say which."""
    engines = {e["id"]: e for e in authed.get("/api/engines", headers=XHR).json()}

    assert engines["wget-warc"]["available"]
    browsertrix = engines["browsertrix"]
    assert browsertrix["enabled"], "it loads regardless of the environment"
    if not browsertrix["available"]:
        assert "docker" in (browsertrix["unavailable_reason"] or "").lower()


def test_the_two_engines_declare_different_auth(authed: TestClient) -> None:
    """The difference the picker warns about: browsertrix cannot use a cookie
    jar, measured rather than assumed — it has no cookie option, and it runs a
    different browser from the one that mints our jars."""
    engines = {e["id"]: e for e in authed.get("/api/engines", headers=XHR).json()}

    assert "cookies" in engines["wget-warc"]["capabilities"]["auth"]
    assert "cookies" not in engines["browsertrix"]["capabilities"]["auth"]
    assert engines["browsertrix"]["capabilities"]["javascript"] is True
    assert engines["wget-warc"]["capabilities"]["javascript"] is False


def test_a_sites_engine_and_config_can_be_changed(authed: TestClient) -> None:
    site = authed.post(
        "/api/sites", json={"seed_url": "https://blog.example/", "title": "Blog"}, headers=XHR
    ).json()

    updated = authed.patch(
        f"/api/sites/{site['id']}",
        json={"engine_id": "browsertrix", "engine_config": {"workers": 2}},
        headers=XHR,
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["engine_id"] == "browsertrix"
    assert updated.json()["engine_config"]["workers"] == 2


def test_a_config_the_engine_would_refuse_is_rejected(authed: TestClient) -> None:
    site = authed.post(
        "/api/sites", json={"seed_url": "https://blog.example/", "title": "Blog"}, headers=XHR
    ).json()

    res = authed.patch(
        f"/api/sites/{site['id']}",
        json={"engine_id": "browsertrix", "engine_config": {"workers": 99}},
        headers=XHR,
    )

    assert res.status_code == 422, res.text
    body = res.json()["error"]
    assert body["code"] == "invalid_config"
    # The offending property is named, because a form that says only
    # "invalid" leaves somebody guessing which of ten fields it meant.
    assert any("workers" in problem for problem in body["detail"])


def test_the_schema_endpoint_is_what_the_form_is_built_from(authed: TestClient) -> None:
    res = authed.get("/api/engines/browsertrix/schema", headers=XHR).json()

    assert res["schema"]["properties"]["workers"]["title"]
    assert res["defaults"]["workers"] == 1
    assert json.dumps(res["schema"]), "it has to survive being sent as JSON"
