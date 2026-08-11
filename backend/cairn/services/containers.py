"""Running an engine as a sibling container (docs/05).

The second engine runtime type. An engine image is started by the Docker
daemon we are ourselves running under, reads its job from a mounted directory,
and writes its output into the capture directory — exactly like a subprocess
engine, except that the process is somewhere else.

Four things here were established by probing a real daemon rather than by
reading, and each of them is the difference between working and appearing to
work.

**The daemon resolves paths on the host, not in our namespace.** Bind-mounting
our own `/data/archives/...` into the sibling asks the host for a path that
does not exist there. On this machine our `/data` came from a named volume at
`/var/lib/docker/volumes/…/_data`, and our `/config` from
`/run/desktop/mnt/host/c/Coding/Website Backup` — with a space in it. Nothing
about our own view of the filesystem survives the trip.

**`--volumes-from` solves that, and gives away too much.** It reproduces every
one of our mounts at exactly our paths — verified, including the Windows host
bind — but "every one" includes `/config`, which holds the database and the
master key that decrypts every stored cookie jar, and the Docker socket
itself. So instead we ask the daemon for exactly two directories, by looking
up which of *our* mounts contains them: a named volume gets
`VolumeOptions.Subpath` (Docker API 1.45+), a bind gets its host source
composed with the relative path. Probed both; the sibling sees the job
directory and cannot see `/config` at all.

**The engine's paths are not our paths, so the job spec is rewritten.** The
container always sees its output at `/cairn/out` and its job directory at
`/cairn/job`, and the `job.json` it is handed says so. That is part of the
contract, not an implementation detail — an image cannot be written against
paths that depend on how somebody mounted their array.

**The image must speak the protocol.** A docker engine's stdout is read as
cairn NDJSON, exactly like a subprocess engine's. docs/05 sketched running a
stock third-party image with templated arguments, which cannot work: a stock
image emits its own log format. Wrapping a tool that does not speak the
protocol is an *adapter engine's* job — see `cairn.engines.browsertrix` — and
keeping the two mechanisms separate is what stops this one from growing a
log-format field.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from cairn.logging import get_logger

log = get_logger(__name__)

SOCKET_PATH = "/var/run/docker.sock"
API_BASE = "http://docker"

# Where an engine container always finds its work. Fixed, because an image
# cannot be written against paths that depend on somebody's mount layout.
CONTAINER_OUT = "/cairn/out"
CONTAINER_JOB = "/cairn/job"

# Every container we start carries these, so a crash can be cleaned up after
# without guessing from image names.
LABEL_MANAGED = "cairn.managed"
LABEL_JOB = "cairn.job"

PULL_TIMEOUT_S = 1800.0
STOP_GRACE_S = 60

_CONTAINER_ID = re.compile(r"/(?:docker/)?containers/([0-9a-f]{64})")


class ContainerError(RuntimeError):
    """The daemon is unreachable, or refused something we need."""


@dataclass(slots=True)
class RunSpec:
    """One container run, in cairn's terms rather than Docker's.

    `mounts` is `(our path, the container's path)`. The engine runtime uses the
    two fixed locations above; the browsertrix adapter mounts `/crawls` where
    that tool expects to find it. Everything else about translating those into
    something the daemon can resolve is this module's problem.
    """

    image: str
    argv: list[str] = field(default_factory=list)
    mounts: list[tuple[Path, str]] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # Empty means the image's own. Overriding it broke the first browsertrix
    # run: its Dockerfile sets `WORKDIR /crawls` and it resolves its output
    # tree from the working directory, so pointing that at a directory it had
    # not been given wrote the crawl somewhere nobody was looking — and the
    # run still exited 0, reporting two pages crawled and no archive.
    workdir: str = ""
    job_id: int | None = None
    shm_size: str = "2g"
    memory: str = ""
    # Docker's own default is no network isolation beyond a bridge, which a
    # crawler needs. Named here so it is a decision rather than an omission.
    network: str = ""


def available() -> tuple[bool, str]:
    """Whether sibling containers can be started, and if not, what to say."""
    if not Path(SOCKET_PATH).exists():
        return False, (
            "The Docker socket is not mounted, so container engines cannot run. "
            "Add `-v /var/run/docker.sock:/var/run/docker.sock` to this container "
            "— and read docs/11 first: it grants root-equivalent control of the host."
        )
    return True, ""


def client(*, timeout: float = 60.0) -> httpx.AsyncClient:
    """An async client bound to the Docker socket.

    Over the unix socket with httpx rather than shelling out to `docker`: the
    CLI is another 50 MB in the image, another thing to keep in step with the
    daemon, and a second place for arguments to be quoted wrongly.
    """
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=SOCKET_PATH),
        base_url=API_BASE,
        timeout=timeout,
        trust_env=False,
    )


# ── knowing where we are ─────────────────────────────────────────────────


def self_container_id() -> str | None:
    """Our own container id, or None if we are not in a container.

    `/proc/self/mountinfo` first: the daemon bind-mounts `/etc/hosts` and
    friends out of `/var/lib/docker/containers/<id>/`, so the full id is
    there whatever anyone did to the hostname. `/etc/hostname` is the short id
    by default but is exactly what `--hostname` overrides, which Unraid
    templates routinely do, so it is only a fallback.
    """
    with suppress(OSError):
        match = _CONTAINER_ID.search(Path("/proc/self/mountinfo").read_text())
        if match:
            return match.group(1)
    with suppress(OSError):
        candidate = Path("/etc/hostname").read_text().strip()
        if re.fullmatch(r"[0-9a-f]{12,64}", candidate):
            return candidate
    return None


async def our_mounts(http: httpx.AsyncClient, container_id: str) -> list[dict[str, Any]]:
    response = await http.get(f"/containers/{container_id}/json")
    if response.status_code != 200:
        raise ContainerError(
            f"the daemon does not recognise this container ({response.status_code}); "
            "container engines need the socket of the daemon that started it"
        )
    payload: dict[str, Any] = response.json()
    mounts: list[dict[str, Any]] = payload.get("Mounts") or []
    return mounts


def mount_for(path: Path, mounts: list[dict[str, Any]], target: str) -> dict[str, Any]:
    """A Docker mount giving a sibling exactly `path`, at `target`.

    Finds which of our own mounts contains the path and expresses the
    remainder in terms the daemon can resolve. Raises when nothing contains it,
    which is the honest answer: a path only this process can see is a path the
    daemon cannot mount.
    """
    posix = PurePosixPath(path.as_posix())
    best: dict[str, Any] | None = None
    for mount in mounts:
        destination = str(mount.get("Destination") or "")
        if not destination:
            continue
        with suppress(ValueError):
            posix.relative_to(destination)
            if best is None or len(destination) > len(str(best.get("Destination"))):
                best = mount
    if best is None:
        raise ContainerError(
            f"{path} is not inside any mounted volume, so a container engine cannot reach it. "
            "Mount /data (and /config) into this container rather than keeping them in its "
            "writable layer."
        )

    relative = posix.relative_to(str(best["Destination"])).as_posix()
    if best.get("Type") == "volume" and best.get("Name"):
        spec: dict[str, Any] = {
            "Type": "volume",
            "Source": best["Name"],
            "Target": target,
            "ReadOnly": False,
        }
        if relative and relative != ".":
            spec["VolumeOptions"] = {"Subpath": relative}
        return spec

    source = str(best.get("Source") or "")
    if not source:
        raise ContainerError(f"the daemon reports no source for the mount holding {path}")
    # Composed with a forward slash even when the daemon reports a Windows
    # source (`C:\Users\…`, on Docker Desktop) — tested, and the daemon
    # normalises it. On Linux both halves are POSIX anyway.
    joined = source.rstrip("/\\") + "/" + relative if relative and relative != "." else source
    return {"Type": "bind", "Source": joined, "Target": target, "ReadOnly": False}


async def plan_mounts(http: httpx.AsyncClient, spec: RunSpec) -> list[dict[str, Any]]:
    """The directories the container gets, and nothing else."""
    container_id = self_container_id()
    if container_id is None:
        # Not containerized: our paths *are* host paths, so the daemon can
        # bind them directly. This is the development case.
        return [
            {"Type": "bind", "Source": str(ours), "Target": theirs, "ReadOnly": False}
            for ours, theirs in spec.mounts
        ]
    ours_all = await our_mounts(http, container_id)
    return [mount_for(path, ours_all, target) for path, target in spec.mounts]


# ── images ───────────────────────────────────────────────────────────────


async def image_present(http: httpx.AsyncClient, image: str) -> bool:
    response = await http.get(f"/images/{image}/json")
    return response.status_code == 200


async def pull(http: httpx.AsyncClient, image: str) -> AsyncIterator[str]:
    """Pull an image, yielding human-readable progress.

    Engine images are large — browsertrix is most of a gigabyte — so a pull
    that reports nothing looks exactly like a hang, and the first capture with
    a new engine is precisely when somebody is watching.
    """
    name, _, tag = image.partition(":")
    params = {"fromImage": name, "tag": tag or "latest"}
    async with http.stream(
        "POST", "/images/create", params=params, timeout=PULL_TIMEOUT_S
    ) as response:
        if response.status_code >= 400:
            body = (await response.aread()).decode("utf-8", errors="replace")
            raise ContainerError(f"could not pull {image}: {body[:300]}")
        last = ""
        async for line in response.aiter_lines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:  # pragma: no cover — malformed daemon output
                continue
            if error := event.get("error"):
                raise ContainerError(f"could not pull {image}: {error}")
            status = str(event.get("status") or "")
            # The per-layer byte counts are noise; the phase changes are not.
            if status and status != last and "Downloading" not in status:
                last = status
                yield status


# ── running ──────────────────────────────────────────────────────────────


async def create(http: httpx.AsyncClient, spec: RunSpec) -> str:
    mounts = await plan_mounts(http, spec)
    host_config: dict[str, Any] = {
        "Mounts": mounts,
        "AutoRemove": False,
        # Chromium's default shared-memory use crashes it at Docker's 64 MB.
        "ShmSize": _bytes(spec.shm_size),
        # Never. The socket is not passed on, the container is not privileged,
        # and no capability is added — an engine image is third-party code and
        # the point of running it here is that it is contained.
        "Privileged": False,
        "SecurityOpt": ["no-new-privileges"],
    }
    if spec.memory:
        host_config["Memory"] = _bytes(spec.memory)
    if spec.network:
        host_config["NetworkMode"] = spec.network

    body: dict[str, Any] = {
        "Image": spec.image,
        "Cmd": spec.argv,
        "Env": [f"{k}={v}" for k, v in spec.env.items()],
        "WorkingDir": spec.workdir,
        # Not a TTY: with one, Docker merges stdout and stderr into a single
        # unframed stream, and the protocol needs them apart — NDJSON events
        # on stdout, diagnostics on stderr.
        "Tty": False,
        "Labels": {
            LABEL_MANAGED: "true",
            LABEL_JOB: str(spec.job_id) if spec.job_id is not None else "",
        },
        "HostConfig": host_config,
    }
    response = await http.post("/containers/create", json=body)
    if response.status_code >= 400:
        raise ContainerError(f"could not create a container: {response.text[:400]}")
    container_id: str = response.json()["Id"]
    return container_id


async def start(http: httpx.AsyncClient, container_id: str) -> None:
    response = await http.post(f"/containers/{container_id}/start")
    if response.status_code >= 400:
        raise ContainerError(f"could not start the container: {response.text[:400]}")


async def stream_logs(
    http: httpx.AsyncClient, container_id: str
) -> AsyncIterator[tuple[int, bytes]]:
    """Yield `(stream, chunk)` as the container writes, 1=stdout 2=stderr.

    Docker frames each chunk with an eight-byte header. Frames do not align
    with lines, so the caller reassembles — which is also why the two streams
    have to be kept apart here rather than merged into text.
    """
    params = {"stdout": 1, "stderr": 1, "follow": 1, "timestamps": 0}
    async with http.stream(
        "GET", f"/containers/{container_id}/logs", params=params, timeout=None
    ) as response:
        if response.status_code >= 400:
            raise ContainerError(f"could not read container logs: {response.status_code}")
        buffer = b""
        async for chunk in response.aiter_bytes():
            buffer += chunk
            while len(buffer) >= 8:
                size = int.from_bytes(buffer[4:8], "big")
                if len(buffer) < 8 + size:
                    break
                yield buffer[0], buffer[8 : 8 + size]
                buffer = buffer[8 + size :]


async def logs_text(http: httpx.AsyncClient, container_id: str, *, limit: int = 4_000_000) -> str:
    """Everything the container wrote, once it has finished.

    Separate from `stream_logs` because it is a different question: that one
    follows a running container, this one reads the whole thing back to keep
    beside the capture. Bounded, because a crawler in a retry loop can produce
    megabytes of the same line and this ends up on disk.
    """
    params: dict[str, str] = {"stdout": "1", "stderr": "1", "follow": "0", "tail": "all"}
    response = await http.get(f"/containers/{container_id}/logs", params=params)
    if response.status_code >= 400:  # pragma: no cover — container already gone
        return ""
    raw = response.content[:limit]
    chunks: list[str] = []
    i = 0
    while i + 8 <= len(raw):
        size = int.from_bytes(raw[i + 4 : i + 8], "big")
        chunks.append(raw[i + 8 : i + 8 + size].decode("utf-8", errors="replace"))
        i += 8 + size
    return "".join(chunks) if chunks else raw.decode("utf-8", errors="replace")


async def wait(http: httpx.AsyncClient, container_id: str) -> int:
    response = await http.post(f"/containers/{container_id}/wait", timeout=None)
    if response.status_code >= 400:  # pragma: no cover — container already gone
        return -1
    code: int = int(response.json().get("StatusCode", -1))
    return code


async def stop(http: httpx.AsyncClient, container_id: str) -> None:
    """SIGTERM then SIGKILL, like the subprocess path.

    The engine contract is that it closes and flushes its WARC on SIGTERM, so
    a cancelled capture costs a partial archive rather than a truncated one.
    """
    with suppress(httpx.HTTPError):
        await http.post(
            f"/containers/{container_id}/stop",
            params={"t": STOP_GRACE_S},
            timeout=STOP_GRACE_S + 30,
        )


async def remove(http: httpx.AsyncClient, container_id: str) -> None:
    with suppress(httpx.HTTPError):
        await http.delete(f"/containers/{container_id}", params={"force": 1, "v": 1})


async def sweep(http: httpx.AsyncClient) -> int:
    """Remove containers we started and never cleaned up.

    The container-stopped-mid-capture case: our process dies, the engine
    container keeps running, and nothing on the next boot would otherwise
    notice it. Matched on our label rather than on the image, so an engine
    somebody runs by hand is left alone.
    """
    params = {"all": "true", "filters": json.dumps({"label": [f"{LABEL_MANAGED}=true"]})}
    response = await http.get("/containers/json", params=params)
    if response.status_code >= 400:  # pragma: no cover
        return 0
    removed = 0
    for entry in response.json():
        await stop(http, entry["Id"])
        await remove(http, entry["Id"])
        removed += 1
    if removed:
        log.warning("removed orphaned engine containers", extra={"count": removed})
    return removed


# ── helpers ──────────────────────────────────────────────────────────────

_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _bytes(value: str) -> int:
    """`2g` → 2147483648. Docker's own notation, since manifests use it."""
    text = str(value).strip().lower().rstrip("b")
    if not text:
        return 0
    unit = _UNITS.get(text[-1], 1)
    digits = text[:-1] if text[-1] in _UNITS else text
    try:
        return int(float(digits) * unit)
    except ValueError:
        return 0


def rewrite_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """The job spec as the container will see it.

    Every absolute path in the spec points into our namespace, and the
    container has neither those paths nor any way to guess them. The two
    directories it does have are at fixed locations, so everything under them
    is rewritten to match.
    """
    translated = dict(spec)
    ours_out = spec.get("output_dir") or ""
    ours_job = spec.get("temp_dir") or ""
    translated["output_dir"] = CONTAINER_OUT
    translated["temp_dir"] = CONTAINER_JOB

    def remap(value: str | None) -> str | None:
        if not value:
            return value
        posix = value.replace(os.sep, "/")
        for ours, theirs in ((ours_job, CONTAINER_JOB), (ours_out, CONTAINER_OUT)):
            root = str(ours).replace(os.sep, "/")
            if root and posix.startswith(root):
                return theirs + posix[len(root) :]
        return value

    auth = dict(translated.get("auth") or {})
    if auth.get("cookies_file"):
        auth["cookies_file"] = remap(auth["cookies_file"])
        translated["auth"] = auth

    incremental = dict(translated.get("incremental") or {})
    if incremental.get("dedup_cdx"):
        incremental["dedup_cdx"] = remap(incremental["dedup_cdx"])
        translated["incremental"] = incremental

    return translated
