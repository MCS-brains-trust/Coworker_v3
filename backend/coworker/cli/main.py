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
def create_firm(name: str) -> None:
    """Create a new firm tenant."""
    import asyncio
    from slugify import slugify
    from coworker.db.session import get_session_maker
    from coworker.db.models.tenancy import Firm

    async def _create():
        session_maker = get_session_maker()
        async with session_maker() as session:
            slug = slugify(name)
            firm = Firm(name=name, slug=slug)
            session.add(firm)
            await session.commit()
            click.echo(f"Created firm '{name}' with slug '{slug}'")

    asyncio.run(_create())
