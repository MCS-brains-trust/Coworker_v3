from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from coworker.db.session import get_session
from coworker.db.models.tenancy import Firm, User
from coworker.security.auth import build_auth_url, exchange_code

router = APIRouter(prefix="/auth", tags=["auth"])

@router.get("/start")
async def auth_start(firm_slug: str, request: Request, session: AsyncSession = Depends(get_session)):
    firm = await session.scalar(select(Firm).where(Firm.slug == firm_slug))
    if not firm:
        raise HTTPException(status_code=404, detail="Firm not found")
    
    # In a real implementation, state and code_verifier would be generated and stored in Redis
    state = "dummy_state"
    code_verifier = "dummy_verifier"
    
    url = build_auth_url(
        firm_tenant_id=firm.azure_tenant_id or "common",
        firm_client_id=firm.azure_client_id or "dummy",
        redirect_uri=str(request.url_for("auth_callback")),
        state=state,
        code_verifier=code_verifier
    )
    return {"url": url}

@router.get("/callback")
async def auth_callback(code: str, state: str, request: Request, session: AsyncSession = Depends(get_session)):
    # In a real implementation, we would validate state and retrieve code_verifier from Redis
    # Then exchange code for tokens, upsert User, and set JWT cookie
    return {"status": "ok", "message": "Auth callback received"}

@router.get("/me")
async def get_me(request: Request):
    # In a real implementation, we would decode the JWT cookie and return user info
    return {"status": "ok", "user": "dummy"}
