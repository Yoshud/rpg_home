# RPG HOME

RPG HOME is a strictly local app (one Mac, Apple Silicon) for running tabletop RPG
sessions at home: tiles switch the sound scene with one click (ambient bed, random spot
sounds, overlays, one-shot effects) with smooth transitions.
It is built from a native playback engine, an app server and an MQTT broker, all
started and stopped with one script.

This repo currently contains the skeleton: the layered `rpghome` package, the MQTT broker
(mosquitto in docker compose), an engine placeholder run by launchd, and the start/stop
scripts.

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
  If it does not, it prints the tail of the engine log and exits 1. If the job is
  already running with the same plist, it does nothing; if it is loaded but stopped,
  it is kickstarted; if the plist changed, it is unloaded and loaded again.
- `stop.sh`: `launchctl bootout` (launchd sends SIGTERM, SIGKILL after 10 s), then
  `docker compose down`.
- `status.sh`: broker container state and whether the port accepts connections; the
  engine job (loaded or not, state, pid, last exit code, runs); the engine log path.

The engine log is `$RPGHOME_STATE/logs/engine.log` (default
`~/.local/state/rpghome/logs/engine.log`); broker logs:
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
  `caffeinate -i -w`, and `exec`s `.venv/bin/python -m rpghome.engine` (same PID, so
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
src/rpghome/                the package, in layers (below)
tests/                      pytest, mirrors the package (core/, engine/, cli/);
                            test_setup_sh.py, test_scripts.py (fake docker/launchctl/nc)
```

Package layers (`src/rpghome/`):

```
core/          config.py (paths and settings from the environment), errors.py (RpgHomeError)
models/        domain data: scene, overlay, fx, pool, clip, engine commands and state, roles
repositories/  persistence: catalog (SQLite + catalog.jsonl), scene files, git of the data folder
managers/      application logic: scene loading and checks, catalog import, permissions
               (roles.toml: the default roles config shipped with the package)
bus/           MQTT: Bus interface, MqttBus, InMemoryBus, topics
infra/         wrappers around external tools and services (Epidemic API client, MOSS generation, ffmpeg)
api/           FastAPI: app.py, routers/, ws.py, deps.py, schemas.py (REST DTOs)
engine/        the native audio process (for now a placeholder: logs, waits for SIGTERM)
runner/        scene scripts
mcp/           MCP server, a thin client of the app API
cli/           Typer: main.py (the `rpghome` command)

The package does not know how it is deployed: starting and stopping is done by the
scripts above, not by Python code.
```

Imports between subpackages go only downwards (checked by `tests/test_layers.py`):

| package                               | may import |
|---------------------------------------|------------|
| `core`                                | nothing else from `rpghome` |
| `models`                              | `core` |
| `repositories`                        | `core`, `models` |
| `bus`, `infra`                        | `core`, `models` |
| `managers`                            | `core`, `models`, `repositories`, `bus`, `infra` |
| `api`, `cli`, `mcp`, `runner` (entry points) | all of the above, but not each other |
| `engine`                              | `core`, `models`, `bus` only: it gets scene data and commands over the bus, never reads scene files or the catalog |

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

## Development

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
for f in scripts/*.sh; do bash -n "$f"; done
```

Tests never touch the real data or state folders, Docker or launchd: they set
`RPGHOME_ARTIFACTS` / `RPGHOME_STATE` to temporary directories and put fake `docker`,
`launchctl`, `nc` and `caffeinate` executables first on `PATH`.
