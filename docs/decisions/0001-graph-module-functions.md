# ADR-0001: Microsoft Graph connector uses module-functions, not a client class

## Status

Accepted. Effective since Phase 3C (mail / calendar / drive
connectors); extended through Phases 11 (subscriptions) and 12
(calendar webhooks). Supersedes the implicit "one connector class
per external system" assumption of the architecture doc §3.3.

## Context

The architecture doc proposes a connector layer with one class per
external system: `XPMClient`, `FuseSignClient`, `AnthropicClient`,
and so on. Each holds firm-scoped state (encrypted credentials,
rate-limit budget, audit logger) and exposes methods like
`xpm.create_note(client_id, body)`.

Microsoft Graph didn't fit that shape cleanly when we wired it in
Phase 3C. Three pressures pushed in another direction:

1. **Two distinct credential scopes.** Graph supports both
   delegated (per-user) and application (per-firm) tokens, and
   the choice depends on the resource. Mail / calendar reads use
   the per-user token (so the message-list reflects what the user
   would see in Outlook); subscriptions and lifecycle management
   use the per-firm app-only token. A single `GraphClient` would
   need to either accept the context per call (defeating the
   class) or hold both tokens (defeating per-call rate-limit
   accounting and audit logging).

2. **Cross-cutting helpers grow alongside the connector.**
   Subscription bootstrap (Phase 11-2), the platform-wide sweep
   (11-3), missed-notification backfill (11-7), lifecycle event
   handling (11-6), per-user token refresh (11-5), and the
   subscription delete-on-deactivate path (11-9) are all
   Graph-adjacent, but they're not method calls on a "client" —
   they're orchestration helpers that bundle multiple Graph calls
   with DB writes, audit, and per-firm RLS.

3. **Testing.** Each module-function is a single async function
   you can drive with `respx`. A class with N methods sharing an
   `httpx.AsyncClient` instance is harder to mock per-method
   without monkeypatching internals.

## Decision

`coworker.graph` is structured as **module-functions over two
context types**, not as a single client class.

### Two context types

```python
# coworker.graph.context.GraphContext       (per-user, delegated)
@dataclass(frozen=True)
class GraphContext:
    firm: Firm
    user: User
    access_token: str   # decrypted, freshly refreshed if near expiry
    session: AsyncSession

# coworker.graph.subscriptions.AppGraphContext  (per-firm, app-only)
class AppGraphContext(BaseModel):
    firm: Firm
    access_token: str   # acquired via client_credentials grant
    session: AsyncSession
```

Choice rule:

- **Per-user resources** (mailbox, calendar, drive items the
  signed-in user owns) take `GraphContext`. Construct via
  `coworker.graph.user_context.resolve_user_graph_context(...)`
  which transparently refreshes a near-expiry stored token.
- **Tenant-scoped operations** (subscriptions on `/users/{id}/...`,
  lifecycle management) take `AppGraphContext`. Construct via
  `coworker.graph.subscriptions.graph_app_context(...)` which
  uses a process-wide token cache keyed on firm.

### Module-functions

Each module exposes a small set of async functions, each taking
the appropriate context as its first argument:

```text
coworker.graph.mail
  list_inbox(ctx: GraphContext, *, top, since)        -> [InboxMessage]
  get_message(ctx: GraphContext, message_id)          -> FullEmailMessage
  create_draft(ctx: GraphContext, *, to, subject, ...) -> FullEmailMessage
  mark_as_read(ctx: GraphContext, message_id)         -> None

coworker.graph.calendar
  list_calendar_events(ctx: GraphContext, *, start, end, top)
    -> [CalendarEvent]

coworker.graph.drive
  (streaming download into a SpooledTemporaryFile, etc.)

coworker.graph.subscriptions
  subscribe_change_notifications(ctx: AppGraphContext, ...)
  renew_subscription(ctx: AppGraphContext, sub_id, ...)
  delete_subscription(ctx: AppGraphContext, sub_id)

coworker.graph.auth
  refresh_access_token(session, user, firm)
    # special: doesn't take a Context — produces the token a
    # GraphContext would carry, called from resolve_user_graph_context.
```

