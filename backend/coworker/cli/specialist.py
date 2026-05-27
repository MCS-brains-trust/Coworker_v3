"""Specialist prompt management CLI.

``coworker specialist seed`` loads the .md files under
``backend/coworker/specialists/prompts/`` into the database for one
firm. Idempotent and no-clobber by default: an unchanged body is a
silent skip; a changed body is also a skip unless ``--force`` is
passed, in which case the previous active version is retired and the
new body becomes the active version.

The dry-run path does all the work in one transaction and rolls back
at the end, so the printed action plan reflects exactly what the
non-dry-run pass would do.
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any

import click
import frontmatter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.db.firms import lookup_firm_by_slug
from coworker.db.models.specialist import Specialist, SpecialistPromptVersion
from coworker.db.session import SessionLocal, firm_context

PROMPTS_DIR = (
    Path(__file__).resolve().parent.parent / "specialists" / "prompts"
)

_REQUIRED_FRONTMATTER_KEYS = (
    "name",
    "display_name",
    "description",
    "model",
    "extended_thinking",
)


@click.group()
def specialist() -> None:
    """Specialist prompt management."""


@specialist.command()
@click.option(
    "--firm",
    "firm_slug",
    required=True,
    help="Firm slug (matches firms.slug), e.g. mc-s-accountants",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show the action plan but roll back at the end (no DB writes).",
)
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Overwrite existing prompts whose body has changed. Default is to "
        "skip changed prompts (no-clobber)."
    ),
)
def seed(firm_slug: str, dry_run: bool, force: bool) -> None:
    """Load specialist prompts from backend/coworker/specialists/prompts/."""
    files = _discover_prompt_files()
    if not files:
        raise click.ClickException(
            f"No prompt .md files found in {PROMPTS_DIR}"
        )

    parsed = [_parse_prompt_file(p) for p in files]
    results = asyncio.run(
        _run_seed(
            firm_slug=firm_slug,
            prompts=parsed,
            dry_run=dry_run,
            force=force,
        )
    )
    _render_summary(results, dry_run=dry_run)


def _discover_prompt_files() -> list[Path]:
    if not PROMPTS_DIR.exists():
        return []
    return sorted(PROMPTS_DIR.glob("*.md"))


def _parse_prompt_file(path: Path) -> dict[str, Any]:
    post = frontmatter.load(path)
    missing = [k for k in _REQUIRED_FRONTMATTER_KEYS if k not in post.metadata]
    if missing:
        raise click.ClickException(
            f"{path.name} is missing frontmatter keys: {', '.join(missing)}"
        )
    body = post.content.strip() + "\n"
    return {
        "path": path,
        "name": str(post["name"]),
        "display_name": str(post["display_name"]),
        "description": str(post["description"]),
        "model": str(post["model"]),
        "extended_thinking": bool(post["extended_thinking"]),
        "body": body,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


async def _run_seed(
    *,
    firm_slug: str,
    prompts: list[dict[str, Any]],
    dry_run: bool,
    force: bool,
) -> list[dict[str, Any]]:
    async with SessionLocal() as session:
        firm = await lookup_firm_by_slug(session, firm_slug)
        if firm is None:
            raise click.ClickException(
                f"No firm with slug {firm_slug!r}."
            )
        firm_id = firm.id
        await session.commit()

    async with SessionLocal() as session, firm_context(firm_id):
        results: list[dict[str, Any]] = []
        for prompt in prompts:
            result = await _apply_prompt(
                session,
                firm_id=firm_id,
                prompt=prompt,
                force=force,
            )
            results.append(result)
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    return results


async def _apply_prompt(
    session: AsyncSession,
    *,
    firm_id: uuid.UUID,
    prompt: dict[str, Any],
    force: bool,
) -> dict[str, Any]:
    existing = (
        await session.execute(
            select(Specialist).where(Specialist.name == prompt["name"])
        )
    ).scalar_one_or_none()

    if existing is None:
        spec = Specialist(
            firm_id=firm_id,
            name=prompt["name"],
            display_name=prompt["display_name"],
            description=prompt["description"],
            model=prompt["model"],
            extended_thinking=prompt["extended_thinking"],
        )
        session.add(spec)
        await session.flush()
        version = SpecialistPromptVersion(
            firm_id=firm_id,
            specialist_id=spec.id,
            version_number=1,
            prompt_text=prompt["body"],
            status="active",
            change_summary="initial seed",
            created_by_user_id=None,
        )
        session.add(version)
        await session.flush()
        spec.active_version_id = version.id
        await session.flush()
        return {
            "name": prompt["name"],
            "action": "created",
            "version_number": 1,
        }

    # Existing row: compare active prompt body hash.
    current_version: SpecialistPromptVersion | None = None
    if existing.active_version_id is not None:
        current_version = (
            await session.execute(
                select(SpecialistPromptVersion).where(
                    SpecialistPromptVersion.id == existing.active_version_id
                )
            )
        ).scalar_one()

    current_hash = (
        hashlib.sha256(current_version.prompt_text.encode("utf-8")).hexdigest()
        if current_version is not None
        else None
    )

    if current_hash == prompt["body_sha256"]:
        return {
            "name": prompt["name"],
            "action": "unchanged",
            "version_number": (
                current_version.version_number if current_version else None
            ),
        }

    if not force:
        return {
            "name": prompt["name"],
            "action": "skipped (use --force)",
            "version_number": (
                current_version.version_number if current_version else None
            ),
        }

    prev_number = current_version.version_number if current_version else 0
    if current_version is not None:
        await session.execute(
            update(SpecialistPromptVersion)
            .where(SpecialistPromptVersion.id == current_version.id)
            .values(status="retired")
        )
    new_version = SpecialistPromptVersion(
        firm_id=firm_id,
        specialist_id=existing.id,
        version_number=prev_number + 1,
        prompt_text=prompt["body"],
        status="active",
        change_summary="seed --force overwrite",
        created_by_user_id=None,
    )
    session.add(new_version)
    await session.flush()
    existing.active_version_id = new_version.id
    await session.flush()
    return {
        "name": prompt["name"],
        "action": "updated",
        "version_number": new_version.version_number,
    }


def _render_summary(
    results: list[dict[str, Any]], *, dry_run: bool
) -> None:
    header = f"{'Name':<32} {'Action':<24} {'Version':>7}"
    click.echo(header)
    click.echo("-" * len(header))
    for r in sorted(results, key=lambda x: str(x["name"])):
        version = "-" if r["version_number"] is None else str(r["version_number"])
        click.echo(f"{r['name']:<32} {r['action']:<24} {version:>7}")
    if dry_run:
        click.echo()
        click.echo("(dry-run: no changes written)")
