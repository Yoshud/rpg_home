"""`rpghome` command line."""

import typer

from rpghome.cli import library
from rpghome.core import config

app = typer.Typer(no_args_is_help=True, add_completion=False, help="RPG HOME.")
app.add_typer(library.app, name="library")


@app.callback()
def _root() -> None:
    """RPG HOME. Start and stop with scripts/start.sh and scripts/stop.sh."""


@app.command("config")
def show_config() -> None:
    """Print the resolved settings. Exit code 1 if they are invalid."""
    try:
        s = config.load_settings()
    except config.ConfigError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    typer.echo(f"data folder:  {s.artifacts_dir}")
    typer.echo(f"state folder: {s.state_dir}")
    typer.echo(f"log folder:   {s.log_dir}")
    typer.echo(f"mqtt:         {s.mqtt_host}:{s.mqtt_port}")


def main() -> None:
    app()
