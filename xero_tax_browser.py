"""
Playwright automation for Xero Practice Manager (practicemanager.xero.com).

Session cookies are persisted in browser_session/ so you only need to log in
manually on the very first run. After that the script runs unattended.

NOTE ON SELECTORS
-----------------
XPM's DOM changes periodically. If a step fails, run the script with
headless=False (default), watch where it stops, and update the relevant
selector below. Each action block is isolated so only that step fails — the
script screenshots the page on every error to help diagnose.
"""

import logging
import re
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from config import BROWSER_USER_DATA_DIR, CURRENT_TAX_YEAR, HEADLESS

logger = logging.getLogger(__name__)

_XPM_CLIENTS_URL = "https://practicemanager.xero.com/app/clients"
_DEFAULT_TIMEOUT = 30_000   # ms
_LOGIN_TIMEOUT = 300_000    # ms — time allowed for manual first-run login (5 min)
_NETWORK_TIMEOUT = 90_000   # ms — ATO PLS calls can be slow


class XeroTaxBrowser:
    def __init__(self):
        self._pw = None
        self._ctx = None
        self._page: Page | None = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        BROWSER_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_USER_DATA_DIR),
            headless=HEADLESS,
            slow_mo=150,
        )
        self._page = self._ctx.new_page()
        self._page.set_default_timeout(_DEFAULT_TIMEOUT)
        return self

    def __exit__(self, *_):
        if self._ctx:
            self._ctx.close()
        if self._pw:
            self._pw.stop()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def process_client(self, contact: dict) -> bool:
        """
        Open or create the client's ITR in XPM, then pre-fill from ATO if needed.
        Returns True on success, False on any error (error screenshot saved).
        """
        name = contact.get("Name", "Unknown")
        logger.info("Processing: %s", name)
        try:
            self._go_to_client(name)
            self._open_tax_returns_tab()
            prefill_done = self._ensure_current_year_return(name)
            if not prefill_done:
                self._run_pls_prefill(name)
            logger.info("Pre-fill complete: %s", name)
            return True
        except Exception as exc:
            logger.error("Failed: %s — %s", name, exc)
            self._screenshot(f"error_{name}")
            return False

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _go_to_client(self, client_name: str) -> None:
        self._page.goto(_XPM_CLIENTS_URL, wait_until="load", timeout=60_000)
        self._ensure_on_clients_page()

        # Click to focus, then type to trigger XPM's live search filter
        search = self._page.get_by_placeholder("Search clients")
        search.click()
        search.press_sequentially(client_name, delay=60)
        self._page.wait_for_timeout(2_000)

        # Client names are links in the Name column
        try:
            self._page.get_by_role("link", name=client_name).first.click(timeout=10_000)
        except PWTimeout:
            self._page.get_by_text(client_name).first.click()

        self._page.wait_for_load_state("load", timeout=60_000)

    def _ensure_on_clients_page(self) -> None:
        """
        Confirm the XPM clients page is ready by waiting for the search box.
        XPM is a SPA — the session can expire and trigger a JS redirect to
        login.xero.com *after* the initial page load, so checking the URL
        at goto time is not reliable. We check for the element instead.
        """
        try:
            self._page.get_by_placeholder("Search clients").wait_for(timeout=8_000)
            return
        except PWTimeout:
            pass

        # Redirected to login
        if HEADLESS:
            raise RuntimeError(
                "Xero browser session has expired. RDP to the server and run "
                "refresh_session.py to re-authenticate."
            )

        logger.warning(
            "Xero browser login required — please log in manually in the browser window. "
            "Waiting up to 5 minutes."
        )
        self._page.wait_for_url("https://practicemanager.xero.com/**", timeout=_LOGIN_TIMEOUT)
        # After login, XPM lands on the dashboard — navigate back to clients
        self._page.goto(_XPM_CLIENTS_URL, wait_until="load", timeout=60_000)
        self._page.get_by_placeholder("Search clients").wait_for(timeout=15_000)
        logger.info("Login complete — on clients page.")

    def _open_tax_returns_tab(self) -> None:
        """
        Click the Tax Returns tab on the client detail page.
        Tries multiple strategies to handle both the old ExtJS and new React XPM UI.
        """
        # Strategy 1: link whose href contains TaxReturn (old UI: /TaxReturns, new: /tax-returns)
        try:
            self._page.locator('a[href*="axReturn"]').first.click(timeout=8_000)
            self._page.wait_for_load_state("load", timeout=60_000)
            return
        except PWTimeout:
            pass

        # Strategy 2: visible tab/link labelled "Tax Returns"
        try:
            self._page.get_by_role("link", name="Tax Returns").first.click(timeout=8_000)
            self._page.wait_for_load_state("load", timeout=60_000)
            return
        except PWTimeout:
            pass

        logger.warning("Tax Returns tab not found — proceeding from current page.")

    # ------------------------------------------------------------------
    # Return management
    # ------------------------------------------------------------------

    def _ensure_current_year_return(self, client_name: str) -> bool:
        """
        Open or create the current year ITR.
        Returns True if pre-fill was already performed during creation (PLS checkbox),
        False if the return already existed and pre-fill still needs to be triggered.
        """
        year = str(CURRENT_TAX_YEAR)

        # Check if the current year return already exists in the XPM list
        try:
            year_cell = self._page.locator("td").filter(
                has_text=re.compile(rf"^\s*{year}\s*$")
            ).first
            year_cell.wait_for(timeout=6_000)
            logger.info("%s: %s return exists — already pre-filled.", client_name, year)
            return True  # existing return assumed already pre-filled
        except PWTimeout:
            pass

        # No return found — create one via + Tax > Return (PLS checkbox ticked by default)
        logger.info("%s: no %s return — creating via + Tax.", client_name, year)
        self._create_return_via_tax_button(client_name, year)

        # If creation left us in XPM (not Xero Tax), navigate to the new return
        if "practicemanager.xero.com" in self._page.url:
            self._go_to_client(client_name)
            self._open_tax_returns_tab()
            try:
                year_cell = self._page.locator("td").filter(
                    has_text=re.compile(rf"^\s*{year}\s*$")
                ).first
                year_cell.wait_for(timeout=10_000)
                year_cell.locator("xpath=ancestor::tr").get_by_role("link").first.click()
                self._page.wait_for_load_state("load", timeout=60_000)
            except PWTimeout:
                raise RuntimeError(f"Could not open {year} return after creating it")

        return True  # pre-fill already done via PLS checkbox during creation

    def _create_return_via_tax_button(self, client_name: str, year: str) -> None:
        """Create a new ITR via the + Tax > Return workflow in XPM."""
        self._screenshot("debug_before_plus_tax")

        # 1. Click the + Tax button
        for attempt in (
            lambda: self._page.get_by_role("link", name="+ Tax").click(timeout=6_000),
            lambda: self._page.get_by_text("+ Tax").first.click(timeout=6_000),
            lambda: self._page.locator("a.x-btn-skin", has_text="Tax").first.click(timeout=6_000),
            lambda: self._page.locator("a", has_text="+ Tax").first.click(timeout=6_000),
        ):
            try:
                attempt()
                break
            except PWTimeout:
                continue
        else:
            raise RuntimeError("Could not find the '+ Tax' button")

        self._page.wait_for_timeout(600)
        self._screenshot("debug_tax_submenu")

        # 2. Click "Return" from the submenu
        for attempt in (
            lambda: self._page.get_by_role("menuitem", name="Return").click(timeout=4_000),
            lambda: self._page.locator("[role='menuitem']", has_text="Return").first.click(timeout=4_000),
            lambda: self._page.get_by_text("Return", exact=True).first.click(timeout=4_000),
        ):
            try:
                attempt()
                break
            except PWTimeout:
                continue
        else:
            raise RuntimeError("Could not find 'Return' in the + Tax submenu")

        self._page.wait_for_timeout(800)
        self._screenshot("debug_create_return_form")

        # 3. Fill the form — selects appear in this order: Job(0), Related Client(1),
        #    Tax Type(2), Tax Year(3), Rollover from(4)
        selects = self._page.locator("select")

        # Related Client
        selects.nth(1).select_option(label=client_name, timeout=6_000)
        self._page.wait_for_timeout(300)

        # Tax Type = ITR
        selects.nth(2).select_option(label="ITR", timeout=6_000)
        self._page.wait_for_timeout(500)  # Year options may load dynamically after type is set

        # Tax Year
        selects.nth(3).select_option(label=year, timeout=6_000)

        self._screenshot("debug_create_return_filled")

        # 4. Submit
        for btn_label in ("Create", "Save", "Add", "OK"):
            try:
                self._page.get_by_role("button", name=btn_label).first.click(timeout=4_000)
                break
            except PWTimeout:
                continue

        self._page.wait_for_load_state("load", timeout=_NETWORK_TIMEOUT)
        logger.info("%s: %s ITR created.", client_name, year)

    def _open_three_dot_menu(self) -> None:
        """Click the ⋮ / More options button in the Xero Tax return editor."""
        for attempt in (
            lambda: self._page.get_by_role("button", name="More options").click(timeout=4_000),
            lambda: self._page.locator('[aria-label="More options"]').click(timeout=4_000),
            lambda: self._page.locator('[aria-label*="More"]').first.click(timeout=4_000),
            lambda: self._page.locator("button").filter(has_text=re.compile(r"^[⋮•]+$")).first.click(timeout=4_000),
            # Xero Tax return editor: the ⋮ button is the last button in the top toolbar
            lambda: self._page.locator('[aria-label="Menu"]').first.click(timeout=4_000),
            lambda: self._page.locator('[aria-label*="option"]').first.click(timeout=4_000),
            lambda: self._page.locator("button").filter(has_text=re.compile(r"^\s*\.\.\.\s*$")).first.click(timeout=4_000),
            lambda: self._page.locator("button[data-testid*='more'], button[data-testid*='menu'], button[data-testid*='option']").first.click(timeout=4_000),
            # Last-resort: the rightmost button in the page header
            lambda: self._page.locator("header button, [role='banner'] button").last.click(timeout=4_000),
        ):
            try:
                attempt()
                return
            except PWTimeout:
                continue
        raise RuntimeError("Could not find the three-dot menu button")

    # ------------------------------------------------------------------
    # PLS pre-fill
    # ------------------------------------------------------------------

    def _run_pls_prefill(self, client_name: str) -> None:
        if not self._click_prefill_button():
            raise RuntimeError(f"Pre-fill button not found for {client_name}")
        self._page.wait_for_load_state("networkidle", timeout=_NETWORK_TIMEOUT)
        self._handle_managed_fund_prompt()

    def _click_prefill_button(self) -> bool:
        # Xero Tax is a SPA — wait for the page to settle before inspecting buttons
        try:
            self._page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass
        self._screenshot("debug_before_prefill")

        # Try direct top-level button first
        for label in ("Pre-fill from ATO", "Pre-fill", "Get pre-fill data", "ATO pre-fill", "Pre-fill return"):
            try:
                self._page.get_by_role("button", name=label).first.click(timeout=4_000)
                return True
            except PWTimeout:
                continue

        # Pre-fill is typically in the ⋮ three-dot menu in the Xero Tax return editor
        try:
            self._open_three_dot_menu()
            self._page.wait_for_timeout(500)
            for label in ("Pre-fill from ATO", "Pre-fill", "Get pre-fill data", "ATO pre-fill", "Pre-fill return"):
                try:
                    self._page.get_by_text(label).first.click(timeout=4_000)
                    return True
                except PWTimeout:
                    continue
            # Capture what the open menu actually contains to diagnose mismatched labels
            self._screenshot("debug_menu_open")
        except RuntimeError:
            pass

        return False

    def _handle_managed_fund_prompt(self) -> None:
        try:
            self._page.get_by_text(
                "Remove 0s from managed fund distributions", exact=False
            ).wait_for(timeout=10_000)
            self._page.get_by_role("button", name="Yes").click()
            self._page.wait_for_load_state("networkidle", timeout=_DEFAULT_TIMEOUT)
            logger.info("Managed fund prompt: answered Yes.")
        except PWTimeout:
            logger.debug("No managed fund distribution prompt.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _screenshot(self, label: str) -> None:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        path = Path(f"{safe}.png")
        try:
            self._page.screenshot(path=str(path))
            logger.info("Screenshot saved: %s", path)
        except Exception:
            pass
