"""Connector exception taxonomy for Microsoft Graph (and, by precedent,
future external connectors — XPM, FuseSign, Teams).

Three classes, intentionally coarse:

  ConnectorAuthError    — credentials are invalid or unrecoverable.
                          The user must sign in again or the firm
                          admin must rotate something. Retrying the
                          same call with the same state will fail
                          identically. Examples: Microsoft 4xx
                          (invalid_grant, expired refresh token,
                          tenant-side revocation), undecryptable
                          ciphertext (corrupt blob, AAD mismatch,
                          rotated master key).

  ConnectorRateLimited  — server told us to slow down (HTTP 429).
                          Carries `retry_after` (seconds) parsed
                          from the Retry-After header where
                          available. Distinguished from
                          ConnectorTransient because the caller may
                          want to schedule (rather than immediately
                          retry) and may want to surface the wait
                          time to the user.

  ConnectorTransient    — recoverable failure. Retrying after a
                          delay (and possibly with jitter/backoff)
                          may succeed. Examples: Microsoft 5xx,
                          network errors, timeouts.

Names match the architecture document's §3.1 connector taxonomy so
later refactors (a unified GraphClient class, an XPM client) can
adopt the same surface without renaming.
"""


class GraphConnectorError(Exception):
    """Base class for Graph connector failures."""


class ConnectorAuthError(GraphConnectorError):
    """Credentials are invalid or unrecoverable; user must sign in again."""


class ConnectorRateLimited(GraphConnectorError):
    """Server told us to back off (HTTP 429)."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(
            f"rate limited by Microsoft Graph; retry_after={retry_after}"
        )
        self.retry_after = retry_after


class ConnectorTransient(GraphConnectorError):
    """Recoverable failure; safe to retry after backoff."""
