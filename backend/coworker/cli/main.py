import uuid
from typing import Any

import click
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.cli.specialist import specialist as specialist_group


@click.group()
def cli() -> None:
    """MC & S CoWorker v3 CLI."""
    pass


cli.add_command(specialist_group)


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

    from coworker.db.models.tenancy import Firm
    from coworker.db.session import SessionLocal, firm_context

    if abn is not None and (len(abn) != 11 or not abn.isdigit()):
        raise click.BadParameter("ABN must be exactly 11 digits.", param_hint="--abn")

    try:
        ZoneInfo(timezone_)
    except ZoneInfoNotFoundError as exc:
        raise click.BadParameter(f"Unknown IANA timezone: {timezone_!r}.", param_hint="--timezone") from exc

    resolved_slug = slug if slug is not None else slugify(name)

    # Pre-generate the firm id so we can enter firm_context BEFORE the
    # INSERT. Under FORCE ROW LEVEL SECURITY on `firms` (Stage C2), the
    # INSERT's WITH CHECK predicate is `id = NULLIF(current_setting('app.firm_id',
    # true), '')::uuid`, so app.firm_id must already match the row's id at
    # transaction begin or the INSERT is denied. The Session after_begin
    # listener picks the value up from the firm_context ContextVar.
    firm_id = uuid.uuid4()

    async def _create():
        async with SessionLocal() as session, firm_context(firm_id):
            firm = Firm(id=firm_id, name=name, slug=resolved_slug, abn=abn, timezone=timezone_)
            session.add(firm)
            await session.commit()
        click.echo(f"Created firm '{name}' with slug '{resolved_slug}' (id={firm_id})")

    asyncio.run(_create())


@cli.command("bootstrap-firm")
@click.option("--slug", required=True, help="URL-safe identifier. UPSERT key.")
@click.option("--name", required=True, help="Display name for the firm.")
@click.option("--azure-tenant-id", "azure_tenant_id", required=True,
              help="GUID of the firm's Microsoft 365 tenant.")
@click.option("--azure-client-id", "azure_client_id", required=True,
              help="GUID of the firm's Azure AD app registration (client ID).")
@click.option("--azure-client-secret", "azure_client_secret", required=True,
              help="Client secret for the Azure AD app. Encrypted before storage.")
@click.option("--timezone", "timezone_", default="Australia/Melbourne", show_default=True,
              help="IANA timezone name (e.g. Australia/Sydney).")
@click.option("--abn", default=None, help="Australian Business Number, exactly 11 digits.")
def bootstrap_firm(
    slug: str,
    name: str,
    azure_tenant_id: str,
    azure_client_id: str,
    azure_client_secret: str,
    timezone_: str,
    abn: str | None,
) -> None:
    """Provision (or refresh) a firm with encrypted Azure AD credentials.

    Idempotent on --slug. On a fresh slug, all fields are persisted. On an
    existing slug, only the three Azure credential fields are refreshed —
    name/abn/timezone are left as-is. To change those, edit the row directly.
    """
    import asyncio
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from coworker.db.session import SessionLocal

    try:
        uuid.UUID(azure_tenant_id)
    except ValueError as exc:
        raise click.BadParameter(
            f"Not a valid GUID: {azure_tenant_id!r}.", param_hint="--azure-tenant-id"
        ) from exc
    try:
        uuid.UUID(azure_client_id)
    except ValueError as exc:
        raise click.BadParameter(
            f"Not a valid GUID: {azure_client_id!r}.", param_hint="--azure-client-id"
        ) from exc

    if abn is not None and (len(abn) != 11 or not abn.isdigit()):
        raise click.BadParameter("ABN must be exactly 11 digits.", param_hint="--abn")

    try:
        ZoneInfo(timezone_)
    except ZoneInfoNotFoundError as exc:
        raise click.BadParameter(
            f"Unknown IANA timezone: {timezone_!r}.", param_hint="--timezone"
        ) from exc

    async def _run() -> uuid.UUID:
        async with SessionLocal() as session:
            firm_id = await _bootstrap_firm(
                session,
                slug=slug,
                name=name,
                azure_tenant_id=azure_tenant_id,
                azure_client_id=azure_client_id,
                azure_client_secret=azure_client_secret,
                timezone=timezone_,
                abn=abn,
            )
            await session.commit()
            return firm_id

    firm_id = asyncio.run(_run())
    click.echo(str(firm_id))


