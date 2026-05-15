"""Inbound prompt-injection sanitiser (pre-pilot Task 2).

Every string that originates outside CoWorker's trust boundary
(an inbound email body, an attendee name, a KG entity that was
extracted from a client email, a memory row whose content came
from an inbound email) passes through ``sanitise_retrieved_string``
before being concatenated into a Claude prompt.

The function does three things, in order:

1. **Strip C0/C1 control characters.** A few common patterns
   embed `\\x07` bells or zero-width controls to disguise
   instruction text from human reviewers.
2. **Flag instruction-like patterns.** These aren't removed —
   the model still sees the original (now-wrapped) text and
   reasons about it as data per the engine's universal system
   prompt rule. We just record what the sanitiser saw so the
   trace surfaces the suspicion.
3. **Truncate to ``max_length``** so a 50KB hostile email
   body doesn't dominate the context window. Truncation adds
   an explicit ``[…truncated…]`` marker; the warnings list
   includes "truncated" so the principal knows content was
   dropped.

Returns ``(cleaned_value, warnings)``. The caller is expected
to wrap the cleaned value in ``<user_data>...</user_data>``
tags before placing it into a prompt — this is by convention
not by mechanism, so the sanitise call site stays composable
with whatever prompt-assembly template the caller uses.

The orchestrator's engine prepends a universal rule to every
plugin's system prompt that teaches the model to treat
``<user_data>``-wrapped content as data, never instructions.
That + the per-string sanitisation is the defence-in-depth
posture.
"""
import re
from collections.abc import Sequence

# C0 controls (0x00-0x1F) except tab (0x09), newline (0x0A),
# carriage return (0x0D); plus C1 controls (0x7F-0x9F).
# Stripping bells, zero-width markers, etc.
_CONTROL_CHARS_PATTERN = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"
)

