import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

XERO_CLIENT_ID = os.environ["XERO_CLIENT_ID"]
XERO_CLIENT_SECRET = os.environ["XERO_CLIENT_SECRET"]
XERO_REDIRECT_URI = os.getenv("XERO_REDIRECT_URI", "http://localhost:8080/callback")
XERO_TOKEN_FILE = BASE_DIR / os.getenv("XERO_TOKEN_FILE", "xero_token.json")
XERO_TENANT_ID = os.getenv("XERO_TENANT_ID", "")

# Microsoft Graph API — Azure AD app credentials for calendar access
AZURE_CLIENT_ID     = os.environ["AZURE_CLIENT_ID"]
AZURE_CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]
AZURE_TENANT_ID     = os.environ["AZURE_TENANT_ID"]
CALENDAR_USER_EMAIL = os.environ["CALENDAR_USER_EMAIL"]

INTERNAL_DOMAIN = os.getenv("INTERNAL_EMAIL_DOMAIN", "mcands.com.au").lower()

CURRENT_TAX_YEAR = int(os.getenv("CURRENT_TAX_YEAR", "2025"))
PRIOR_TAX_YEAR = CURRENT_TAX_YEAR - 1

BROWSER_USER_DATA_DIR = BASE_DIR / "browser_session"
LOG_FILE = BASE_DIR / os.getenv("LOG_FILE", "prefill_returns.log")

# Set HEADLESS=false in .env when running interactively (e.g. refresh_session.py)
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
