"""Unit tests for create-sandbox-firm CLI input validation.

Click-level only — these never reach the DB code path because
BadParameter raises before SessionLocal is touched.
"""
from click.testing import CliRunner

from coworker.cli.main import cli


def _invoke(**overrides: str) -> object:
    args = {
        "--name": "Sandbox Co",
        "--catchall": "sink@coworker.test",
    }
    args.update(overrides)
    flat: list[str] = ["create-sandbox-firm"]
    for k, v in args.items():
        flat += [k, v]
    return CliRunner().invoke(cli, flat)


def test_rejects_missing_catchall() -> None:
    result = CliRunner().invoke(
        cli, ["create-sandbox-firm", "--name", "Sandbox Co"],
    )
    assert result.exit_code != 0
    assert "--catchall" in result.output


def test_rejects_invalid_catchall_no_at() -> None:
    result = _invoke(**{"--catchall": "not-an-email"})
    assert result.exit_code != 0
    assert "Not a valid email" in result.output
    assert "--catchall" in result.output


def test_rejects_invalid_catchall_leading_at() -> None:
    result = _invoke(**{"--catchall": "@example.com"})
    assert result.exit_code != 0
    assert "Not a valid email" in result.output


def test_rejects_invalid_catchall_trailing_at() -> None:
    result = _invoke(**{"--catchall": "user@"})
    assert result.exit_code != 0
    assert "Not a valid email" in result.output


def test_rejects_bad_timezone() -> None:
    result = _invoke(**{"--timezone": "Mars/Olympus_Mons"})
    assert result.exit_code != 0
    assert "Unknown IANA timezone" in result.output


def test_help_lists_command() -> None:
    result = CliRunner().invoke(cli, ["create-sandbox-firm", "--help"])
    assert result.exit_code == 0
    assert "sandbox firm" in result.output.lower()
    assert "--catchall" in result.output