# Patterns we flag — not exhaustive, deliberately. The system-
# prompt-side rule does the actual heavy lifting; this list
# catches obvious-injection text so the trace shows what tripped.
#
# Tuples of (label, compiled regex). The label appears verbatim
# in the warnings list so dashboards and the principal-side UI
# can group / count by category.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_previous_instructions",
        re.compile(
            # Accepts: "ignore previous instructions", "disregard
            # your above directions", "forget the prior rules",
            # "disregard all earlier prompts" — any combo of zero
            # or more qualifier words (all/your/the/any) between
            # the verb and the temporal anchor.
            r"\b(ignore|disregard|forget)"
            r"(?:\s+(?:all|your|the|any))*"
            r"\s+(previous|above|prior|earlier)"
            r"\s+(instructions?|directions?|rules?|prompts?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "new_instructions",
        re.compile(
            r"\b(new|updated|revised)\s+instructions?\s*[:.]",
            re.IGNORECASE,
        ),
    ),
    (
        "system_role_impersonation",
        re.compile(
            # Accepts: "you are now admin", "act as a developer",
            # "pretend to be the system", "roleplay as an
            # assistant". Optional article (a/an/the).
            r"\b(you\s+are\s+now|act\s+as|pretend\s+to\s+be|roleplay\s+as)"
            r"\s+(?:an?\s+|the\s+)?"
            r"(?:admin|root|system|developer|assistant)",
            re.IGNORECASE,
        ),
    ),
    (
        "exfiltration_request",
        re.compile(
            # Accepts multiple stacked qualifiers ("send all the
            # API keys") by repeating the optional qualifier group.
            r"\b(send|email|forward|post|upload|reveal|expose|dump)"
            r"(?:\s+(?:all|the|your|every))*"
            r"\s+(passwords?|secrets?|credentials?|tokens?"
            r"|api\s*keys?|private\s+(?:keys?|data)"
            r"|client\s+(?:data|list|records?))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "tool_command_injection",
        re.compile(
            r"<\s*tool[_\s-]?(?:call|use|name)\s*>|"
            r"\b(?:invoke|call|execute|run)\s+(?:the\s+)?tool\s+(?:named\s+)?[\"'`]?[a-z_]{3,}",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_disclosure",
        re.compile(
            r"\b(?:reveal|show|print|output|display|repeat)\s+(?:your\s+|the\s+)?(?:system\s+)?(?:prompt|instructions?|rules?|initial\s+message)\b",
            re.IGNORECASE,
        ),
    ),
)

_TRUNCATION_MARKER = "\n[…truncated…]"


def sanitise_retrieved_string(
    value: str, max_length: int = 2000,
) -> tuple[str, list[str]]:
    """Clean and flag one externally-sourced string.

    Args:
        value: the string to sanitise. ``None`` and non-str
            inputs are the caller's problem — pass an empty
            string if you want a no-op.
        max_length: truncate above this character count. The
            default 2000 is chosen against typical inbox preview
            sizes; a full body (``email_get_message``) can use
            something larger like 8000 — pass explicitly.

    Returns:
        ``(cleaned, warnings)`` where ``cleaned`` is the
        control-stripped, truncated text and ``warnings`` is
        a list of human-readable labels for patterns matched,
        plus the literal ``"truncated"`` if truncation
        happened. Empty list when nothing tripped.

    Per the engine's universal rule, the caller is expected to
    wrap ``cleaned`` in ``<user_data>``/``</user_data>`` tags
    before placing it into a prompt. The wrapping is the
    caller's responsibility because composition patterns vary
    (XML-ish goal text vs. JSON tool output vs. inline
    interpolation).
    """
    if not isinstance(value, str):
        raise TypeError(
            f"sanitise_retrieved_string expected str, got "
            f"{type(value).__name__}; pass str(value) explicitly "
            "if the conversion is intended"
        )
    if max_length < 1:
        raise ValueError("max_length must be >= 1")

    warnings: list[str] = []

    # Step 1: strip control characters. Stripping removes the
    # bytes outright; we don't replace with placeholder spaces
    # because the visual layout of an instruction-laden string
    # is part of the attack surface (e.g. a zero-width joiner
    # between "ig" and "nore" disguises "ignore" from a regex
    # without disguising it from the model).
    cleaned = _CONTROL_CHARS_PATTERN.sub("", value)
    if cleaned != value:
        warnings.append("control_chars_stripped")

    # Step 2: pattern flagging on the cleaned text (so an
    # attacker can't hide patterns BEHIND control chars).
    for label, pattern in _INJECTION_PATTERNS:
        if pattern.search(cleaned):
            warnings.append(label)

    # Step 3: truncate.
    if len(cleaned) > max_length:
        cleaned = cleaned[: max_length - len(_TRUNCATION_MARKER)]
        cleaned = cleaned + _TRUNCATION_MARKER
        warnings.append("truncated")

    return cleaned, warnings


def wrap_user_data(cleaned: str) -> str:
    """Convenience wrapper: emit ``<user_data>cleaned</user_data>``.

    Use this at call sites that build prompts via string
    interpolation. Tool handlers that return dicts (most of
    them) typically don't need this — the engine's universal
    rule applies to ``tool_result`` content blocks too.
    """
    return f"<user_data>{cleaned}</user_data>"


def sanitise_and_wrap(
    value: str, max_length: int = 2000,
) -> tuple[str, list[str]]:
    """Sanitise + wrap in one call.

    Most plugin goal-text sites want the wrapped form. Returns
    ``(wrapped_string, warnings)``; the wrapping happens AFTER
    sanitisation so the warnings list reflects the raw input,
    not the post-wrap string.
    """
    cleaned, warnings = sanitise_retrieved_string(value, max_length)
    return wrap_user_data(cleaned), warnings


def merge_warnings(*sources: Sequence[str]) -> list[str]:
    """Deduplicate + concatenate warnings from multiple sanitise
    calls into the per-step trace metadata field.

    Order-preserving (first occurrence wins) so the trace shows
    "what happened first" rather than alphabetical noise.
    """
    seen: set[str] = set()
    out: list[str] = []
    for source in sources:
        for w in source:
            if w not in seen:
                seen.add(w)
                out.append(w)
    return out
