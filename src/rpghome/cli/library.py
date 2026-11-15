"""`rpghome library`: the clip catalog. Typer is synchronous: each command runs its async body
with `asyncio.run`."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from rpghome.core import config
from rpghome.core.errors import RpgHomeError
from rpghome.managers import library
from rpghome.models.clip import Clip, Kind, Source, Status
from rpghome.repositories.catalog import DB_NAME, Catalog

app = typer.Typer(no_args_is_help=True, help="The clip catalog in the data folder.")


def _fail(message: str) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(1)


def _settings() -> config.Settings:
    try:
        return config.load_settings()
    except RpgHomeError as exc:
        raise _fail(str(exc)) from None


def _catalog(create: bool) -> Catalog:
    data = _settings().artifacts_dir
    if not create and not (data / DB_NAME).exists():
        raise _fail(f"no catalog in {data} yet: run `rpghome library import-sound-gen`")
    return Catalog(data)


@app.command("import-sound-gen")
def import_sound_gen(
    path: Annotated[Path, typer.Argument(help="the sound-gen folder")] = library.DEFAULT_SOUND_GEN,
) -> None:
    """Copy the sound-gen test clips into the data folder and add them to the catalog.
    Safe to run again: nothing changes when nothing changed."""
    lock = _settings().state_dir / "locks" / library.LOCK_NAME

    async def run() -> tuple[Catalog, library.ImportReport]:
        async with _catalog(create=True) as catalog:
            report = await library.import_sound_gen(
                path, catalog, lock_path=lock, progress=lambda msg: typer.echo(msg, err=True)
            )
        return catalog, report

    try:
        catalog, report = asyncio.run(run())
    except RpgHomeError as exc:
        raise _fail(str(exc)) from None
    typer.echo(
        f"created {len(report.created)}, updated {len(report.updated)}, "
        f"unchanged {len(report.unchanged)}, files copied {len(report.copied)}"
    )
    for missing in report.missing:
        typer.secho(f"missing file, skipped: {missing}", fg=typer.colors.YELLOW)
    for message in report.kept + report.skipped:
        typer.secho(message, fg=typer.colors.YELLOW)
    typer.echo(f"catalog: {catalog.db_path}, export: {catalog.export_path}")


def _line(clip: Clip) -> str:
    rating = str(clip.rating) if clip.rating is not None else "-"
    gain = f"{clip.gain_db:+.1f}dB" if clip.gain_db is not None else "-"
    return (
        f"{clip.id:<52} {clip.kind.value:<5} {clip.status.value:<8} {rating} "
        f"{gain:>8}  {clip.title}"
    )


@app.command()
def search(
    text: Annotated[str | None, typer.Argument(help="text in title, prompt, notes or tags")] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option("--tag", "-t", help="tag, repeatable (AND); `cave` also matches place:cave"),
    ] = None,
    kind: Annotated[Kind | None, typer.Option(help="clip kind")] = None,
    status: Annotated[Status | None, typer.Option(help="review status")] = None,
    source: Annotated[Source | None, typer.Option(help="where the clip comes from")] = None,
) -> None:
    """List matching clips, one per line: id, kind, status, rating, gain, title."""

    async def run() -> list[Clip]:
        async with _catalog(create=False) as catalog:
            return await catalog.search(text, tag or (), kind=kind, status=status, source=source)

    try:
        clips = asyncio.run(run())
    except RpgHomeError as exc:
        raise _fail(str(exc)) from None
    for clip in clips:
        typer.echo(_line(clip))
    typer.echo(f"{len(clips)} clip(s)", err=True)


@app.command()
def pools() -> None:
    """Every pool: clip count, kinds, accepted clips."""

    async def run() -> list[library.PoolSummary]:
        async with _catalog(create=False) as catalog:
            return await library.pool_summaries(catalog)

    summaries = asyncio.run(run())
    typer.echo(f"{'pool':<16} {'clips':>5}  {'kinds':<12} {'accepted':>8}")
    for p in summaries:
        kinds = ",".join(p.kinds) or "-"
        typer.echo(f"{p.name:<16} {p.clips:>5}  {kinds:<12} {p.accepted:>8}")
