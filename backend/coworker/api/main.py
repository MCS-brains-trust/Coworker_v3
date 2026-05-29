from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from coworker.api.routes import approval, auth, chat, mail, specialist, webhooks
from coworker.config import get_settings
from coworker.db.session import engine
from coworker.logging import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    setup_logging()
    yield
    await engine.dispose()


app = FastAPI(
    title="MC & S CoWorker v3",
    version="3.0.0",
    lifespan=lifespan,
    docs_url="/docs" if get_settings().ENVIRONMENT != "production" else None,
)
app.include_router(approval.router)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(mail.router)
app.include_router(specialist.router)
app.include_router(webhooks.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness + database reachability check."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {
        "status": "ok",
        "service": "coworker-api",
        "version": app.version,
        "shadow_mode": str(get_settings().SHADOW_MODE),
    }


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "MC & S CoWorker v3"}
