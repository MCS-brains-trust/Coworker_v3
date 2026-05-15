"""Builtin plugin catalogue.

``register_builtin_plugins(registry)`` populates a PluginRegistry
with the plugins that every firm gets the option to install.
The Phase 13 onboarding wizard offers each of these to new firms
with appropriate defaults.

What's here vs deferred to later phases:

- ``smart_responder`` (email_received): the canonical hot-path
  plugin. Ships in 6-3.
- ``meeting_prep`` (scheduled + manual): daily pre-meeting
  briefs. Ships in 12-2.

Coming as separate sub-phases of Phase 6:

- correspondence_logger (email_received): writes every email +
  proposed entities to client_interactions + entities.
- noa_processor (email_received): extracts ATO Notice of
  Assessment data from attachments via the Phase 7 vision
  pipeline.
- asic_handler (email_received): processes ASIC company-extract
  notifications.
- bas_reminder (scheduled): proactive client reminders for BAS
  due dates.
- debtor_followup (scheduled): weekly aged-debtor follow-ups.
- fusesign_monitor (fusesign_event): track envelope state changes.
- engagement_letter (manual + two-person approval): generates
  the firm's standard engagement letter.
- client_outreach (scheduled): periodic touchpoint emails to
  clients who haven't been contacted recently.
- annual_review (scheduled): client anniversary outreach.
- morning_briefing (scheduled 06:30): principal's daily briefing.
- nightly_reflection (scheduled 02:00): internal — turns
  yesterday's approval edits into lessons.
- proactive_intelligence (scheduled Monday 07:00): weekly briefing
  with cross-client trends.
"""
from coworker.plugins.base import PluginRegistry
from coworker.plugins.builtin.delivery_status_handler import (
    DeliveryStatusHandlerPlugin,
)
from coworker.plugins.builtin.meeting_prep import MeetingPrepPlugin
from coworker.plugins.builtin.smart_responder import SmartResponderPlugin


def register_builtin_plugins(registry: PluginRegistry) -> None:
    """Populate ``registry`` with every builtin plugin."""
    registry.register(SmartResponderPlugin)
    registry.register(MeetingPrepPlugin)
    registry.register(DeliveryStatusHandlerPlugin)


__all__ = [
    "DeliveryStatusHandlerPlugin",
    "MeetingPrepPlugin",
    "SmartResponderPlugin",
    "register_builtin_plugins",
]
