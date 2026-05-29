"""
Run this locally (on your Windows machine) when the Xero browser session has expired.

A browser window will open — log in to Xero Practice Manager with MFA as normal.
Once you are on the Clients page, press Enter to save the session and exit.

Then copy the saved session to the droplet:
    scp -r browser_session/ user@your-droplet-ip:/path/to/prefill_returns/

Future scheduled runs on the droplet will use the refreshed session.
"""

from playwright.sync_api import sync_playwright
from config import BROWSER_USER_DATA_DIR

_XPM_CLIENTS_URL = "https://practicemanager.xero.com/app/clients"

print("Opening browser — log in to Xero Practice Manager.")
print("Once you are on the Clients page, press Enter to save and exit.\n")

with sync_playwright() as pw:
    BROWSER_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_USER_DATA_DIR),
        headless=False,
    )
    page = ctx.new_page()
    page.goto(_XPM_CLIENTS_URL)
    input("Press Enter once logged in and on the Clients page...")
    ctx.close()

print("Session saved. The scheduled job will now run unattended.")
