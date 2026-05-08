"""Shared connector exception taxonomy.

Used by every external-service connector — Microsoft Graph,
Anthropic, XPM, FuseSign, Teams. All of them produce HTTP-shaped
failures that fall into the same coarse buckets, so callers
(orchestrator, plugins, routes) can reason about failure modes
uniformly rather than learning a separate exception family per
connector.

Three classes, intentionally coarse:

  ConnectorAuthError    — credentials are invalid or unrecoverable.
                          The user must sign in again or the firm
                          admin must rotate something. Retrying the
                          same call with the same state will fail
                          identically. Examples: Microsoft 4xx
                          (invalid_grant, expired refresh token,
                          tenant-side revocation), Anthropic 401,
                          undecryptable ciphertext (corrupt blob,
                          AAD mismatch, rotated master key).

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
                          may succeed. Examples: 5xx, network
                          errors, timeouts.

Names match the architecture document's §3.1 connector taxonomy.
The base class is named `ConnectorError` rather than the older
`GraphConnectorError` because the family is now shared; the latter
is preserved as an alias so existing imports continue to resolve
without churn.
"""


class ConnectorError(Exception):
    """Base class for any external-connector failure."""


# Alias preserved for the older Graph-specific name. Direct imports of
# `GraphConnectorError` continue to work; new code should use
# `ConnectorError` for the base class.
GraphConnectorError = ConnectorError


class ConnectorAuthError(ConnectorError):
    """Credentials are invalid or unrecoverable; user must sign in again."""


class ConnectorRateLimited(ConnectorError):
    """Server told us to back off (HTTP 429)."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(f"rate limited; retry_after={retry_after}")
        self.retry_after = retry_after


class ConnectorTransient(ConnectorError):
    """Recoverable failure; safe to retry after backoff."""
