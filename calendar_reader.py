"""
Calendar reader using Microsoft Graph API.
Requires an Azure AD app registration with:
  - Calendars.Read  (application permission)
  - Contacts.Read   (application permission — for phone fallback)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import requests
from msal import ConfidentialClientApplication

from config import (
    AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID,
    CALENDAR_USER_EMAIL, INTERNAL_DOMAIN,
)

logger = logging.getLogger(__name__)

_GRAPH_URL = "https://graph.microsoft.com/v1.0"
_SCOPES    = ["https://graph.microsoft.com/.default"]

_msal_app: Optional[ConfidentialClientApplication] = None


def _get_msal_app() -> ConfidentialClientApplication:
    global _msal_app
    if _msal_app is None:
        _msal_app = ConfidentialClientApplication(
            client_id=AZURE_CLIENT_ID,
            client_credential=AZURE_CLIENT_SECRET,
            authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
        )
    return _msal_app


def _graph_headers() -> dict:
    result = _get_msal_app().acquire_token_silent(_SCOPES, account=None)
    if not result:
        result = _get_msal_app().acquire_token_for_client(scopes=_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(f"Graph API auth failed: {result.get('error_description', result)}")
    return {"Authorization": f"Bearer {result['access_token']}"}


def _resolve_phone_from_contacts(email: str) -> Optional[str]:
    """Look up phone number for an attendee in the calendar owner's Outlook contacts."""
    try:
        resp = requests.get(
            f"{_GRAPH_URL}/users/{CALENDAR_USER_EMAIL}/contacts",
            headers=_graph_headers(),
            params={
                "$filter": f"emailAddresses/any(e:e/address eq '{email}')",
                "$select": "mobilePhone,businessPhones",
                "$top": "1",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            contacts = resp.json().get("value", [])
            if contacts:
                mobile = contacts[0].get("mobilePhone")
                if mobile and mobile.strip():
                    return mobile.strip()
                phones = contacts[0].get("businessPhones") or []
                if phones:
                    return phones[0].strip()
    except Exception as exc:
        logger.debug("Could not resolve phone from contacts for %s: %s", email, exc)
    return None


def get_next_day_appointments() -> list[dict]:
    """Return tomorrow's calendar items with their attendees."""
    tomorrow = datetime.now().date() + timedelta(days=1)
    start = f"{tomorrow}T00:00:00"
    end   = f"{tomorrow}T23:59:59"

    try:
        resp = requests.get(
            f"{_GRAPH_URL}/users/{CALENDAR_USER_EMAIL}/calendarView",
            headers=_graph_headers(),
            params={
                "startDateTime": start,
                "endDateTime":   end,
                "$select":       "subject,start,end,attendees",
                "$top":          "50",
            },
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json().get("value", [])
    except Exception as exc:
        logger.error("Graph API calendar error: %s", exc)
        return []

    appointments = []
    for event in events:
        subject = (event.get("subject") or "").strip()
        if not subject:
            continue

        external = []
        for attendee in event.get("attendees", []):
            addr  = attendee.get("emailAddress", {})
            email = (addr.get("address") or "").lower().strip()
            name  = (addr.get("name") or "").strip()

            # Skip internal attendees
            if email and email.endswith(f"@{INTERNAL_DOMAIN}"):
                continue

            phone = _resolve_phone_from_contacts(email) if email else None
            external.append({"email": email or None, "phone": phone, "name": name})

        appointments.append({
            "subject": subject,
            "start":   event.get("start", {}).get("dateTime", ""),
            "external_attendees": external,
        })

    logger.info("Tomorrow (%s): %d appointment(s) found.", tomorrow, len(appointments))
    return appointments
