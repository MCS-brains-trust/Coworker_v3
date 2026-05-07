"""Connector exception taxonomy for Microsoft Graph (and, by precedent,
future external connectors — XPM, FuseSign, Teams).

Two classes, intentionally coarse:

  ConnectorAuthError    — credentials are invalid or unrecoverable.
                          The user must sign in again or the firm
                          admin must rotate something. Retrying the
                          same call with the same state will fail
                          identically. Examples: Microsoft 4xx
                          (invalid_grant, expired refresh token,
                          tenant-side revocation), undecryptable
                          ciphertext (corrupt blob, AAD mismatch,
                          rotated master key).

  ConnectorTransient    — recoverable failure. Retrying after a
                          delay (and possibly with jitter/backoff)
                          may succeed. Examples: Microsoft 5xx,
                          network errors, timeouts, 429 (handled at
                          the call site as a retry, but escalates
                          to ConnectorTransient if the retry also
                          fails).

Names match the architecture document's §3.1 connector taxonomy so
later refactors (a unified GraphClient class, an XPM client) can
adopt the same surface without renaming.
"""


class GraphConnectorError(Exception):
    """Base class for Graph connector failures."""


class ConnectorAuthError(GraphConnectorError):
    """Credentials are invalid or unrecoverable; user must sign in again."""


class ConnectorTransient(GraphConnectorError):
    """Recoverable failure; safe to retry after backoff."""
