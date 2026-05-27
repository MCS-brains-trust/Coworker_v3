"""End-to-end tests for ``coworker specialist seed``.

Runs the Click CLI in-process via ``CliRunner``, against the real
test DB. Each test seeds a fresh firm, optionally writes test
prompt .md files into a temp directory, monkeypatches the CLI's
``PROMPTS_DIR`` to point at that temp directory, and asserts the
DB state after the run.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest_asyncio
from click.testing import CliRunner
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.cli import specialist as specialist_cli
from coworker.db.models import (
    Firm,
    Specialist,
    SpecialistPromptVersion,
)
from coworker.db.session import _attach_pool_listeners, firm_context

_CLEANUP_TABLES = (
    "firms",
    "users",
    "audit_log",
    "specialists",
    "specialist_prompt_versions",
)

# The seeder validates min body length implicitly through the routes,
# but for the CLI seed step there's no min body length. Tests use a
# fixed prefix so updates can re-seed with a longer or different body
# without tripping anything.
_BASE_BODY = "Test specialist prompt body. " * 8


def _make_prompt(
    tmpdir: Path,
    *,
    name: str,
    display_name: str,
    description: str = "Test specialist description.",
    body: str = _BASE_BODY,
) -> Path:
    path = tmpdir / f"{name}.md"
    content = (
        "---\n"
        f"name: {name}\n"
        f"display_name: {display_name}\n"
        f"description: {description}\n"
        "model: claude-opus-4-7\n"
        "extended_thinking: true\n"
        "---\n"
        "\n"
        f"{body}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


@pytest_asyncio.fixture
async def cli_env(test_database_url, monkeypatch, tmp_path):
    from coworker.db import session as session_module

    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: sm)
    monkeypatch.setattr(session_module, "get_engine", lambda: engine)
    # The CLI imports SessionLocal directly into its module namespace,
    # so patching session_module isn't enough.
    monkeypatch.setattr(specialist_cli, "SessionLocal", sm)

    firm_id = uuid.uuid4()
    slug = f"cli-{uuid.uuid4().hex[:8]}"
    async with sm() as session, firm_context(firm_id):
        session.add(Firm(id=firm_id, name="CLI Firm", slug=slug))
        await session.commit()

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    monkeypatch.setattr(specialist_cli, "PROMPTS_DIR", prompts_dir)

    try:
        yield {
            "sm": sm,
            "firm_id": firm_id,
            "slug": slug,
            "prompts_dir": prompts_dir,
        }
    finally:
        await _cleanup_firm(sm, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm, firm_id: uuid.UUID) -> None:
    async with sm() as session:
        for t in _CLEANUP_TABLES:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            await session.execute(
                text(
                    "UPDATE specialists SET active_version_id = NULL "
                    "WHERE firm_id = :id"
                ),
                {"id": str(firm_id)},
            )
            for t in (
                "specialist_prompt_versions",
                "specialists",
                "audit_log",
                "users",
            ):
                await session.execute(
                    text(f"DELETE FROM {t} WHERE firm_id = :id"),
                    {"id": str(firm_id)},
                )
            await session.execute(
                text("DELETE FROM firms WHERE id = :id"),
                {"id": str(firm_id)},
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    async with sm() as session:
        for t in _CLEANUP_TABLES:
            await session.execute(
                text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            )
        await session.commit()


def _seed_five_files(prompts_dir: Path) -> None:
    for name, display in [
        ("gst", "GST"),
        ("smsf", "SMSF"),
        ("div7a", "Division 7A"),
        ("trust_tax", "Trust Tax"),
        ("cgt_concessions_rollovers", "CGT Concessions and Rollovers"),
    ]:
        _make_prompt(prompts_dir, name=name, display_name=display)


def _count_rows(sm, firm_id: uuid.UUID) -> dict[str, int]:
    async def _go() -> dict[str, int]:
        async with sm() as session, firm_context(firm_id):
            specs = (
                await session.execute(select(Specialist))
            ).scalars().all()
            versions = (
                await session.execute(select(SpecialistPromptVersion))
            ).scalars().all()
            return {
                "specialists": len(specs),
                "versions": len(versions),
                "active_versions": sum(
                    1 for v in versions if v.status == "active"
                ),
            }
    return asyncio.run(_go())


# ===========================================================================
# Tests
# ===========================================================================


def test_seed_creates_new_specialists(cli_env) -> None:
    _seed_five_files(cli_env["prompts_dir"])
    runner = CliRunner()
    result = runner.invoke(
        specialist_cli.specialist,
        ["seed", "--firm", cli_env["slug"]],
    )
    assert result.exit_code == 0, result.output
    assert result.output.count("created") == 5

    counts = _count_rows(cli_env["sm"], cli_env["firm_id"])
    assert counts["specialists"] == 5
    assert counts["versions"] == 5
    assert counts["active_versions"] == 5


def test_seed_is_idempotent(cli_env) -> None:
    _seed_five_files(cli_env["prompts_dir"])
    runner = CliRunner()
    first = runner.invoke(
        specialist_cli.specialist,
        ["seed", "--firm", cli_env["slug"]],
    )
    assert first.exit_code == 0
    second = runner.invoke(
        specialist_cli.specialist,
        ["seed", "--firm", cli_env["slug"]],
    )
    assert second.exit_code == 0, second.output
    assert second.output.count("unchanged") == 5
    assert "created" not in second.output

    counts = _count_rows(cli_env["sm"], cli_env["firm_id"])
    assert counts["specialists"] == 5
    assert counts["versions"] == 5  # no new versions on idempotent run


def test_seed_no_force_skips_changed(cli_env) -> None:
    _seed_five_files(cli_env["prompts_dir"])
    runner = CliRunner()
    first = runner.invoke(
        specialist_cli.specialist,
        ["seed", "--firm", cli_env["slug"]],
    )
    assert first.exit_code == 0

    # Modify one prompt's body.
    _make_prompt(
        cli_env["prompts_dir"],
        name="gst", display_name="GST",
        body="Completely different body for GST. " * 8,
    )

    second = runner.invoke(
        specialist_cli.specialist,
        ["seed", "--firm", cli_env["slug"]],
    )
    assert second.exit_code == 0, second.output
    assert "skipped" in second.output
    assert "use --force" in second.output

    counts = _count_rows(cli_env["sm"], cli_env["firm_id"])
    assert counts["versions"] == 5  # no new version inserted


def test_seed_force_updates_changed(cli_env) -> None:
    _seed_five_files(cli_env["prompts_dir"])
    runner = CliRunner()
    first = runner.invoke(
        specialist_cli.specialist,
        ["seed", "--firm", cli_env["slug"]],
    )
    assert first.exit_code == 0

    # Modify one prompt's body.
    new_body = "Completely different body for GST. " * 8
    _make_prompt(
        cli_env["prompts_dir"],
        name="gst", display_name="GST",
        body=new_body,
    )

    second = runner.invoke(
        specialist_cli.specialist,
        ["seed", "--firm", cli_env["slug"], "--force"],
    )
    assert second.exit_code == 0, second.output
    assert "updated" in second.output

    counts = _count_rows(cli_env["sm"], cli_env["firm_id"])
    assert counts["versions"] == 6  # one new version row inserted
    assert counts["active_versions"] == 5  # still exactly one active per

    # Confirm the active row for gst now has version 2 and new body.
    async def _check() -> tuple[int, str]:
        async with cli_env["sm"]() as session, firm_context(
            cli_env["firm_id"]
        ):
            spec = (
                await session.execute(
                    select(Specialist).where(Specialist.name == "gst")
                )
            ).scalar_one()
            version = (
                await session.execute(
                    select(SpecialistPromptVersion).where(
                        SpecialistPromptVersion.id == spec.active_version_id
                    )
                )
            ).scalar_one()
            return version.version_number, version.prompt_text
    vnum, ptext = asyncio.run(_check())
    assert vnum == 2
    assert ptext.startswith("Completely different body")


def test_seed_dry_run_makes_no_writes(cli_env) -> None:
    _seed_five_files(cli_env["prompts_dir"])
    runner = CliRunner()
    result = runner.invoke(
        specialist_cli.specialist,
        ["seed", "--firm", cli_env["slug"], "--dry-run"],
    )
    assert result.exit_code == 0
    assert "dry-run" in result.output
    assert result.output.count("created") == 5

    counts = _count_rows(cli_env["sm"], cli_env["firm_id"])
    assert counts["specialists"] == 0
    assert counts["versions"] == 0


def test_seed_unknown_firm_errors(cli_env) -> None:
    _seed_five_files(cli_env["prompts_dir"])
    runner = CliRunner()
    result = runner.invoke(
        specialist_cli.specialist,
        ["seed", "--firm", "definitely-not-a-firm-slug"],
    )
    assert result.exit_code != 0
    assert "No firm" in result.output

    counts = _count_rows(cli_env["sm"], cli_env["firm_id"])
    assert counts["specialists"] == 0