@cli.command("create-sandbox-firm")
@click.option("--name", required=True, help="Display name for the firm.")
@click.option("--slug", default=None, help="URL-safe identifier. Defaults to slugify(name).")
@click.option("--catchall", required=True,
              help="Email address that all outbound recipients are rewritten to. Required.")
@click.option("--timezone", "timezone_", default="Australia/Melbourne", show_default=True,
              help="IANA timezone name.")
def create_sandbox_firm(
    name: str, slug: str | None, catchall: str, timezone_: str,
) -> None:
    """Create a sandbox firm with outbound recipient rewriting.

    All connector-level outbound writes (Graph drafts, FuseSign
    envelopes) reroute their recipient addresses to ``--catchall``
    even when shadow_mode=False. Use this to drive the whole
    pipeline against synthetic data without touching real clients.

    Per Phase 9-1 onwards, ``shadow_mode`` is left at its default
    (True). Flip it off explicitly with a separate SQL UPDATE once
    you're ready to exercise the dispatch sweep end-to-end.
    """
    import asyncio
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from slugify import slugify

    from coworker.db.models.tenancy import Firm
    from coworker.db.session import SessionLocal, firm_context

    # Cheap email-shape sanity. The real validation happens at the
    # connector layer when an outbound actually fires; this is a
    # smoke check so we don't insert obviously-bad rows.
    if "@" not in catchall or catchall.count("@") != 1:
        raise click.BadParameter(
            f"Not a valid email: {catchall!r}.", param_hint="--catchall",
        )
    if catchall.startswith("@") or catchall.endswith("@"):
        raise click.BadParameter(
            f"Not a valid email: {catchall!r}.", param_hint="--catchall",
        )

    try:
        ZoneInfo(timezone_)
    except ZoneInfoNotFoundError as exc:
        raise click.BadParameter(
            f"Unknown IANA timezone: {timezone_!r}.", param_hint="--timezone",
        ) from exc

    resolved_slug = slug if slug is not None else slugify(name)
    firm_id = uuid.uuid4()

    async def _create() -> None:
        async with SessionLocal() as session, firm_context(firm_id):
            session.add(Firm(
                id=firm_id, name=name, slug=resolved_slug,
                timezone=timezone_,
                is_sandbox=True,
                sandbox_outbound_catchall=catchall,
            ))
            await session.commit()
        click.echo(
            f"Created sandbox firm {name!r} slug={resolved_slug} "
            f"catchall={catchall} id={firm_id}"
        )

    asyncio.run(_create())


@cli.command("tokens")
@click.option(
    "--firm",
    "firm_slug",
    required=True,
    help="Slug of the firm to report on (matches firms.slug).",
)
@click.option(
    "--month",
    "month",
    required=True,
    help="Reporting month in YYYY-MM format, UTC.",
)
@click.option(
    "--flush/--no-flush",
    "do_flush",
    default=True,
    show_default=True,
    help=(
        "Flush live Redis counters into Postgres before querying. "
        "--no-flush is useful for testing or when the APScheduler "
        "flush has already run."
    ),
)
def tokens(firm_slug: str, month: str, do_flush: bool) -> None:
    """Per-firm token-spend report for a given calendar month.

    Aggregates the ``token_usage`` table by model and prints a small
    table. Live Redis counters are flushed first by default so the
    report includes today's data; pass ``--no-flush`` to skip.

    Example:
        coworker tokens --firm mcands --month 2026-05
    """
    import asyncio
    import calendar as _cal
    import datetime as _dt
    import re as _re

    from sqlalchemy import select

    from coworker.db.firms import lookup_firm_by_slug
    from coworker.db.models.token_usage import TokenUsageRow
    from coworker.db.redis import get_redis
    from coworker.db.session import SessionLocal, firm_context
    from coworker.observability.token_meter import (
        flush_token_meter_to_postgres,
    )

    match = _re.fullmatch(r"(\d{4})-(\d{2})", month)
    if not match:
        raise click.BadParameter(
            f"Expected YYYY-MM, got {month!r}.", param_hint="--month"
        )
    year, mon = int(match.group(1)), int(match.group(2))
    if not 1 <= mon <= 12:
        raise click.BadParameter(
            f"Month must be 01-12, got {mon:02d}.", param_hint="--month"
        )
    first_day = _dt.date(year, mon, 1)
    last_day = _dt.date(year, mon, _cal.monthrange(year, mon)[1])

    async def _run() -> tuple[str, list[TokenUsageRow]]:
        sessionmaker = SessionLocal
        if do_flush:
            await flush_token_meter_to_postgres(get_redis(), sessionmaker)

        async with sessionmaker() as session:
            firm = await lookup_firm_by_slug(session, firm_slug)
            if firm is None:
                raise click.ClickException(
                    f"No firm with slug {firm_slug!r}."
                )
            firm_id = firm.id
            firm_name = firm.name
            await session.commit()

        async with sessionmaker() as session, firm_context(firm_id):
            result = await session.execute(
                select(TokenUsageRow)
                .where(TokenUsageRow.firm_id == firm_id)
                .where(TokenUsageRow.day >= first_day)
                .where(TokenUsageRow.day <= last_day)
                .order_by(TokenUsageRow.model, TokenUsageRow.day)
            )
            rows = list(result.scalars().all())

        return firm_name, rows

    firm_name, rows = asyncio.run(_run())
    _render_token_report(
        firm_slug=firm_slug,
        firm_name=firm_name,
        month=month,
        rows=rows,
    )


