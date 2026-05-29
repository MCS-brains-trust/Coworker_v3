"""
Xero Contacts API — email / phone / name matching.

If your clients are stored in Xero Practice Manager (XPM) rather than the
standard Contacts list, the endpoint and scope will need to change to:
  GET https://api.xero.com/practicemgr/1.0/client?Email=...
  scope: practice.manager.read
"""

import logging
import re

import requests

from xero_auth import get_access_token

logger = logging.getLogger(__name__)

_CONTACTS_URL = "https://api.xero.com/api.xro/2.0/Contacts"


def _auth_headers() -> dict:
    access_token, tenant_id = get_access_token()
    return {
        "Authorization": f"Bearer {access_token}",
        "Xero-Tenant-Id": tenant_id,
        "Accept": "application/json",
    }


def find_contacts_by_name(search_term: str) -> list[dict]:
    """Return Xero contacts whose name broadly matches search_term."""
    try:
        resp = requests.get(
            _CONTACTS_URL,
            headers=_auth_headers(),
            params={"searchTerm": search_term, "summaryOnly": "true"},
            timeout=15,
        )
        resp.raise_for_status()
        contacts = resp.json().get("Contacts", [])
        logger.info("Xero: %d contact(s) found for search '%s'", len(contacts), search_term)
        return contacts
    except Exception as exc:
        logger.error("Xero API error searching '%s': %s", search_term, exc)
        return []


def find_contacts_by_email(email: str) -> list[dict]:
    """Return all Xero contacts whose EmailAddress matches exactly."""
    try:
        resp = requests.get(
            _CONTACTS_URL,
            headers=_auth_headers(),
            params={"EmailAddress": email, "summaryOnly": "true"},
            timeout=15,
        )
        resp.raise_for_status()
        contacts = resp.json().get("Contacts", [])
        logger.info("Xero: %d contact(s) found for %s", len(contacts), email)
        return contacts
    except Exception as exc:
        logger.error("Xero API error for %s: %s", email, exc)
        return []


def find_contacts_by_phone(phone: str) -> list[dict]:
    """Return Xero contacts whose phone number matches (tries several normalised forms)."""
    digits = re.sub(r"\D", "", phone)
    # Normalise Australian numbers: +61 4xx → 04xx, 614xx → 04xx
    if digits.startswith("61") and len(digits) == 11:
        digits = "0" + digits[2:]

    candidates = {phone.strip(), re.sub(r"\s+", "", phone), digits}

    for number in candidates:
        if not number:
            continue
        try:
            resp = requests.get(
                _CONTACTS_URL,
                headers=_auth_headers(),
                params={"where": f'Phones.PhoneNumber=="{number}"', "summaryOnly": "true"},
                timeout=15,
            )
            resp.raise_for_status()
            contacts = resp.json().get("Contacts", [])
            if contacts:
                logger.info("Xero: %d contact(s) found for phone %s", len(contacts), number)
                return contacts
        except Exception as exc:
            logger.error("Xero API error searching phone '%s': %s", number, exc)

    logger.info("Xero: 0 contact(s) found for phone %s", phone)
    return []


def disambiguate_by_name(contacts: list[dict], display_name: str) -> dict | None:
    """
    When multiple contacts share an email, score each against the attendee's
    display name and return the best match (or None if no words match at all).
    """
    name_lower = display_name.lower()
    scored = [
        (sum(1 for w in name_lower.split() if w in c.get("Name", "").lower()), c)
        for c in contacts
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]

    if best_score == 0:
        logger.warning(
            "Multiple contacts share email but none name-match '%s' — skipping.", display_name
        )
        return None

    logger.info("Disambiguated '%s' → '%s' (score %d)", display_name, best["Name"], best_score)
    return best
