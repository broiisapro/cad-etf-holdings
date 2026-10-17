"""CLI smoke tests."""

from __future__ import annotations

from typer.testing import CliRunner

from etfh.cli import app

runner = CliRunner()

COMMANDS = ("collect", "parse", "build", "qa", "overlap", "changes")


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.stdout


def test_stub_command_exits_with_code_two() -> None:
    result = runner.invoke(app, ["build"])
    assert result.exit_code == 2
    assert "not implemented yet" in result.output