def _render_token_report(
    *,
    firm_slug: str,
    firm_name: str,
    month: str,
    rows: list[Any],
) -> None:
    """Print a per-model totals table for a token report."""
    # Aggregate by model. dict preserves insertion order so the
    # table follows the row ordering from the query
    # (model ASC, day ASC).
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = totals.setdefault(
            row.model,
            {"input_tokens": 0, "output_tokens": 0, "calls": 0},
        )
        bucket["input_tokens"] += int(row.input_tokens)
        bucket["output_tokens"] += int(row.output_tokens)
        bucket["calls"] += int(row.calls)

    click.echo(
        f"Token usage for firm '{firm_slug}' ({firm_name}) — {month}"
    )
    click.echo("=" * 72)
    if not totals:
        click.echo("(no usage recorded in this period)")
        return

    header = f"{'Model':<32} {'Input':>12} {'Output':>12} {'Calls':>10}"
    click.echo(header)
    click.echo("-" * 72)
    grand = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    for model, bucket in totals.items():
        click.echo(
            f"{model:<32} "
            f"{bucket['input_tokens']:>12,} "
            f"{bucket['output_tokens']:>12,} "
            f"{bucket['calls']:>10,}"
        )
        for k in grand:
            grand[k] += bucket[k]
    click.echo("-" * 72)
    click.echo(
        f"{'TOTAL':<32} "
        f"{grand['input_tokens']:>12,} "
        f"{grand['output_tokens']:>12,} "
        f"{grand['calls']:>10,}"
    )


async def _bootstrap_firm(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    azure_tenant_id: str,
    azure_client_id: str,
    azure_client_secret: str,
    timezone: str = "Australia/Melbourne",
    abn: str | None = None,
) -> uuid.UUID:
    """UPSERT a firm by slug. Returns the firm's UUID. Caller commits.

    The lookup uses `lookup_firm_by_slug`, which lifts FORCE RLS on
    `firms` only for its SELECT. We then commit (closing that
    transaction) and do the INSERT/UPDATE in a fresh transaction under
    `firm_context(firm_id)` — the per-row INSERT/UPDATE policies pass
    because app.firm_id matches the row's id. This keeps RLS bypass
    narrowly scoped to the slug-to-id resolution step.
    """
    from coworker.db.firms import lookup_firm_by_slug
    from coworker.db.models.tenancy import Firm
    from coworker.db.session import firm_context
    from coworker.security.encryption import encrypt_str

    existing = await lookup_firm_by_slug(session, slug)
    is_new = existing is None
    firm_id = uuid.uuid4() if is_new else existing.id  # type: ignore[union-attr]
    ciphertext = encrypt_str(azure_client_secret, firm_id=str(firm_id))

    # Close the lookup's transaction so the next execute (INSERT/UPDATE)
    # opens a fresh one under firm_context — that's the only way the
    # after_begin listener can apply app.firm_id for the per-row policy.
    await session.commit()

    async with firm_context(firm_id):
        if is_new:
            session.add(
                Firm(
                    id=firm_id,
                    slug=slug,
                    name=name,
                    abn=abn,
                    timezone=timezone,
                    azure_tenant_id=azure_tenant_id,
                    azure_client_id=azure_client_id,
                    azure_client_secret_ciphertext=ciphertext,
                )
            )
        else:
            await session.execute(
                update(Firm)
                .where(Firm.id == firm_id)
                .values(
                    azure_tenant_id=azure_tenant_id,
                    azure_client_id=azure_client_id,
                    azure_client_secret_ciphertext=ciphertext,
                )
            )
        await session.flush()
    return firm_id
