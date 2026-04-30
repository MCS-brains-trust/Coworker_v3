import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import base64
import hashlib

import httpx

from coworker.config import get_settings
from coworker.security.encryption import encrypt_str

GRAPH_SCOPES = [
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.Read",
    "Files.Read.All",
    "Sites.Read.All",
    "offline_access",
]


def _pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_auth_url(*, firm_tenant_id: str, firm_client_id: str,
                   redirect_uri: str, state: str, code_verifier: str) -> str:
    code_challenge = _pkce_challenge(code_verifier)
    params = {
        "client_id": firm_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(GRAPH_SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"https://login.microsoftonline.com/{firm_tenant_id}/oauth2/v2.0/authorize?" + urlencode(params)


async def exchange_code(*, firm_tenant_id: str, firm_client_id: str,
                        firm_client_secret: str, code: str,
                        code_verifier: str, redirect_uri: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://login.microsoftonline.com/{firm_tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": firm_client_id,
                "client_secret": firm_client_secret,
                "scope": " ".join(GRAPH_SCOPES),
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
        resp.raise_for_status()
        return resp.json()
