"""
Xero OAuth 2.0 token management.

First run: opens a browser tab so you can log in to Xero.
After that: silently refreshes the token using the stored refresh_token.
Tokens are stored in the file set by XERO_TOKEN_FILE in your .env.
"""

import json
import logging
import secrets
import threading
import time
import webbrowser
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from config import (
    XERO_CLIENT_ID,
    XERO_CLIENT_SECRET,
    XERO_REDIRECT_URI,
    XERO_TENANT_ID,
    XERO_TOKEN_FILE,
)

logger = logging.getLogger(__name__)

_AUTH_URL = "https://login.xero.com/identity/connect/authorize"
_TOKEN_URL = "https://identity.xero.com/connect/token"
_CONNECTIONS_URL = "https://api.xero.com/connections"
_SCOPES = "openid profile email accounting.contacts.read offline_access"


# ---------------------------------------------------------------------------
# Local callback server (used only during first-time auth)
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    auth_code: str | None = None
    state_received: str | None = None

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        _CallbackHandler.auth_code = params.get("code", [None])[0]
        _CallbackHandler.state_received = params.get("state", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Xero authorisation complete. You can close this tab.")

    def log_message(self, *_):
        pass


def _start_callback_server(port: int) -> HTTPServer:
    server = HTTPServer(("localhost", port), _CallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    return server


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _basic_auth_header() -> str:
    return "Basic " + b64encode(f"{XERO_CLIENT_ID}:{XERO_CLIENT_SECRET}".encode()).decode()


def _post_token(data: dict) -> dict:
    resp = requests.post(
        _TOKEN_URL,
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=data,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _save_token(token_data: dict, tenant_id: str) -> None:
    token_data["expires_at"] = time.time() + token_data.get("expires_in", 1800) - 60
    token_data["tenant_id"] = tenant_id
    XERO_TOKEN_FILE.write_text(json.dumps(token_data, indent=2), encoding="utf-8")


def _load_token() -> dict | None:
    if XERO_TOKEN_FILE.exists():
        return json.loads(XERO_TOKEN_FILE.read_text(encoding="utf-8"))
    return None


def _resolve_tenant_id(access_token: str) -> str:
    if XERO_TENANT_ID:
        return XERO_TENANT_ID
    resp = requests.get(
        _CONNECTIONS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    connections = resp.json()
    if not connections:
        raise RuntimeError("No Xero organisations are connected to this app.")
    if len(connections) > 1:
        names = [c["tenantName"] for c in connections]
        logger.warning("Multiple Xero orgs found %s — using '%s'.", names, connections[0]["tenantName"])
    return connections[0]["tenantId"]


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def get_access_token() -> tuple[str, str]:
    """Return (access_token, tenant_id), refreshing silently or re-authorising as needed."""
    token = _load_token()

    # Valid token in hand
    if token and time.time() < token.get("expires_at", 0):
        return token["access_token"], token["tenant_id"]

    # Refresh using stored refresh_token
    if token and token.get("refresh_token"):
        logger.info("Refreshing Xero access token.")
        refreshed = _post_token({
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
        })
        tenant_id = token.get("tenant_id") or _resolve_tenant_id(refreshed["access_token"])
        _save_token(refreshed, tenant_id)
        return refreshed["access_token"], tenant_id

    # -----------------------------------------------------------------------
    # First-time auth: open browser, wait for callback
    # -----------------------------------------------------------------------
    logger.info("No stored token. Starting one-time Xero OAuth2 authorisation.")
    state = secrets.token_urlsafe(16)
    port = int(urlparse(XERO_REDIRECT_URI).port or 8080)
    _start_callback_server(port)

    auth_params = {
        "response_type": "code",
        "client_id": XERO_CLIENT_ID,
        "redirect_uri": XERO_REDIRECT_URI,
        "scope": _SCOPES,
        "state": state,
    }
    webbrowser.open(f"{_AUTH_URL}?{urlencode(auth_params)}")
    print("\nBrowser opened for Xero login. Waiting (up to 2 minutes)...")

    for _ in range(300):
        time.sleep(1)
        if _CallbackHandler.auth_code:
            break
    else:
        raise TimeoutError("Xero authorisation timed out after 5 minutes.")

    if _CallbackHandler.state_received != state:
        raise ValueError("OAuth2 state mismatch — possible CSRF attack.")

    token_data = _post_token({
        "grant_type": "authorization_code",
        "code": _CallbackHandler.auth_code,
        "redirect_uri": XERO_REDIRECT_URI,
    })
    tenant_id = _resolve_tenant_id(token_data["access_token"])
    _save_token(token_data, tenant_id)
    logger.info("Xero authorisation complete. Tenant ID saved.")
    return token_data["access_token"], tenant_id
