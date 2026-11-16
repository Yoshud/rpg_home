from typer.testing import CliRunner

from rpghome.cli import main as cli

runner = CliRunner()


def test_config_prints_settings(env, monkeypatch):
    monkeypatch.setenv("RPGHOME_MQTT_PORT", "1999")
    result = runner.invoke(cli.app, ["config"])
    assert result.exit_code == 0, result.output
    root = env.resolve()
    assert f"data folder:  {root / 'artifacts'}" in result.output
    assert f"state folder: {root / 'state'}" in result.output
    assert f"log folder:   {root / 'state' / 'logs'}" in result.output
    assert "mqtt:         127.0.0.1:1999" in result.output


def test_config_invalid(env, monkeypatch):
    monkeypatch.setenv("RPGHOME_STATE", str(env / "artifacts" / "state"))
    result = runner.invoke(cli.app, ["config"])
    assert result.exit_code == 1
    assert "must not be inside the data folder" in result.output


def test_no_deployment_commands():
    # `status` is a command now (it reads the bus); starting and stopping stays with the scripts.
    for name in ("up", "down", "logs"):
        assert runner.invoke(cli.app, [name]).exit_code != 0
