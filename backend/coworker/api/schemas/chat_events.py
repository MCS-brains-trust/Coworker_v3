"""SSE event protocol for POST /api/v1/conversations/{id}/messages.

These models are documentation only: SSE responses bypass FastAPI's
``response_model`` validation, so the actual frames are produced as
hand-built strings by the chat orchestrator. The shapes below are the
contract a frontend client should code against.

Event sequence within one turn (success path):

    token (source=orchestrator)*
    [ specialist_consultation_started
      token (source=specialist:<slug>)*
      ( specialist_consultation_complete | specialist_consultation_error )
    ]*
    token (source=orchestrator)*
    done

On failure, ``error`` replaces ``done``.
"""
from pydantic import BaseModel


class TokenEvent(BaseModel):
    """One incremental text fragment.

    ``source`` is either ``"orchestrator"`` for Sonnet's framing /
    synthesis text, or ``"specialist:<slug>"`` (e.g.
    ``"specialist:gst"``) for tokens streamed from a specialist
    consultation. Text is in PII placeholder space; clients that
    need fully-restored content should refresh from
    ``GET /api/v1/conversations/{id}/messages`` once ``done``
    arrives.
    """

    text: str
    source: str


class SpecialistConsultationStartedEvent(BaseModel):
    """A specialist consultation has begun.

    ``step_index`` is the index of the ``tool_result`` step that
    will record this consultation in the parent ``agent_traces`` row.
    Stable: the started and complete / error events for the same
    consultation share the same ``step_index``.
    """

    specialist_name: str
    display_name: str
    prompt_version_id: str
    model: str
    step_index: int


class SpecialistConsultationCompleteEvent(BaseModel):
    specialist_name: str
    input_tokens: int
    output_tokens: int
    step_index: int


class SpecialistConsultationErrorEvent(BaseModel):
    specialist_name: str
    error: str
    step_index: int


class DoneEvent(BaseModel):
    """Terminal success event. Totals roll up the orchestrator's
    Sonnet calls plus every specialist consultation in this turn."""

    message_id: str
    trace_id: str
    total_input_tokens: int
    total_output_tokens: int


class ErrorEvent(BaseModel):
    """Terminal failure event. Replaces ``done`` when the turn
    aborts. Whatever partial text already streamed is still
    persisted on the assistant ``chat_messages`` row."""

    error: str
