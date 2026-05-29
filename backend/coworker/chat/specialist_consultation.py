"""Specialist consultation: load active prompt, stream the specialist's answer.

Invoked by the chat orchestrator when Claude (Sonnet) emits a
``consult_specialist`` tool_use block. Loads the specialist row, its
currently active prompt version, and opens an Opus stream against
that prompt. Yields a typed event stream the orchestrator forwards
to the SSE client and also uses to persist the ``tool_result`` step
on the parent ``agent_traces`` row (via ``AgentTraceWriter``).

Persistence is intentionally NOT done here: this function is a pure
streaming producer of consultation events. The orchestrator owns
the trace lifecycle and writes the ``tool_call`` / ``tool_result``
step pair from the events emitted below.
"""
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.anthropic_client import (
    AnthropicClient,
    CompletionMessage,
    StreamCompletion,
    StreamTextDelta,
)
from coworker.connectors.exceptions import ConnectorError
from coworker.db.models.specialist import Specialist, SpecialistPromptVersion

_CONSULTATION_MAX_TOKENS = 4096


@dataclass(frozen=True)
class ConsultationStarted:
    specialist_name: str
    display_name: str
    prompt_version_id: uuid.UUID
    model: str


@dataclass(frozen=True)
class ConsultationTextDelta:
    specialist_name: str
    text: str


@dataclass(frozen=True)
class ConsultationComplete:
    specialist_name: str
    full_text: str
    input_tokens: int
    output_tokens: int
    model: str
    prompt_version_id: uuid.UUID


@dataclass(frozen=True)
class ConsultationError:
    """Emitted instead of ``ConsultationComplete`` when the consultation
    cannot run to completion. ``prompt_version_id`` is None only when
    the specialist itself was missing (so no version was ever loaded);
    on streaming errors the version was loaded successfully and the
    id is recorded for the audit step.
    """

    specialist_name: str
    error: str
    prompt_version_id: uuid.UUID | None
    model: str | None
    partial_text: str


ConsultationEvent = (
    ConsultationStarted
    | ConsultationTextDelta
    | ConsultationComplete
    | ConsultationError
)


async def consult_specialist(
    session: AsyncSession,
    client: AnthropicClient,
    *,
    specialist_name: str,
    question: str,
) -> AsyncIterator[ConsultationEvent]:
    """Stream one specialist consultation.

    Caller must already be inside ``firm_context(firm_id)`` so the
    specialist lookup is RLS-scoped to the current firm. The question
    text arrives in real-PII space (the chat orchestrator restored
    placeholders before forwarding); the specialist's own
    ``AnthropicClient`` will scrub on its way out and restore on its
    way back in the assembled ``StreamCompletion.full_text``.

    Events ordering:

    - On unknown specialist or no active version: a single
      ``ConsultationError`` (no Started, no deltas).
    - On normal flow: one ``ConsultationStarted``, zero or more
      ``ConsultationTextDelta``, then either ``ConsultationComplete``
      (assembled full_text + tokens) or ``ConsultationError`` (with
      ``partial_text`` capturing anything that did arrive before the
      error).
    """
    spec = (
        await session.execute(
            select(Specialist).where(Specialist.name == specialist_name)
        )
    ).scalar_one_or_none()
    if spec is None or spec.active_version_id is None:
        yield ConsultationError(
            specialist_name=specialist_name,
            error=(
                f"Specialist '{specialist_name}' is not registered for this "
                "firm or has no active prompt version."
            ),
            prompt_version_id=None,
            model=None,
            partial_text="",
        )
        return

    version = (
        await session.execute(
            select(SpecialistPromptVersion).where(
                SpecialistPromptVersion.id == spec.active_version_id
            )
        )
    ).scalar_one()

    yield ConsultationStarted(
        specialist_name=spec.name,
        display_name=spec.display_name,
        prompt_version_id=version.id,
        model=spec.model,
    )

    assembled: list[str] = []
    full_text: str | None = None
    input_tokens = 0
    output_tokens = 0

    try:
        async for event in client.stream_message(
            [CompletionMessage(role="user", content=question)],
            model=spec.model,
            max_tokens=_CONSULTATION_MAX_TOKENS,
            system=version.prompt_text,
        ):
            if isinstance(event, StreamTextDelta):
                assembled.append(event.text)
                yield ConsultationTextDelta(
                    specialist_name=spec.name, text=event.text
                )
            elif isinstance(event, StreamCompletion):
                full_text = event.full_text
                input_tokens = event.input_tokens
                output_tokens = event.output_tokens
    except (ConnectorError, Exception) as exc:
        yield ConsultationError(
            specialist_name=spec.name,
            error=f"{type(exc).__name__}: {exc}",
            prompt_version_id=version.id,
            model=spec.model,
            partial_text="".join(assembled),
        )
        return

    yield ConsultationComplete(
        specialist_name=spec.name,
        full_text=full_text if full_text is not None else "".join(assembled),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=spec.model,
        prompt_version_id=version.id,
    )
