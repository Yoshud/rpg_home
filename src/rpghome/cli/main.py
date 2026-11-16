"""`rpghome` command line.

`config` shows the settings, `library ...` manages the clip catalog. `scene`, `stop`, `set`
and `status` talk to the engine over the broker: the CLI never touches the engine process
itself, and a browser does the same through the app server. A command is not sent at all to
an engine that is not online, and each one takes `--timeout` for the wait for its answer.
"""

import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from rpghome.bus import topics
from rpghome.bus.base import Bus
from rpghome.bus.mqtt import MqttBus
from rpghome.cli import library
from rpghome.core import config
from rpghome.models.commands import CommandError, parse_command
from rpghome.models.scene import SceneError, scene_from_data

app = typer.Typer(no_args_is_help=True, add_completion=False, help="RPG HOME.")
app.add_typer(library.app, name="library")

REPLY_TIMEOUT_S = 5.0  # the engine's answer to `stop` and `set`
SCENE_TIMEOUT_S = 15.0  # `scene`: the engine reads the clip files before it answers
ENGINE_ONLINE_WAIT_S = 1.0  # the retained online flag, before anything is sent
BROKER_WAIT_S = 3.0  # reaching the broker
STATE_TIMEOUT_S = 3.0  # the retained state and the online flag


@app.callback()
def _root() -> None:
    """RPG HOME. Start and stop with scripts/start.sh and scripts/stop.sh."""


def _fail(message: str) -> NoReturn:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


# -- talking to the engine ---------------------------------------------------------------


def send_command(
    bus: Bus, command: str, payload: dict, timeout_s: float | None = None
) -> dict | None:
    """Publish on `rpghome/cmd/<command>` and wait for the event carrying our `id`."""
    timeout_s = REPLY_TIMEOUT_S if timeout_s is None else timeout_s  # read at call time
    msg_id = uuid.uuid4().hex
    answered = threading.Event()
    answer: dict[str, dict] = {}

    def on_event(_topic: str, event: dict) -> None:
        if event.get("id") == msg_id and event.get("type") in ("ok", "error"):
            answer["event"] = event
            answered.set()

    bus.subscribe(topics.EVENTS, on_event)  # subscribe first: InMemoryBus delivers at once
    bus.publish(topics.cmd(command), {**payload, "id": msg_id})
    return answer.get("event") if answered.wait(timeout_s) else None


def report_command(command: str, event: dict | None, timeout_s: float) -> int:
    """Print the engine's answer. 0 for ok, 1 for an error or for no answer."""
    if event is None:
        _fail(f"the engine did not answer within {timeout_s:g} s (is it running? scripts/start.sh)")
    if event.get("type") == "ok":
        typer.echo(json.dumps(event, ensure_ascii=False))
        return 0
    typer.secho(json.dumps(event, ensure_ascii=False), fg=typer.colors.RED, err=True)
    return 1


def read_retained(bus: Bus, wanted: list[str], timeout_s: float) -> dict[str, dict]:
    """The last retained message of each topic (a retained message arrives at once)."""
    got: dict[str, dict] = {}
    missing = set(wanted)
    all_arrived = threading.Event()
    lock = threading.Lock()

    def on_message(topic: str, payload: dict) -> None:
        with lock:
            if topic not in missing:
                return
            missing.discard(topic)
            got[topic] = payload
            if not missing:
                all_arrived.set()

    for topic in wanted:
        bus.subscribe(topic, on_message)
    all_arrived.wait(timeout_s)
    return got


def engine_is_online(bus: Bus, timeout_s: float = ENGINE_ONLINE_WAIT_S) -> bool:
    """The retained `rpghome/engine/online`: is an engine there to take a command?"""
    online = read_retained(bus, [topics.ONLINE], timeout_s).get(topics.ONLINE)
    return isinstance(online, dict) and online.get("online") is True


def send_and_report(bus: Bus, command: str, payload: dict, timeout_s: float) -> int:
    """Send one command and report its answer. Nothing goes to an engine that is not there.

    The online flag is read first: a command sent to no one waits our whole timeout and
    comes back with nothing to say about why (the engine publishes the flag when it connects
    and the broker publishes `false` from its last will when it dies).
    """
    if not engine_is_online(bus):
        _fail("engine is offline; start it with scripts/start.sh")
    return report_command(command, send_command(bus, command, payload, timeout_s), timeout_s)


def command_timeout(value: float | None, default: float) -> float:
    """`--timeout`, or the command's own default - checked before anything is sent."""
    if value is None:
        return default  # read at call time, so the defaults can be changed
    if not value > 0:
        _fail(f"--timeout must be > 0, got {value:g}")
    return value


