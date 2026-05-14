"""Smoke tests for the worker entry-point CLI.

These don't actually start the loop (that needs DB + Redis +
plugins). They verify the argparse surface and the validation
gates so a typo in systemd unit args fails loud instead of
silently doing nothing.
"""
import pytest

from coworker.workers.run import main


def test_zero_concurrency_rejected(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--concurrency", "0"])
    err = capsys.readouterr().err
    assert "concurrency" in err


def test_negative_concurrency_rejected(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--concurrency", "-1"])
    err = capsys.readouterr().err
    assert "concurrency" in err


def test_zero_idle_poll_rejected(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--idle-poll-seconds", "0"])
    err = capsys.readouterr().err
    assert "idle-poll-seconds" in err


def test_help_flag_prints_usage(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    # argparse exits 0 on --help.
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--concurrency" in out
    assert "--idle-poll-seconds" in out


def test_unknown_flag_rejected(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--not-a-flag"])
    err = capsys.readouterr().err
    assert "not-a-flag" in err or "unrecognized" in err
