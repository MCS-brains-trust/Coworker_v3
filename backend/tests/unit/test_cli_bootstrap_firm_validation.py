"""Unit tests for bootstrap-firm input validation.

These exercise click-level argument validation only — they never reach
the DB code path because click raises BadParameter before SessionLocal
is touched.
"""
import uuid

from click.testing import CliRunner

from coworker.cli.main import cli


_VALID_GUID = str(uuid.uuid4())


def _invoke(**overrides: str) -> object:
    args = {
        "--slug": "acme",
        "--name": "Acme",
        "--azure-tenant-id": _VALID_GUID,
        "--azure-client-id": _VALID_GUID,
        "--azure-client-secret": "shh",
    }
    args.update(overrides)
    flat: list[str] = ["bootstrap-firm"]
    for k, v in args.items():
        flat += [k, v]
    return CliRunner().invoke(cli, flat)


def test_bootstrap_firm_rejects_bad_tenant_guid() -> None:
    result = _invoke(**{"--azure-tenant-id": "not-a-guid"})
    assert result.exit_code != 0
    assert "Not a valid GUID" in result.output
    assert "--azure-tenant-id" in result.output


def test_bootstrap_firm_rejects_bad_client_guid() -> None:
    result = _invoke(**{"--azure-client-id": "also-not-a-guid"})
    assert result.exit_code != 0
    assert "Not a valid GUID" in result.output
    assert "--azure-client-id" in result.output


def test_bootstrap_firm_rejects_bad_abn() -> None:
    result = _invoke(**{"--abn": "12345"})
    assert result.exit_code != 0
    assert "ABN must be exactly 11 digits" in result.output


def test_bootstrap_firm_rejects_bad_timezone() -> None:
    result = _invoke(**{"--timezone": "Mars/Olympus_Mons"})
    assert result.exit_code != 0
    assert "Unknown IANA timezone" in result.output
