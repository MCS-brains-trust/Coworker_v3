from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

async def with_firm_scope(session: AsyncSession, firm_id: str):
    await session.execute(
        text("SET LOCAL coworker.current_firm_id = :f"), {"f": firm_id}
    )
