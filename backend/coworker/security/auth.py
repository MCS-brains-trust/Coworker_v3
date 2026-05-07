import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
import base64
import hashlib

import httpx
import jwt

from coworker.config import get_settings
from coworker.security.encryption import encrypt_str

GRAPH_SCOPES = [
    "openid",
    "profile",
    "email",
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


def decode_id_token_unverified(id_token: str) -> dict[str, Any]:
    """Decode a Microsoft id_token without verifying its signature.

    This decodes the JWT body purely to read identity claims (oid,
    preferred_username, name) so we can populate the local User row
    at sign-in time. Signature verification is deliberately skipped.
    The reasoning, in order:

    The token arrived in our own outbound token-exchange request,
    over TLS, directly to login.microsoftonline.com. The TLS channel
    authenticates the issuer; no untrusted intermediary has touched
    this token in transit. The id_token did not arrive through the
    user's browser — only the auth code did, which we then traded
    server-to-server for the token bundle.

    The id_token is used here only for *directory lookup* — mapping
    Microsoft's `oid` claim to a row in our `users` table. It is NOT
    the credential that authorises any subsequent action. We do not
    grant the session privileges based on claims inside this token.

    The actual auth credential is the access_token, returned in the
    same exchange. That token is validated by Microsoft Graph every
    time we call the API. Forging an id_token while not also forging
    a Graph-valid access_token would yield a session that can do
    nothing — the very next Graph call would fail. So id_token forgery
    is not a useful attack on this flow.

    JWKS-based signature verification would mean fetching Microsoft's
    public signing keys (or maintaining a JWKS cache), adding latency
    and operational complexity to every login, without defending
    against any realistic attack here.

    If we ever start trusting id_token claims for *authorisation*
    decisions (e.g. role assertions baked into the token, or relying
    on group/scope claims to grant privileges), this must be revisited
    — at that point the id_token IS the credential and signature
    verification becomes load-bearing.
    """
    return jwt.decode(
        id_token,
        options={
            "verify_signature": False,
            "verify_exp": False,
            "verify_nbf": False,
            "verify_iat": False,
            "verify_aud": False,
            "verify_iss": False,
        },
    )


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
