"""
Entry point — run once manually to authorise Xero, then via Task Scheduler daily.
"""

import logging
import sys

from config import LOG_FILE
from calendar_reader import get_next_day_appointments
from xero_api import disambiguate_by_name, find_contacts_by_email, find_contacts_by_name, find_contacts_by_phone
from xero_tax_browser import XeroTaxBrowser


def _setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> None:
    _setup_logging()
    log = logging.getLogger(__name__)
    log.info("=" * 60)
    log.info("Pre-fill run started")

    # 1. Read Outlook calendar
    appointments = get_next_day_appointments()
    if not appointments:
        log.info("No appointments tomorrow — nothing to do.")
        return

    # 2. Match each appointment to a Xero contact
    #    - Meeting invitations (recipients present): match by email
    #    - Personal appointments (no recipients): match by subject text
    seen_keys: set[str] = set()
    clients: list[dict] = []

    for appt in appointments:
        if appt["external_attendees"]:
            for attendee in appt["external_attendees"]:
                # Dedup key: email preferred, then phone, then display name
                dedup_key = attendee["email"] or attendee.get("phone") or attendee["name"]
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                contact = None

                # 1. Try email
                if attendee["email"]:
                    matches = find_contacts_by_email(attendee["email"])
                    if matches:
                        contact = matches[0] if len(matches) == 1 else disambiguate_by_name(matches, attendee["name"])
                        if contact:
                            log.info("Matched (email)  %s  ->  %s", attendee["email"], contact["Name"])

                # 2. Try phone
                if not contact and attendee.get("phone"):
                    matches = find_contacts_by_phone(attendee["phone"])
                    if matches:
                        contact = matches[0] if len(matches) == 1 else disambiguate_by_name(matches, attendee["name"])
                        if contact:
                            log.info("Matched (phone)  %s  ->  %s", attendee["phone"], contact["Name"])

                # 3. Try name
                if not contact and attendee["name"]:
                    matches = find_contacts_by_name(attendee["name"])
                    if matches:
                        contact = matches[0] if len(matches) == 1 else disambiguate_by_name(matches, attendee["name"])
                        if contact:
                            log.info("Matched (name)   %s  ->  %s", attendee["name"], contact["Name"])

                if not contact:
                    log.warning("No Xero contact found for %s — skipping.", dedup_key)
                    continue

                clients.append(contact)

        else:
            key = appt["subject"]
            if key in seen_keys:
                continue
            seen_keys.add(key)

            matches = find_contacts_by_name(key)
            if not matches:
                log.info("No Xero contact matched subject '%s' — skipping.", key)
                continue

            contact = matches[0] if len(matches) == 1 else disambiguate_by_name(matches, key)
            if not contact:
                log.warning("Could not disambiguate subject '%s' — skipping.", key)
                continue

            log.info("Matched (subject) '%s'  ->  %s", key, contact["Name"])
            clients.append(contact)

    if not clients:
        log.info("No Xero clients matched — nothing to process.")
        return

    # 3. Process each client in Xero Tax
    log.info("Processing %d client(s).", len(clients))
    ok = fail = 0

    with XeroTaxBrowser() as browser:
        for client in clients:
            if browser.process_client(client):
                ok += 1
            else:
                fail += 1

    log.info("Done — %d succeeded, %d failed.", ok, fail)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
