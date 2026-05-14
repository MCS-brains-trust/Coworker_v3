"""Smoke tests for the subscription-sweep CLI argparse surface."""
import pytest

from coworker.workers.subscribe import main


def test_help_flag_prints_usage(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "Microsoft Graph subscription sweep" in out
    assert "--dry-run" in out


def test_unknown_flag_rejected(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--not-a-flag"])
    err = capsys.readouterr().err
    assert "not-a-flag" in err or "unrecognized" in err
