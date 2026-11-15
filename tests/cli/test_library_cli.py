from typer.testing import CliRunner

from rpghome.cli import main as cli

runner = CliRunner()


def test_search_without_catalog(env):
    result = runner.invoke(cli.app, ["library", "search"])
    assert result.exit_code == 1
    assert "import-sound-gen" in result.output


def test_search_with_a_broken_catalog(env):
    """An old-schema or corrupt catalog.sqlite: one error line and exit 1, no traceback."""
    data = env / "artifacts"
    data.mkdir(parents=True)
    (data / "catalog.sqlite").write_bytes(b"not a database")
    result = runner.invoke(cli.app, ["library", "search"])
    assert result.exit_code == 1
    assert "not a valid database" in result.output
    assert "Traceback" not in result.output
    assert len([line for line in result.output.splitlines() if "error:" in line]) == 1


def test_import_search_pools(env, needs_ffmpeg, sound_gen):
    result = runner.invoke(cli.app, ["library", "import-sound-gen", str(sound_gen)])
    assert result.exit_code == 0, result.output
    assert "created 10, updated 0, unchanged 0, files copied 10" in result.output
    data = env / "artifacts"
    assert (data / "catalog.jsonl").is_file()
    export = (data / "catalog.jsonl").read_bytes()

    again = runner.invoke(cli.app, ["library", "import-sound-gen", str(sound_gen)])
    assert "created 0, updated 0, unchanged 10, files copied 0" in again.output
    assert (data / "catalog.jsonl").read_bytes() == export

    result = runner.invoke(
        cli.app, ["library", "search", "--tag", "place:cave", "-t", "source:stone"]
    )
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert lines[1].startswith("moss:sound-gen/spots/rockfall__s1 ")
    assert " spot " in lines[1] and " review " in lines[1]

    result = runner.invoke(
        cli.app, ["library", "search", "cave", "--kind", "bed", "--status", "accepted"]
    )
    assert result.stdout.splitlines() == [
        line for line in result.stdout.splitlines() if "cave_ambience__default__s1" in line
    ]
    assert len(result.stdout.splitlines()) == 1
    assert " 5 " in result.stdout

    assert runner.invoke(cli.app, ["library", "search", "--tag", "bad:tag"]).exit_code == 1
    assert runner.invoke(cli.app, ["library", "search", "--kind", "nope"]).exit_code != 0

    result = runner.invoke(cli.app, ["library", "pools"])
    assert result.exit_code == 0, result.output
    rows = {line.split()[0]: line.split()[1:] for line in result.stdout.splitlines()[1:]}
    assert rows["cave.bed"] == ["5", "bed", "1"]
    assert rows["storm.thunder"] == ["0", "-", "0"]
    assert rows["swords.clash"] == ["2", "fx", "1"]