Each function:

- enforces the shadow-mode guard at the function boundary (writes
  only),
- audits success and failure via `append_audit` under the firm's
  RLS context,
- maps Microsoft errors into the shared connector taxonomy
  (`ConnectorAuthError` / `ConnectorRateLimited` / `ConnectorTransient`
  / `ConnectorNotFound`),
- accounts the rate-limit slot (per-mailbox for user calls,
  per-firm for app-only calls).

### Helpers built on top

Above the function layer sit orchestration helpers, each living in
its own module so the per-function audit/error/rate-limit
contract stays at one layer:

| Module | Purpose | Driver |
|---|---|---|
| `subscription_bootstrap.ensure_subscription` | create/renew/reuse one subscription | called per-(user, resource) |
| `subscription_sweep.sweep_subscriptions` | iterate every active firm + user | `coworker-subscribe.timer` (30 min) |
| `missed_sweep.sweep_missed_backfill` | walk `last_missed_at` rows | `coworker-backfill.timer` (5 min) |
| `subscription_backfill.backfill_missed_for_subscription` | per-row reconciliation | called from missed sweep |
| `user_context.resolve_user_graph_context` | per-user `GraphContext` with refresh | called from worker + sweep |

The split keeps each helper testable in isolation (the sweeps take
a `firm_ids` override so a shared test DB doesn't leak other
tests' firms; the per-(user, resource) helpers take an explicit
context so they don't need their own session bootstrapping).

### Trigger discrimination

The webhook receiver (Phase 11-4 + 12-6) maps each incoming
notification to the right plugin trigger by looking at the stored
subscription's `resource` path:

```python
# coworker.graph.subscription_bootstrap
RESOURCE_TRIGGER_MAP: dict[str, Trigger] = {
    "/messages": "email_received",
    "/events":   "calendar_event",
}
```

Adding a new resource type (tasks, drive items, …) is one entry
here plus one template in `_USER_RESOURCE_TEMPLATES` (the sweep's
list of per-user resources to bootstrap). No webhook changes.

## What this means for new connectors

When wiring a new external system (XPM v2, FuseSign API v3, …):

- **Class-with-methods** is fine when the credential scope is
  single (one token per firm) and there's no
  subscription/lifecycle machinery on top. XPMClient and
  AnthropicClient follow this shape today.
- **Module-functions** is the right shape when:
  - the system has both per-user and per-firm credential scopes, OR
  - there's enough orchestration machinery (sweeps, backfill,
    lifecycle) that a single class would either need too many
    "kind: 'user' | 'app'" branches or would devolve into a stub
    container for free functions anyway.

## Consequences

- **No `GraphClient` import target.** Code that wants Graph mail
  imports `coworker.graph.mail.list_inbox` directly. This keeps
  the dependency graph honest (callers that only need calendar
  don't pull in mail).
- **Two construction sites for contexts.** Callers must pick
  `GraphContext` vs `AppGraphContext` based on the operation.
  The split is documented in this ADR and in the per-function
  docstrings; in practice the choice falls out of the call site
  (worker pool → `GraphContext`; subscription sweep →
  `AppGraphContext`).
- **Token refresh lives in user_context.** All per-user
  `GraphContext` construction goes through
  `resolve_user_graph_context` so token refresh has exactly one
  call site. Phase 11-8's refactor unified this across the worker
  and the missed-backfill sweep.
- **The architecture doc's §3.3 "connector class per system"
  framing remains correct for everything except Graph.** No other
  current connector has demanded the split; if FuseSign or XPM
  later grow webhook + lifecycle layers we may revisit.

## Phases that landed pieces of this

- 3C-1..3C-6 — connector functions: mail, calendar, drive,
  rate limiter, errors module
- 6-9 — `graph_ctx` threaded through `execute_plugin` into the
  agent loop
- 11-1..11-9 — subscription schema + bootstrap + sweep +
  clientState validation + token refresh + lifecycle events +
  missed backfill + cleanup
- 11-8 — extracted `user_context.resolve_user_graph_context`
  out of the worker into a shared module
- 12-1, 12-6 — calendar tool + calendar webhook subscriptions
