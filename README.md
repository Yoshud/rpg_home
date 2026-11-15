# RPG HOME

RPG HOME is a strictly local app (one Mac, Apple Silicon) for running tabletop RPG
sessions at home: tiles switch the sound scene with one click (ambient bed, random spot
sounds, overlays, one-shot effects) with smooth transitions.
It is built from a native playback engine, an app server and an MQTT broker, all
started and stopped with one script.

This repo currently contains the layered `rpghome` package, the MQTT broker (mosquitto in
docker compose), the start/stop scripts, and the audio engine run by launchd (the
`rpghome_engine` package): its core, the audio path and the ambient bed (see
[Engine](#engine)); it does not listen to the bus yet.

## Setup

Requirements: macOS, [uv](https://docs.astral.sh/uv/), git, and Docker (OrbStack on the Mac).

```sh
scripts/setup.sh            # all steps
scripts/setup.sh data       # or a single step: python | deps | docker | data
```

Each step is safe to re-run:

| step     | what it does                                                                 |
|----------|------------------------------------------------------------------------------|
| `python` | checks uv, `uv python install 3.13`                                          |
| `deps`   | `uv sync`                                                                    |
| `docker` | checks docker + compose (PATH or `~/.orbstack/bin`) and the daemon, `docker compose pull` |
| `data`   | creates the data folder and its subfolders, `git init`, adds missing lines to its `.gitignore`, commits only the files it wrote and only when they changed |

## Start and stop

```sh
scripts/start.sh     # broker + engine; safe to run again
scripts/status.sh    # exit code 0 only when the broker and the engine both run
scripts/stop.sh      # engine, then broker (the broker volume is kept); safe when nothing runs
```

- `start.sh`: `docker compose up -d` (compose project `rpghome`), waits up to 15 s for `127.0.0.1:1883`,
  then loads the engine as a launchd job and checks for ~2 s that it keeps running.
  If it does not, it prints the tail of both engine logs and exits 1. If the job is
  already running with the same plist, it does nothing; if it is loaded but stopped,
  it is kickstarted; if the plist changed, it is unloaded and loaded again.
- `stop.sh`: `launchctl bootout` (launchd sends SIGTERM, SIGKILL after 10 s), then
  `docker compose down`.
- `status.sh`: broker container state and whether the port accepts connections; the
  engine job (loaded or not, state, pid, last exit code, runs); the paths of both engine
  logs; and, if the engine's last start had no audio output, when and why
  (`$RPGHOME_STATE/engine-audio-failure.json`, removed once the output plays again).

The engine log is `$RPGHOME_STATE/logs/engine.log` (default
`~/.local/state/rpghome/logs/engine.log`), written by the engine itself and rotated;
`engine.stdout.log` next to it gets the job's stdout and stderr (tracebacks, stray output).
Broker logs:
`docker compose -f docker-compose.yml logs mosquitto` in the repo.
`docker` is looked up on `PATH`, then in `~/.orbstack/bin`.

### How launchd is used

The engine needs CoreAudio, so it runs natively on the Mac, not in Docker, as a
launchd job (LaunchAgent in the `gui/<uid>` domain, label `local.rpghome.engine`).

- `start.sh` fills `scripts/launchd/engine.plist.in` and writes the result to
  `$RPGHOME_STATE/launchd/<label>.plist`, then `launchctl bootstrap`s it. Nothing is
  written to `~/Library/LaunchAgents`: there is no autostart at login and nothing to
  uninstall. After `stop.sh` (or a reboot) the job is gone.
- `KeepAlive: {SuccessfulExit: false}`: launchd restarts the engine after a crash
  (at most every 5 s), but not after a clean exit.
- `ProcessType: Interactive`, so real-time audio is not throttled.
- launchd runs `scripts/engine.sh`, which sets `PATH`, keeps the Mac awake with
  `caffeinate -i -w`, and `exec`s `.venv/bin/python -m rpghome_engine` (same PID, so
  launchd's SIGTERM reaches the engine). Run `scripts/engine.sh` by hand to debug it.

## The `rpghome` command

`uv run rpghome config` prints the resolved settings (data, state and log folders,
MQTT host and port) and exits 1 if they are invalid. Later tasks add more commands.

## Roles and permissions

Each mode of work is a **role**: a name and its own permissions. A role may include other
roles (`includes`); its effective permissions are its own plus everything the included
roles give, transitively. A device class (`phone`, `mac`, …) has a maximum role, and the UI
switches the current mode only up to it.

Role names live only in `src/rpghome/managers/roles.toml`, which ships with the package:
`load_roles()` reads it (pass a path for another file), and nothing in
`managers/permissions.py` knows a role name, so a new role or device class needs no code
change. The default config:

| role            | its own permissions                                                                                           | includes        |
|-----------------|---------------------------------------------------------------------------------------------------------------|-----------------|
| `sesja`         | `state.read`, `library.read`, `scene.play`, `overlay.play`, `fx.play`                                        | —               |
| `szybka-edycja` | `scene.write`, `overlay.write`, `fx.write`, `music.fetch`, `chat.use`                                        | `sesja`         |
| `studio`        | `library.review`, `library.tag`, `library.fetch`, `library.generate`, `library.use_unreviewed`              | `szybka-edycja` |

with `[devices] phone = "szybka-edycja"`, `mac = "studio"`.

`managers/permissions.py` holds the 15 known permissions (`PERMISSIONS`) and
`REQUIRED_PERMISSION`, the one table saying what each action needs (`cmd.*`, `mode.switch`,
`tool.*`, `library.*`, `chat.send`); the app server and the MCP server will ask it later.
The functions are `effective_permissions`, `includes`, `allowed`, `allowed_action`,
`can_switch` and `check_switch`. A broken config raises `RolesConfigError` (an unknown
permission with the closest suggestion, an unknown included role, a cycle in `includes`
shown as `a -> b -> a`, a device pointing to a missing role, invalid TOML); switching above
the device's maximum role raises `PermissionDenied`.

## Environment variables

| variable            | default                  | meaning |
|---------------------|--------------------------|---------|
| `RPGHOME_ARTIFACTS` | `~/rpg_home_artifacts`   | data folder (scenes, tiles, clips, catalog); a leading `~` is expanded |
| `RPGHOME_STATE`     | `~/.local/state/rpghome` | runtime state: `logs/engine.log`, `launchd/<label>.plist`. Must not be inside the data folder. |
| `RPGHOME_MQTT_HOST` | `127.0.0.1`              | broker host for the engine (and later the app) |
| `RPGHOME_MQTT_PORT` | `1883`                   | broker port, also checked by `start.sh` / `status.sh` |
| `RPGHOME_ENGINE_LABEL` | `local.rpghome.engine` | launchd label of the engine job (use another one for tests and checks) |
| `RPGHOME_ENGINE_CHECK_SECONDS` | `2`         | how long `start.sh` watches the engine after starting it |
| `RPGHOME_BROKER_WAIT_SECONDS` | `15`         | how long `start.sh` waits for the broker port |
| `RPGHOME_AUDIO_DEVICE` | unset                | engine output: unset = the default device, a device name or index, `null` (a real-time fake device with no sound, for tests), or `null-stalled` (one that never pulls a block, to test the watchdog) |
| `RPGHOME_AUDIO_LATENCY` | `0.05`             | engine output latency asked of the stream: seconds (0 < s <= 2), or PortAudio's `low` / `high`. 50 ms leaves room for effects (target: under 100 ms from click to sound); ambience alone tolerates more. The latency the stream got is logged. |
| `RPGHOME_SKIP_PULL` | unset                    | `scripts/setup.sh docker` skips `docker compose pull` when set to `1` |

## Layout

Code repo (`~/rpg_home`):

```
docker-compose.yml          compose project "rpghome": mosquitto on 127.0.0.1:1883
docker/mosquitto/           broker config (anonymous, persistence on a named volume)
scripts/lib.sh              shared by all scripts: settings, docker lookup, launchd helpers
scripts/setup.sh            first-time setup, one function per step
scripts/start.sh, stop.sh, status.sh   start / stop / status of broker and engine
scripts/engine.sh           runs the engine in the foreground (launchd calls it)
scripts/launchd/engine.plist.in        launchd job template, filled by start.sh
examples/cave.json          a scene for the engine (the cave bed), see "Engine"
src/rpghome/                the app package, in layers (below)
src/rpghome_engine/         the engine package: the native audio process (see "Engine")
tests/                      pytest, mirrors the packages (core/, engine/, cli/);
                            test_setup_sh.py, test_scripts.py (fake docker/launchctl/nc)
```

Package layers (`src/rpghome/`):

```
core/          config.py (paths and settings from the environment), errors.py (RpgHomeError),
               logs.py (setup_logging: a rotated log file, 5 MB × 3, for each process)
models/        domain data: scene, overlay, fx, pool, clip, engine commands and state, roles;
               the rule: pydantic v2 models: frozen, extra fields forbidden, JSON through
               model_dump(mode="json") (roles.py is not converted yet)
repositories/  persistence: catalog (SQLite + catalog.jsonl), scene files, git of the data folder
managers/      application logic: scene loading and checks, catalog import, permissions
               (roles.toml: the default roles config shipped with the package)
bus/           MQTT: Bus interface, MqttBus, InMemoryBus, topics
infra/         wrappers around external tools and services (Epidemic API client, MOSS generation, ffmpeg)
api/           FastAPI: app.py, routers/, ws.py, deps.py, schemas.py (REST DTOs)
runner/        scene scripts
mcp/           MCP server, a thin client of the app API
cli/           Typer: main.py (the `rpghome` command)

The package does not know how it is deployed: starting and stopping is done by the
scripts above, not by Python code.
```

Engine package (`src/rpghome_engine/`): core.py (commands, control cycle, status), mixer.py
(audio thread), bed.py, clips.py, limiter.py, devices.py, memory.py, stats.py, __main__.py.

The engine is its own top-level package, not a layer of `rpghome`, because it is a separate
native process that never runs inside the app server. Both are in the same project (one
`pyproject.toml`, one `.venv`, one wheel). The contract between them is the scene data
(`rpghome.models.scene`) and, later, the MQTT topics, so the engine could one day be
replaced by another implementation without touching the app.

Imports between subpackages go only downwards (checked by `tests/test_layers.py`):

| package                               | may import |
|---------------------------------------|------------|
| `core`                                | nothing else from `rpghome` |
| `models`                              | `core` |
| `repositories`                        | `core`, `models` |
| `bus`, `infra`                        | `core`, `models` |
| `managers`                            | `core`, `models`, `repositories`, `bus`, `infra` |
| `api`, `cli`, `mcp`, `runner` (entry points) | all of the above, but not each other |
| `rpghome_engine` (separate package)   | from `rpghome` only `core`, `models`, `bus` (plus itself and third-party libraries): it gets scene data and commands over the bus, never reads scene files or the catalog |

Nothing in `rpghome` imports `rpghome_engine`.

Data folder (`~/rpg_home_artifacts`, a separate local-only git repo, never pushed):

```
scenes/           scene files (Python)              tracked
tiles/            tile images                       tracked
generated/        generated clips (shareable)       audio ignored
epidemic/         Epidemic Sound clips (never share) audio ignored
catalog.sqlite    clip catalog                      ignored
catalog.jsonl     catalog export                    tracked
```

Audio files (wav, flac, mp3, ogg, opus, m4a, aif/aiff/aifc) and `catalog.sqlite` (with its
`-wal`/`-shm` files) are in the data folder's `.gitignore`.

The broker listens only on `127.0.0.1`; containers on the compose network reach it as
`mosquitto:1883`. Browsers never talk to the broker directly.

## Engine

The engine (`rpghome_engine`) is a separate process that only plays audio. It gets a ready
scene (`rpghome.models.scene`: `Scene` → `Bed` → `ClipRef`s, pydantic models; JSON via
`scene_from_data` / `scene_from_json`, which raise `SceneError` naming each bad field, as
in `bed.hold_s: ...`, and `model_dump(mode="json")`) whose clip paths are relative to the data folder and whose levels are
already resolved; it never reads scene files or the catalog.

### Architecture

- **Audio thread** (`mixer.py`): the `sounddevice` callback, 48 kHz stereo, blocks of 512
  (10.7 ms). It only sums buffers that are already prepared, applies the layer, scene and
  master gains (gliding over 50 ms) and the limiter. It takes no lock, does no I/O and
  allocates no block-sized arrays (preallocated buffers, `out=` and in-place numpy); the
  control thread sends it messages on a deque. A message that does not fit is counted
  (`bad_messages`) and dropped: the audio thread never raises on a message.
- **Control thread** (`core.py`): five times per second it applies the queued commands and
  asks each element what it plays in the next 5 s. The bed (`bed.py`) answers with ready
  buffers (clip slices with gains and fades) and their start samples.
- **Bed** (version C of the sound-gen loop demo): one clip of the pool, looped end to start
  with an equal-power crossfade of `loop_crossfade_s`, held for a time drawn from `hold_s`,
  then an equal-power crossfade of `crossfade_s` into another clip, never the same one twice
  in a row. A hold runs from the start of a clip's fade-in to the start of its fade-out,
  so a clip is heard for hold + `crossfade_s`. A pool of one clip has no holds: it loops
  (loop crossfades only), since a crossfade into itself would add correlated material (up
  to +3 dB). Clip order and holds come from a seeded `random.Random`.
- **Master**: master gain, then a lookahead peak limiter (2.5 ms window, 150 ms release)
  with a ceiling of -1 dBFS (the float32 value just below it) that no sample passes; below
  the ceiling it is a plain delay.
- **Output** (`devices.py`): a failed stream start (PortAudio error; seen on the first
  start with the lid closed) is retried 3 times, 0.5 s apart, each time with a new
  stream; then the process exits 1. A watchdog fails the run when the audio clock does
  not move for 2 s, from the start or later: CoreAudio can keep a stream "active" without
  ever calling back (built-in speakers off in clamshell mode, a device that disappears).
  Then it logs one line ("no audio output: is the lid closed or the output device
  missing?"), writes the time and reason to `$RPGHOME_STATE/engine-audio-failure.json`, and
  exits 1; launchd's 5 s throttle paces the retries. The file is removed as soon as a
  later start plays (callbacks arrive).
- **Late callbacks**: CoreAudio does not report a late callback as an underflow through
  PortAudio (a 30-minute run under memory pressure had audible crackles, a 29 ms callback
  and 0 underruns). The audio thread follows the DAC timeline PortAudio gives each
  callback, because wall-clock spacing means nothing: at 50 ms latency PortAudio calls back
  in bursts of 3-4 blocks per host buffer, 32-43 ms apart. `late_callbacks`: the block
  reaches the DAC less than 2 ms after now (headroom `outputBufferDacTime - currentTime`;
  its minimum is `min_headroom_ms`). `gaps`: since the last callback where the DAC time
  changed, it moved more than half a block further than the frames rendered since then,
  so samples were lost (`max_gap_ms` is the largest loss); this works whether PortAudio
  advances the DAC time inside a burst or repeats it. `slow_callbacks` (a callback whose
  own work took longer than a 10.7 ms block) is informational. A report is `degraded` by
  underruns, late callbacks or gaps.
- **Memory pinned in RAM** (`memory.py`, `MemoryPin`): it pins the buffers' pages in RAM
  with `mlock`; it is not mutual exclusion (the audio thread takes no mutex at all). Under
  memory pressure macOS compresses or swaps out pages, and a compressed page read in the
  callback is a page fault, a late callback and a crackle. At start the engine tries
  `mlockall(MCL_CURRENT | MCL_FUTURE)`. macOS declares it but returns ENOSYS, so every clip
  buffer, every planned buffer (what the callback reads) and every mixer and limiter buffer
  is pinned (`mlock`) when allocated and unpinned (`munlock`) when freed (pages are
  reference-counted). The audio thread never calls `mlock` / `munlock` or takes the page
  table's guard: the mixer and limiter are sized up front (larger device blocks are
  rendered in pieces, never by regrowing), and what it lets go of goes to a graveyard deque
  the control thread clears, so buffers are freed and unpinned there. macOS limits this by
  `RLIMIT_MEMLOCK` (unlimited by default) and `sysctl vm.user_wire_limit`. A refused
  `mlock` is one warning, never fatal; the log and the report's `memory` say what was
  pinned and how much. Not covered without `mlockall`: the interpreter's own memory (code,
  objects).
- **Non-finite samples**: a clip with a NaN or inf sample is rejected by `scene()`. The
  limiter also silences any non-finite sample before its detector (`nonfinite_blocks`), so
  its state can never turn into NaN, and its final clamp is NaN-safe.
- **Clips** are read into RAM when a scene is set (`soundfile`), resampled to 48 kHz if
  needed; mono clips play on both channels.

### Command API

Thread-safe methods of `rpghome_engine.core.Engine` (the bus calls them in a later task):

| method | what it does |
|--------|--------------|
| `scene(scene, transition_s=10)` | loads the clips (a missing or unreadable file raises `EngineError`, and the engine keeps playing), then switches with an equal-power crossfade. The old scene stops planning at once: no new clip starts, material it had planned past the switch is dropped, and what plays fades out. |
| `stop(fade_s=2)` | fades everything out; then silence |
| `set(name, value)` | `master_gain_db`, or `bed_gain_db` (the bed of the current scene; the next scene starts at its own bed `gain_db`). With no scene, or a scene without a bed, `bed_gain_db` is ignored with a warning in the log, not kept for later. |
| `status()` | scene, current clip, `hold_s` (`None` for a one-clip pool) and `hold_elapsed_s`, levels, `seconds_rendered`, `late_callbacks`, `gaps`, `max_gap_ms`, `min_headroom_ms`, `slow_callbacks`, `nonfinite_blocks`, output peak and lowest limiter gain over the last ~2 s and since the start, underruns, starved blocks, late buffers, bad messages, planning cycles, control-cycle and callback times (p50, p95 of the whole run from a fixed log-scale histogram, to about 6 %; exact max; in ms). Reading changes nothing and costs the same (about 20 µs) however long the run: any number of readers can poll. |

Offline, `OfflineDevice(engine).render(frames)` or `.render_to_file(path, frames)` pull
blocks the way the callback does and run the control cycle every 0.2 s of rendered time,
so a render depends only on the seed and the commands. The tests use only this.

### The process

```sh
.venv/bin/python -m rpghome_engine                 # how launchd runs it (scripts/engine.sh)
.venv/bin/python -m rpghome_engine --scene scene.json [--seconds N] [--report report.json] \
    [--offline out.wav] [--seed S] [--transition S]
```

- No arguments: opens the output stream, plays silence, waits for commands (none arrive
  until the bus exists) and for SIGTERM / SIGINT, then fades out and exits.
- `--scene`: plays the scene (fading in over `--transition`, default 2 s) until a signal,
  or for `--seconds`. `--offline out.wav` renders `--seconds` into a 24-bit WAV with no
  device. `--report` writes the last `status()` while playing, the measurements
  (including `seconds_rendered` and `callbacks`) and the bed timeline as JSON, with
  `valid: false` and an `invalid_reason` when the output failed or the callback never
  ran, and `degraded: true` with a `degraded_reason` when it played with underruns, late
  callbacks or gaps (what a listener hears as crackles). The minute status line in the
  log shows the same counters. Without `--seed` a random seed is used and logged.
- Exit codes: `0` after SIGTERM / SIGINT or `--seconds`; `0` with a `configuration error`
  log line for invalid settings, bad arguments, or an unreadable scene or clip given on
  the command line, an `RPGHOME_AUDIO_DEVICE` that matches only an input device (a
  microphone), a bad `RPGHOME_AUDIO_LATENCY`, or a state or log folder that
  cannot be written (one line on stderr,
  which is `engine.stdout.log` under launchd); launchd does not restart those. `1` when the
  output fails (no default device; an `RPGHOME_AUDIO_DEVICE` that matches nothing, which
  is either unplugged now or a typo: PortAudio lists only present devices, so they look
  the same, and the engine starts when the device appears, while a typo repeats one clear
  line with the devices present every 5 s; a device that will not start after the
  retries; the watchdog) or
  after an unexpected crash; launchd restarts it every 5 s, for example until the lid is
  opened or a device appears.
- Logs: `$RPGHOME_STATE/logs/engine.log` (rotated by `rpghome.core.logs`, 5 MB × 3 files),
  also on stderr with `--offline` or a TTY. Under launchd, stdout and stderr go to
  `engine.stdout.log`. With `--seconds` or no arguments it logs a status line every minute.

### Device test

`examples/cave.json` is the cave bed: the three cave clips rated 4-5, with gains from the
loop demo's version C (the clips' bed levels, the 20th percentile of momentary loudness,
matched to their median, then one common gain for a -1 dBFS peak). Its paths are relative
to `~/claude_code/tasks/sound-gen/samples`, so use that as the data folder. 30 minutes on
the default output device:

```sh
RPGHOME_ARTIFACTS=~/claude_code/tasks/sound-gen/samples RPGHOME_STATE=/tmp/rpghome-devtest \
  .venv/bin/python -m rpghome_engine --scene examples/cave.json --seconds 1800 \
  --report /tmp/rpghome-devtest/report.json
```

Check `valid` first (a stream that never called back renders nothing, so its zero counters
mean nothing), `seconds_rendered` (about 1800), then `degraded` and the measurements
`underruns` (device underflows), `late_callbacks`, `gaps`, `starved_blocks` and
`late_events` (all should be 0), `min_headroom_ms`, the `memory` summary (what was pinned) and `callback_ms` against
`callback_budget_ms` (10.7 ms). With the lid closed and no external output the run fails
on purpose: exit 1, `valid: false`. The same command with
`--offline /tmp/cave.wav` renders it to a file instead.

### Measurements (offline, Apple Silicon)

The cave scene, seed 3, 600 s rendered offline on a busy machine (load average about 4):
387× real time, control cycle p50 0.027 ms, p95 0.16 ms, max 0.66 ms; callback p50
0.012 ms, p95 0.013 ms, max 0.19 ms (budget 10.7 ms). On an idle machine an earlier
commit measured 640× real time and a callback p50 of 0.007 ms. On the real-time null
device (`RPGHOME_AUDIO_DEVICE=null`, which gives the engine a DAC timeline 20 ms ahead)
for 30 s: control p50 0.065 / p95 0.21 / max 0.51 ms, callback p50 0.043 / p95 0.097 /
max 0.26 ms, minimum headroom 14.7 ms, no late callbacks, gaps or underruns.
Real-device numbers come from the device test above.

## Development

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
for f in scripts/*.sh; do bash -n "$f"; done
```

Tests never touch the real data or state folders, Docker, launchd or the sound card: they set
`RPGHOME_ARTIFACTS` / `RPGHOME_STATE` to temporary directories, put fake `docker`,
`launchctl`, `nc` and `caffeinate` executables first on `PATH`, render the engine offline
from synthetic clips (tones and noise with known levels), and run the engine process with
`RPGHOME_AUDIO_DEVICE=null`.
