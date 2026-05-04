import click


@click.group()
def cli() -> None:
    """MC & S CoWorker v3 CLI."""
    pass


@cli.command()
def version() -> None:
    """Print the current version."""
    click.echo("MC & S CoWorker v3.0.0")

@cli.command("create-firm")
@click.argument("name")
@click.option("--slug", default=None, help="URL-safe identifier. Defaults to slugify(name).")
@click.option("--abn", default=None, help="Australian Business Number, exactly 11 digits.")
@click.option("--timezone", "timezone_", default="Australia/Melbourne", show_default=True,
              help="IANA timezone name (e.g. Australia/Sydney).")
def create_firm(name: str, slug: str | None, abn: str | None, timezone_: str) -> None:
    """Create a new firm tenant."""
    import asyncio
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    from slugify import slugify
    from coworker.db.session import SessionLocal
    from coworker.db.models.tenancy import Firm

    if abn is not None and (len(abn) != 11 or not abn.isdigit()):
        raise click.BadParameter("ABN must be exactly 11 digits.", param_hint="--abn")

    try:
        ZoneInfo(timezone_)
    except ZoneInfoNotFoundError as exc:
        raise click.BadParameter(f"Unknown IANA timezone: {timezone_!r}.", param_hint="--timezone") from exc

    resolved_slug = slug if slug is not None else slugify(name)

    async def _create():
        async with SessionLocal() as session:
            firm = Firm(name=name, slug=resolved_slug, abn=abn, timezone=timezone_)
            session.add(firm)
            await session.commit()
            click.echo(f"Created firm '{name}' with slug '{resolved_slug}'")

    asyncio.run(_create())