def show_status(bus: Bus, timeout_s: float = STATE_TIMEOUT_S) -> int:
    """Print the retained state and the online flag. 1 when the engine is offline."""
    got = read_retained(bus, [topics.STATE, topics.ONLINE], timeout_s)
    state, online = got.get(topics.STATE), got.get(topics.ONLINE)
    is_online = isinstance(online, dict) and online.get("online") is True
    typer.echo(f"engine online: {is_online}")
    if state is None:
        typer.echo("state: none on the broker")
    else:
        typer.echo("state:")
        typer.echo(json.dumps(state, indent=2, ensure_ascii=False))
    return 0 if is_online and state is not None else 1


def _with_bus(action: Callable[[Bus], int]) -> None:
    """Run `action(bus)` against the broker and exit with its code."""
    try:
        settings = config.load_settings()
    except config.ConfigError as exc:
        _fail(f"invalid settings: {exc}")
    bus = MqttBus(
        settings.mqtt_host, settings.mqtt_port, client_id=f"rpghome-cli-{uuid.uuid4().hex[:8]}"
    )
    try:
        if not bus.wait_connected(BROKER_WAIT_S):
            _fail(
                f"cannot reach the broker at {settings.mqtt_host}:{settings.mqtt_port}; "
                "start it with scripts/start.sh"
            )
        raise typer.Exit(action(bus))
    finally:
        bus.close()


# -- commands ------------------------------------------------------------------------------


@app.command("config")
def show_config() -> None:
    """Print the resolved settings. Exit code 1 if they are invalid."""
    try:
        s = config.load_settings()
    except config.ConfigError as exc:
        _fail(str(exc))
    typer.echo(f"data folder:  {s.artifacts_dir}")
    typer.echo(f"state folder: {s.state_dir}")
    typer.echo(f"log folder:   {s.log_dir}")
    typer.echo(f"mqtt:         {s.mqtt_host}:{s.mqtt_port}")


@app.command("scene")
def scene_command(
    file: Annotated[
        Path,
        typer.Argument(
            help="scene JSON: the scene data, with clip paths relative to the data folder"
        ),
    ],
    transition: Annotated[
        float | None, typer.Option("--transition", help="crossfade from the current scene (s)")
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="wait for the engine's answer (s)")
    ] = None,
) -> None:
    """Play a scene: the engine gets the scene data, never a scene name or file."""
    try:
        data = json.loads(file.read_text())
    except OSError as exc:
        _fail(f"cannot read {file}: {exc.strerror or exc}")
    except json.JSONDecodeError as exc:
        _fail(f"{file} is not valid JSON: {exc}")
    try:
        payload: dict = {"scene": scene_from_data(data).model_dump(mode="json")}
    except SceneError as exc:
        _fail(f"invalid scene: {exc}")
    if transition is not None:
        payload["transition_s"] = transition
    # the engine reads every clip of the scene before it answers, so this one waits longer
    wait_s = command_timeout(timeout, SCENE_TIMEOUT_S)
    _with_bus(lambda bus: send_and_report(bus, "scene", payload, wait_s))


@app.command("stop")
def stop_command(
    fade: Annotated[
        float | None, typer.Option("--fade", help="fade everything out over (s)")
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="wait for the engine's answer (s)")
    ] = None,
) -> None:
    """Stop the scene: fade out, then silence."""
    payload: dict = {} if fade is None else {"fade_s": fade}
    wait_s = command_timeout(timeout, REPLY_TIMEOUT_S)
    _with_bus(lambda bus: send_and_report(bus, "stop", payload, wait_s))


# NAME VALUE are taken as they come: `ignore_unknown_options` is what lets a level be
# negative (`rpghome set master_gain_db -6`), which a normal option parser reads as a flag.
@app.command("set", context_settings={"ignore_unknown_options": True})
def set_command(
    args: Annotated[
        list[str],
        typer.Argument(metavar="NAME VALUE", help="the level and its value in dB"),
    ],
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="wait for the engine's answer (s)")
    ] = None,
) -> None:
    """Set a level: master_gain_db, bed_gain_db (the bed of the current scene) or density."""
    if len(args) != 2:
        _fail("usage: rpghome set NAME VALUE, e.g. rpghome set master_gain_db -6")
    name, value = args
    try:
        number = float(value)
    except ValueError:
        _fail(f"value must be a number, got {value!r}")
    payload = {"name": name, "value": number}
    try:
        parse_command("set", payload)  # checked here too, so a typo fails before it is sent
    except CommandError as exc:
        _fail(str(exc))
    wait_s = command_timeout(timeout, REPLY_TIMEOUT_S)
    _with_bus(lambda bus: send_and_report(bus, "set", payload, wait_s))


@app.command("status")
def status_command() -> None:
    """Print the engine's retained state and whether it is online. Exit 1 when it is not."""
    _with_bus(show_status)


def main() -> None:
    app()
