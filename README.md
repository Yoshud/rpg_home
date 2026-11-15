# RPG HOME

RPG HOME is a strictly local app (one Mac, Apple Silicon) for running tabletop RPG
sessions at home: tiles switch the sound scene with one click (ambient bed, random spot
sounds, overlays, one-shot effects) with smooth transitions.
It is built from a native playback engine, an app server and an MQTT broker, all
started and stopped with one script.

This repo currently contains the layered `rpghome` package, the MQTT broker (mosquitto in
docker compose), the start/stop scripts, and the audio engine run by launchd (the
`rpghome_engine` package): its core, the audio path, the ambient bed, spot sounds with
distance and the scene reverb (see
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
MQTT host and port) and exits 1 if they are invalid. `uv run rpghome library ...` manages
the clip catalog (see [Library](#library)).

## Library

The clip catalog: every audio clip in the data folder, with its kind, tags, pools, source,
license, loudness and the user's rating.

### Data layout

```
catalog.sqlite                       the catalog (SQLite, WAL); not in git
catalog.jsonl                        its export, tracked in the data folder's git
generated/moss/sound-gen/round1/     MOSS clips of the first blind test (<line>__<short|detailed>.wav)
generated/moss/sound-gen/round2/     MOSS clips of the seed/settings grid (<line>__<condition>__s<seed>.wav)
generated/moss/sound-gen/spots/      MOSS cave spots (<drop|drips|pebbles|rockfall>__s<seed>.wav)
epidemic/sound-gen/refs/             Epidemic references of both blind tests (<line>.wav)
epidemic/sound-gen/spots/            Epidemic cave spots (<drop|pebbles|rockfall>_<a|b>.wav)
```

Everything under `epidemic/` is Epidemic Sound material: license `never-share`, never share or
publish it. Generated clips are `shareable`. Paths in the catalog are relative to the data
folder. Code: `models/clip.py`, `models/tags.py`, `repositories/catalog.py`,
`repositories/tables.py`, `repositories/db.py`, `migrations/`, `infra/ffmpeg.py`,
`managers/loudness.py`, `managers/library.py`, `cli/library.py`.

- **Clip ids** come from the source, so a re-import finds the same clip:
  `moss:sound-gen/<round1|round2|spots>/<file name>` and `epidemic:<Epidemic id>`.
- **Kinds:** `bed` (looped by the engine with crossfades, `loopable`), `spot` (random
  sound over a bed), `fx` (one-shot), `music`. `status` is `review`, `accepted` or `rejected`.
  `perspective` (close/medium/far) and `dry` may be unknown (`null`).
- **Tags** are `category:value` or plain free tags, lowercase words joined by `-`. The
  categories (`place`, `source`, `weather`, `mood`, `use`, `batch`) are listed once, in
  `models/tags.py`.
- **Pools** group clips by use (`cave.bed`, `cave.drops`, ...). A clip may be in several pools,
  and a pool may be empty.

### Export

`catalog.jsonl` is rewritten after every change, and only when its bytes change: after the
commit that made the change succeeds (a failed commit leaves no export ahead of the database),
with the file I/O in a worker thread. Two exports with no change in between are identical:
- one JSON object per line: pools first (`"record": "pool"`, by name), then clips
  (`"record": "clip"`, by id);
- each clip is `Clip.model_dump(mode="json")` without `last_used`, keys sorted. The models keep
  floats to 3 digits (loudness and gain are in 0.1 dB steps) and tags and pools sorted;
- there are no timestamps. `last_used` stays in SQLite only, so playing a clip makes no diff.

### Database and migrations

The models (`models/clip.py`) are pydantic and know nothing of the database. The database side
is SQLAlchemy 2.0 with its asyncio extension and aiosqlite:

- `repositories/tables.py`: the ORM tables (`DeclarativeBase`). The repository
  (`repositories/catalog.py`, an async API) maps rows to the pydantic models and back, by
  column name.

  | table | columns |
  |-------|---------|
  | `clips` | `id` (key), `path` (unique), `kind`, `status`, `source`, `license` (indexed), `rating`, `title`, `last_used` (UTC), `perspective`, `dry`, `loopable`, `gain_db`, `duration_s`, `channels`, `sample_rate`, `notes`, `sha256`; JSON: `details` (MOSS or Epidemic fields), `loudness`, `imported`; `search_text` (what a text search reads) |
  | `clip_tags` | `clip_id`, `tag` (indexed) |
  | `pools` | `name` (key), `description` |
  | `clip_pools` | `clip_id`, `pool` (indexed) |

  Deleting a clip deletes its tag and pool rows (foreign keys, `ON DELETE CASCADE`).
- `repositories/db.py`: the engine and session factory for `<data>/catalog.sqlite`
  (`load_settings()`). Every connection gets WAL mode and foreign keys; write batches start
  with `BEGIN IMMEDIATE`, so other writers wait (up to 30 s) instead of failing half-way. Only
  the task that opened a batch may use it: a task started inside one gets a clear error instead
  of sharing its `AsyncSession`.
- `migrations/` and `alembic.ini` (repo root): the Alembic migrations, async template,
  `render_as_batch=True` (SQLite cannot do most `ALTER TABLE`s), `target_metadata` from
  `tables.py`. The database URL comes from `load_settings()`, never from `alembic.ini`.
  Revision `0001` creates the schema. `tests/repositories/test_migrations.py` runs
  `alembic check`, so the tests fail when the migrations and the tables differ.

**The app upgrades on open:** opening a `Catalog` first reads the current revision with a plain
read (a WAL reader never waits for a writer), and runs `upgrade head` on its own event loop only
when the database is behind head (Alembic's connection-sharing recipe: `db.run_upgrade` puts the
connection into `config.attributes["connection"]`, and `env.py` uses it). So `search` and
`pools` are never blocked by another process's write batch. The upgrade takes the write lock
and runs with foreign keys off, because a batch migration drops and recreates tables and a
`DROP TABLE` would cascade into the rows that refer to it; `PRAGMA foreign_key_check` checks
the result. A database at a revision this code does not know (made by a newer rpghome) is
refused; so is a file that is not a SQLite database, or a catalog made by the old sqlite3 code
(its tables, no `alembic_version`) — the error names the file and says to delete it and re-import.

To change the schema, edit `repositories/tables.py`, then:

```sh
uv run alembic revision --autogenerate -m "what changes"
```

Read the new file in `migrations/versions/` before you commit it: autogenerate does not see
renames (it writes a drop and an add) or data changes, and it does not fill a new non-null
column of existing rows. The next `rpghome` command that opens the catalog upgrades it; by
hand: `uv run alembic upgrade head` (both on `RPGHOME_ARTIFACTS`). `uv run alembic check`
says whether the tables and the migrations still match.

### Commands

```sh
uv run rpghome library import-sound-gen [PATH]     # default ~/claude_code/tasks/sound-gen
uv run rpghome library search [TEXT] [--tag T]... [--kind K] [--status S] [--source SRC]
uv run rpghome library pools                       # pool, clips, kinds, accepted clips
```

`search` prints one line per clip: id, kind, status, rating, gain, title.
- Tags combine with AND. `--tag place:cave` matches exactly; a plain `--tag cave` also
  matches any category tag with that value.
- `TEXT` is a case-insensitive substring of the title (or Epidemic title), the MOSS prompt,
  the notes or the tags.

### Loudness and gain

At import every clip is measured with ffmpeg's `ebur128` filter (`infra/ffmpeg.py`, following
sound-gen's `blind.py` and `loop_demo.py`):

| value | what it is | used for |
|-------|------------|----------|
| `bed_lufs` | 20th percentile of all momentary values (400 ms windows) after the first 4 (warm-up, window not yet full), as in `loop_demo.py`; no gating | bed |
| `max_short_term_lufs` | loudest short-term (3 s) window; clips under 3 s are padded with silence, so they read lower | spot, fx |
| `true_peak_dbtp` | true peak, from the ebur128 summary | ceiling |
| `integrated_lufs` | integrated loudness | information (music) |

A clip under 3.5 s is padded with silence for the short-term window only; the padded frames
are not bed values. A level of -120 LUFS or less (ffmpeg's value for no energy) is stored as
`null`.

`gain_db` is the gain that brings the clip to its kind's target. It is capped so the true
peak stays at or below -1 dBTP. The engine applies the gain at playback; **the file itself is
never modified.**

| kind | target | where it comes from |
|------|--------|---------------------|
| bed | -31 LUFS bed level | `loop_demo.py` version C, the user's favourite: beds matched at their median bed level, then lowered 8 dB for drip headroom |
| spot | -42 LUFS max short-term | `spots_demo.py` over that bed: spots at about 10 LU under the bed |
| fx | -22 LUFS max short-term | `blind.py` round 1: one-shots auditioned at -20 LUFS integrated (or lower for the peak) |
| music | -28 LUFS integrated | placeholder, no music test yet |

The targets and their derivation are in one place, `managers/loudness.py`.

### Import from sound-gen

`import-sound-gen` reads `~/claude_code/tasks/sound-gen` and never writes to it.
- **One at a time:** the whole import holds an exclusive `flock` on
  `$RPGHOME_STATE/locks/catalog-import.lock`. A second import waits for the first.
- **Async:** `managers/library.py` is async where it touches the catalog. The blocking work
  (reading sound-gen, hashing, copying, ffmpeg, waiting for the lock) runs in worker threads
  (`asyncio.to_thread`), so a server's event loop is never blocked for long. The `rpghome`
  commands run it with `asyncio.run`.
- **Safe targets:** names from the sound-gen metadata may contain only letters, digits, `_`,
  `-` and `.`, with no `/` and no `..`. Every target is resolved before anything is written,
  and it must stay inside `generated/` (MOSS) or `epidemic/` (Epidemic) of the data folder.
  A bad row (unsafe name, unknown line, missing field, broken JSON) is skipped with a message,
  and the rest is imported.
- **Copies:** files are copied into the layout above, through a uniquely named temp file. Each
  clip records the SHA-256 of its copy (`sha256`). On a re-import:

  | data-folder file | what happens |
  |------------------|--------------|
  | missing | copied, measured |
  | matches the recorded hash, same as the source | nothing |
  | matches the recorded hash, source changed | copied again, measured again, row updated |
  | differs from the recorded hash, identical to the source | adopted: our copy from an interrupted import; measured, new hash recorded |
  | differs from the recorded hash and from the source | **kept**, reported ("changed in the data folder, kept"); its row is not updated |
  | there, but not a file the catalog imported, and not the source | **kept**, reported, not imported |

  Audio is not in git, so a copy that was changed in the data folder could not be recovered:
  it is never overwritten.
  A source file that ffprobe/ffmpeg cannot read is skipped with a message (and a copy made in
  that run is removed), and the rest is imported.
- **Clips:** the 20 round-1 MOSS clips (`results/moss.jsonl`), the 48 grid clips
  (`results/moss_grid.jsonl`), the 8 MOSS spots (`results/spots_moss.jsonl`), the 10
  Epidemic references (`samples/epidemic/index.json`, the `pick` of each line) and the 6
  Epidemic spots (`samples/spots_epidemic/index.jsonl`).
- **Source details:** for MOSS, the prompt, seed, steps, cfg, condition (grid condition, or
  `short`/`detailed` in round 1, `default` for spots) and the negative prompt. For Epidemic,
  the id and title.
- **Kinds and pools** come from the line name (`LINES` in `managers/library.py`):

  | pool | lines |
  |------|-------|
  | `cave.bed` | `cave_ambience` (bed) |
  | `cave.drops` | `drop`, `drips` (spot) |
  | `cave.stones` | `pebbles`, `rockfall` (spot) |
  | `tavern.bed` | `tavern_ambience` (bed) |
  | `storm.rain` | `storm_rain` (bed; these clips carry rain and thunder together) |
  | `swords.clash` | `sword_clash` (fx) |
  | `storm.thunder` | **empty**: sound-gen has no separate thunder clips |

  `forest_fire` is a bed; `shiba_yelp`, `ravens_takeoff`, `laser_beam`, `door_creak` and
  `dragon_roar` are fx. None of them is in a pool.
- **Tags:** keywords in the line name, the MOSS prompt and the Epidemic title map to category
  tags where it is obvious (`KEYWORDS`: `cave`/`underground` -> `place:cave`, `drip`/`water` ->
  `source:water`, `thunder` -> `source:thunder` + `weather:storm`, ...). The spot lines get
  `place:cave` (they were made for the cave demo).
  - Free tags come from the line words (`ambience`, `drop`, `rockfall`), Epidemic's category,
    subcategory and tags, and the round (`blind-round1`, `blind-round2`, `spot-demo`,
    `short-prompt`/`detailed-prompt`).
  - Every clip also gets `batch:sound-gen` and `use:rpg|film` (from `tests/sfx.json`).
- **Perspective / dry:**
  - `close` when the text says "close up" or "close to", and for the MOSS rockfalls, which
    the user heard as far too close although the prompt says "distant". Otherwise unknown.
  - `dry` is true when the prompt says "dry", and for the Epidemic stones (`spots_demo.py`:
    "Epidemic's stones are dry recordings"). It is false when the text mentions echo, reverb
    or a cave. Otherwise unknown.
- **Ratings:**
  - Round 2 (`blind_grid_key.json` + `blind_grid_votes.txt`) gives 1-5 per clip.
  - Round 1 (`blind_key.json` + `blind_votes.txt`) had best/worst picks among the three
    clips of a line. The best becomes **4**, the worst **2**, the middle one gets no rating.
    Round 1 is relative, so 4 and 2 are the clear part: a pick beat, or lost to, two
    alternatives. Where both rounds rated a clip (four Epidemic references), round 2 wins.
  - The spots were never rated. They stay unrated, with the user's spot-demo verdict
    (paraphrased in `NEXT.md`) in their notes.
  - `notes` hold, per round, the clip's blind number, its result and the user's remark on the
    line, verbatim (Polish). The numbers in the remarks can then be resolved.
  - Status: a rating of 3 or more gives `accepted`; a lower rating or none gives `review`.
- **Re-running** changes nothing when the source did not change: no duplicates, no export diff.
  When a clip already exists:
  - what comes from the file and the source is refreshed: path, title, source details,
    license, stream info, loudness, hash;
  - each clip also stores the values the import gave its user-editable fields (`imported`:
    kind, tags, pools, perspective, dry, loopable, rating, notes, status). A field whose
    catalog value still equals its imported value was not edited, and it gets the new
    import's value: a corrected votes file reaches the catalog. A field the user changed
    keeps the user's value. If the user changed the rating, the status is kept too;
  - the gain follows the resulting kind.

  Each row is read again and written inside the import's write transaction, so an edit made
  while files were being measured is merged, not lost. All copying and measuring happens before
  that transaction: the write batch is short and never spans ffmpeg.

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
examples/cave.json          a scene for the engine (the cave: bed, spots, reverb), see "Engine"
migrations/, alembic.ini    Alembic migrations of the clip catalog, see "Library"
src/rpghome/                the app package, in layers (below)
src/rpghome_engine/         the engine package: the native audio process (see "Engine")
tests/                      pytest, mirrors the packages (core/, models/, repositories/, infra/,
                            managers/, engine/, cli/); audio.py: synthetic WAVs, mini sound-gen;
                            test_setup_sh.py, test_scripts.py (fake docker/launchctl/nc)
```

Package layers (`src/rpghome/`):

```
core/          config.py (paths and settings from the environment), errors.py (RpgHomeError),
               logs.py (setup_logging: a rotated log file, 5 MB × 3, for each process)
models/        domain data: scene, overlay, fx, pool, clip, engine commands and state, roles;
               the rule: pydantic v2 models: frozen, extra fields forbidden, changes through
               model_copy(update=...), JSON through model_dump(mode="json") / model_validate
repositories/  persistence: catalog (SQLAlchemy async on SQLite, ORM tables, catalog.jsonl),
               scene files, git of the data folder
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
scene (`rpghome.models.scene`: `Scene` → `Bed`, `Spots` layers and a `Reverb`, each with
`ClipRef`s, pydantic models; JSON via `scene_from_data` / `scene_from_json`, which raise
`SceneError` naming each bad field, as in `bed.hold_s: ...` or `spots.0.interval_s: ...`,
and `model_dump(mode="json")`; older files without spots or reverb still load) whose clip
paths are relative to the data folder and whose levels are
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
  buffers (clip slices with gains and fades) and their start samples; each spot layer
  (`spots.py`) with one ready buffer per event (direct sound plus its reverb tail).
- **Bed** (version C of the sound-gen loop demo): one clip of the pool, looped end to start
  with an equal-power crossfade of `loop_crossfade_s`, held for a time drawn from `hold_s`,
  then an equal-power crossfade of `crossfade_s` into another clip, never the same one twice
  in a row. A hold runs from the start of a clip's fade-in to the start of its fade-out,
  so a clip is heard for hold + `crossfade_s`. A pool of one clip has no holds: it loops
  (loop crossfades only), since a crossfade into itself would add correlated material (up
  to +3 dB). Clip order and holds come from a seeded `random.Random`.
- **Spots** (`rpghome_engine/spots.py`, the sound-gen spots demo): each `Spots` layer
  (`Scene.spots`, a pydantic model in `rpghome.models.scene`) plays random one-shot events
  on its own layer bus (`spots:<name>`).
  - *Model*: `name` (unique in the scene), `clips` (at least one), `interval_s` (`[min,
    max]`, 0 < min <= max), `mean_interval_s` (> 0, default the middle of `interval_s`),
    `gain_db` (`[min, max]`, -90..24 dB, default `[0, 0]`), `pitch_semitones` (0..24,
    default 0), `pan` (0..1, default 0.8), `distance_m` (0..1000 m, default `null`),
    `source_weights` (`moss`, `epidemic`, `other`, each >= 0, default `{}`; some clip must
    weigh more than 0) and `slice` (default false). Each `ClipRef` may carry `source`,
    `perspective` and `dry`, left out of the JSON when unset. Every error names its field,
    as in `spots.0.interval_s: ...`.
  - *Sounds*, prepared once when the scene is set: each clip is read, mixed to mono (a
    spot is a point source) and, with `slice`, cut into single drops as the demo's
    `slice_drops` does (onsets 20 dB over the clip's floor, 0.4 s apart, at most 12,
    0.3-2.5 s each); a clip with no drops is skipped with a warning, a layer with none is
    an error. Without `slice` the clip's silence (40 dB under its loudest 10 ms) is trimmed.
    A spot clip shorter than 10 ms is an error from `scene()`.
  - *Pick*: a source by `source_weights` (a source not listed weighs 1; `{}`: all
    equal), then one of its sounds; never the sound (clip, or slice of a clip) just
    played. Picking the source first keeps the source ratio at the weights whatever the
    number of sounds each one has. A source whose only sound was just played cannot come
    next; its share is credited to its next chance (taken from the sources with no
    credit), so MOSS a, b and Epidemic c at 3:1 still give 3:1 (plain renormalisation
    would give 4:1). When no-repeat makes the weights impossible (one MOSS sound at 3:1)
    the source gets as close as it can: every other pick.
  - *Intervals*: exponential with mean `mean_interval_s` (default: the middle of
    `interval_s`), clamped to `interval_s`, then divided by the scene's density. The
    first event comes after a uniform draw from `interval_s`. The clamped mean is
    `lo + mean (e^-lo/mean - e^-hi/mean)`: 4.05 s for 1.5-12 s / mean 4.
  - *Per event*: gain uniform in `gain_db`, pitch uniform in ±`pitch_semitones`
    (resampling by linear interpolation: higher is shorter), pan uniform in ±`pan`
    (equal power, cos/sin). The draws come from a seeded `random.Random` per layer, in a
    fixed order, so a render depends only on the seed and the commands.
- **Distance** (`rpghome_engine.spots.distance`): a clip sounds from its
  `ClipRef.perspective`: `close`, `medium`, `far` = 1, 5, 20 m, or metres (0..1000); none
  = close. A layer's `distance_m` only moves sounds away: with
  r = max(1, distance_m / clip distance) (a far clip asked for at 1 m stays as it is):
  - direct level: `-20 log10(r)` dB, inverse distance, -6 dB per doubling;
  - low-pass: zero-phase 2nd-order Butterworth magnitude `1 / sqrt(1 + (f/fc)^4)`
    (-12 dB per octave), `fc = 20 kHz / r^0.8`, at least 500 Hz, none at r = 1:
    11.5 kHz at r = 2, 6.6 kHz at 4, 5.5 kHz at 5, 1.8 kHz at 20. The FFT is padded by
    4 / fc (the filter's ringing is under -120 dB after 2 / fc on each side), so the
    zero-phase ringing never wraps around into the buffer;
  - reverb: the wet/direct ratio is the scene's `wet_db + 10 log10(r)`, +3 dB per
    doubling (the direct sound falls 6 dB per doubling, the reverb 3).
- **Reverb** (`rpghome_engine.spots.Room`): the scene's `reverb` (the `Reverb` model:
  `decay_s` 0.1-10 s, default 2.2; `wet_db` -90..24, default -10; `seed` an integer >= 0,
  default 3) is a synthetic stereo impulse response like the demo's `cave_ir`: noise
  decaying 60 dB over `decay_s`, a one-pole low-pass at ~3 kHz, 12 ms pre-delay, each
  channel at unit energy. It is
  generated once when the scene is set. Only spots go through it: the bed carries its own
  acoustics. Each event's buffer is made in the control thread with one FFT: the
  (low-passed) mono sound times `pan gain + wet gain × IR` per channel, so it is the
  clip plus a `decay_s` tail. The wet part is diffuse (both IR channels, at 1/√2 each,
  so its energy is `wet_db` under the direct sound's). No reverb (`null`): spots play dry.
  The reverb spectra (complex64, one per FFT size) and low-pass responses (one per FFT
  size and cutoff) are kept in small LRU caches (3 and 4 entries): 7.8 MB for the cave.
  Event buffers are pinned in RAM (`MemoryPin`, `mlock`) like the bed's planned buffers
  (the audio thread reads them), and freed and unpinned in the control thread; the sounds
  they are made from are read only by the control thread and are not pinned.
  `dry` on a clip is carried from the catalog; every spot goes through the reverb, as in
  the demo.
- **Density**: `Engine.set("density", x)` (`rpghome_engine.core`; 0.1-10, default 1 for each new scene) divides the
  spot intervals of the current scene. Events already planned (up to 5 s ahead) stay; the
  interval after the last planned event and all later ones use the new value, and an
  event is never moved before what is already planned.
- **Scene switch with spots**: spot events planned past the switch are cancelled (the
  mixer drops their buffers); those already started fade out with the scene.
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
| `set(name, value)` | `master_gain_db`, or `bed_gain_db` (the bed of the current scene; the next scene starts at its own bed `gain_db`), or `density` (0.1-10, the current scene's spot rate; the next scene starts at 1). With no scene, or a scene without a bed, `bed_gain_db` is ignored with a warning in the log, not kept for later; so is `density` with no scene. |
| `status()` | scene, current clip, `hold_s` (`None` for a one-clip pool) and `hold_elapsed_s`, levels (with `density`), `spot_layers`, `spot_events` (started so far), `seconds_rendered`, `late_callbacks`, `gaps`, `max_gap_ms`, `min_headroom_ms`, `slow_callbacks`, `nonfinite_blocks`, output peak and lowest limiter gain over the last ~2 s and since the start, underruns, starved blocks, late buffers, bad messages, planning cycles, control-cycle and callback times (p50, p95 of the whole run from a fixed log-scale histogram, to about 6 %; exact max; in ms). Reading changes nothing and costs the same (about 20 µs) however long the run: any number of readers can poll. |

`timeline()` lists the bed clips started so far. `spot_timeline()` is the spot event log
(the latest 20000 events that have started): `scene`, `layer`, `start_s`, `clip` (the path,
`#n` for slice n), `source`, `gain_db`, `pan`, `pitch_semitones`, `clip_distance_m`,
`distance_m`, `attenuation_db`, `lowpass_hz` (`null`: no filter) and `wet_db` (wet level
against the direct sound; `null`: no reverb).

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
  (including `seconds_rendered` and `callbacks`), the bed timeline and the spot event
  log (`spot_timeline`) as JSON, with
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

`examples/cave.json` is the cave: the bed is the three cave clips rated 4-5, with gains from
the loop demo's version C (the clips' bed levels, the 20th percentile of momentary
loudness, matched to their median, then one common gain for a -1 dBFS peak). Two spot
layers use the spots demo's version C settings, mostly MOSS with an occasional Epidemic
sound (weights 3:1): `drops` (sliced MOSS drips and drops and the two Epidemic cave drip
files, every 1.5-12 s, mean 4 s, -10..-2 dB, ±2 semitones) and `stones` (MOSS pebbles and
rockfalls, Epidemic's dry stones, every 15-80 s, mean 35 s, -6..0 dB) at `distance_m` 4.
The demo played the stones as recorded (close, 1 m), and the MOSS rockfall was far too
close: its loud stones were as loud as the bed. At 4 m they are 12 dB quieter, low-passed at
6.6 kHz, with the wet/direct ratio at -4 dB instead of -10, which puts them about 11 dB
under the bed, near the drops' level. Every spot clip's `gain_db` brings its peak (mono) to -8
dBFS, as the demo did (`drips__s1` is left out: it has no drop the slicer finds, and the
demo skipped it too). The reverb is the demo's (2.2 s, -10 dB, seed 3). Its paths are relative
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
`--offline /tmp/cave.wav` renders it to a file instead. The report's `spot_timeline` has
every spot event of the run.

### Measurements (offline, Apple Silicon)

The cave scene, seed 3, 600 s rendered offline on a busy machine (load average about 4):
387× real time, control cycle p50 0.027 ms, p95 0.16 ms, max 0.66 ms; callback p50
0.012 ms, p95 0.013 ms, max 0.19 ms (budget 10.7 ms). On an idle machine an earlier
commit measured 640× real time and a callback p50 of 0.007 ms. On the real-time null
device (`RPGHOME_AUDIO_DEVICE=null`, which gives the engine a DAC timeline 20 ms ahead)
for 30 s: control p50 0.065 / p95 0.21 / max 0.51 ms, callback p50 0.043 / p95 0.097 /
max 0.26 ms, minimum headroom 14.7 ms, no late callbacks, gaps or underruns.
Real-device numbers come from the device test above.

With spots and reverb, after the memory-pinning core (`tests/engine/test_spots.py`,
synthetic cave with both spot layers and the reverb): 20 min of engine time, control cycle
p50 0.022 ms, p95 2.2 ms, max 4.4 ms (5922 cycles; a cycle that prepares an event costs one
FFT convolution, 2.7 ms p50); 2 h, p50 0.019, p95 2.1, max 14 ms. Callback for the same
600 s, seed 3: bed only p50 0.0077 / p95 0.0097 ms, with the two spot layers 0.0109 /
0.0137 ms (two more layer buses, the same per-layer work as the bed's). `examples/cave.json`
with the real clips, 1800 s offline: preparing an event takes p50 3.2 ms, p95 8.1 ms, max
19 ms (the 7.7 s Epidemic stones plus a 2.2 s tail), within the 200 ms control period;
control p50 0.027 ms, p95 2.3 ms, max 19 ms; no starved blocks and no late buffers. These
are wall-clock times on a busy machine: single cycles and callbacks sometimes take
80-125 ms and 10-26 ms in one run and not in another with the same seed and the same work.

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
