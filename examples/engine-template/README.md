# Writing a cairn capture engine

A working engine in two files. Copy this directory, rename it, replace the
fetching, keep the protocol.

```bash
cp -r examples/engine-template /config/engines/my-engine
# edit engine.yaml: `id` must match the directory name
cairn engines validate /config/engines/my-engine
cairn engines test     /config/engines/my-engine
```

Then press **Rescan engines** in Settings, or restart. Your engine appears in
the site editor with a form generated from your `config_schema` — you write no
frontend code.

## What cairn does

Spawns the command from your manifest with the path to a `job.json` as the
last argument, reads NDJSON on stdout, and keeps whatever you declare as an
artifact. It never imports your code, so an engine can be written in anything
that reads JSON and prints lines.

## The contract

1. **You are given `job.json`.** Seeds, scope, auth material, your validated
   config, an `output_dir` to write into and a `temp_dir` for scratch. Every
   config property is populated from your schema's defaults, so you can read
   `config["timeout_s"]` without defending against absence.

2. **stdout is NDJSON and nothing else.** One JSON object per line, flushed.
   Diagnostics go to stderr — cairn keeps the tail of it and shows it when a
   capture fails. A stray `print()` is survivable (malformed lines are counted
   and skipped) but it is still a bug, and `cairn engines test` will say so.

3. **Write under `output_dir`, and declare it.** Artifact paths are *relative*
   to `output_dir`. Anything escaping it is refused — engine output is data,
   not instruction.

4. **Finish with exactly one `result`, and let the exit code agree.** No
   `result` is a failure whatever the exit code: an engine that stopped
   without saying how it went cannot be told apart from one that crashed.
   `partial` is a first-class success — an archive with twelve known gaps is
   not a failed capture.

5. **On SIGTERM, stop and flush.** A cancelled capture should cost a partial
   archive, not a truncated one.

## Events

| Event | When | Why it matters |
|---|---|---|
| `started` | once, first | The first line of the live log |
| `log` | freely | `level`: debug, info, warning, error |
| `url` | per fetched URL | The capture's URL list *and* its gap report. An engine that emits none produces a capture the UI cannot describe. Set `error` on failures |
| `progress` | occasionally | `done`, `total`, `bytes` — drives the progress bar |
| `artifact` | per output file | `kind` (`warc`, `cdx`, `log`, …) and a path relative to `output_dir` |
| `warning` | on a known problem | `code` is machine-readable; `interstitial_detected` and `missing_assets` are acted on |
| `result` | once, last | `ok` \| `partial` \| `failed`, plus `stats` |

## Capabilities are a promise

The `capabilities` block is not decoration. The UI hides options an engine
cannot honour and warns when a site needs something the engine does not
declare — so claiming `cookies` you then ignore produces an archive full of
login pages, with nothing anywhere saying why.

The shipped `browsertrix` engine declares `auth: [user_agent]` for exactly
this reason: it genuinely cannot use a cookie jar, and saying so is what makes
the warning appear.

## The other runtime

`runtime.type: docker` runs your **image** beside cairn instead of a
subprocess, reading its stdout the same way. Your image still speaks this
protocol — it is handed a rewritten `job.json` naming the two directories it
can actually see, always at `/cairn/job` and `/cairn/out`.

It needs the Docker socket mounted into cairn, which grants root-equivalent
control of the host; read `docs/11` before enabling it. Your container gets
those two directories and nothing else — not cairn's `/config`, not the socket.

If your tool does not speak the protocol, do not try to make the runtime
translate for it. Write an adapter that runs it and translates, the way
`cairn/engines/browsertrix.py` does.

## Reference

- [docs/05 — Capture engines](../../docs/05-capture-engines.md), the full protocol
- `cairn/engines/protocol.py`, both sides of the boundary in one file
- `cairn/engines/wget.py`, a complete engine
- `cairn/engines/browsertrix.py`, an adapter around a tool that speaks something else
