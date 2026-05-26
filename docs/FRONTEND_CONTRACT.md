# Frontend Contract

**Last updated:** 2026-05-24
**Source of truth:** this file. The `Front_end_spec.md` document that predates this file is superseded.

## URL Conventions

- All routes are prefixed `/api/v1/...`
- Resource names are plural (`/approvals`, not `/approval`)
- Path parameters are documented in each endpoint section
- All endpoints require auth via session cookie (HttpOnly) except where explicitly marked
- Response envelopes use `{ items: [], total: N }` for collections, bare object for singletons

## Auth

The auth flow is Microsoft Entra OAuth 2.0 Authorization Code + PKCE against the firm's own Azure AD tenant. There is no email + password endpoint. There is no JWT in localStorage. The session is an `HttpOnly` cookie set by the backend after the OAuth callback.

### `GET /api/v1/auth/microsoft/start/{firm_slug}`

No auth required. Initiates the OAuth flow for the named firm.

Looks up the firm by slug. Generates state + PKCE code_verifier in Redis (10-minute TTL). Redirects (302) to `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?...` with the firm's `azure_client_id` and computed redirect URI.

For MC&S, `firm_slug` is `mc-s-accountants`.

### `GET /api/v1/auth/microsoft/callback?code=...&state=...`

No auth required. Microsoft's redirect target.

Atomically consumes the state from Redis (GETDEL, replay-protected). Decrypts the firm's Azure client secret. Exchanges the auth code for tokens. Decodes the id_token. Upserts the User row keyed on `azure_object_id` (with encrypted refresh token). Appends an audit entry. Mints a session JWT and sets it as an `HttpOnly` cookie. Redirects (302) to `OAUTH_POST_LOGIN_REDIRECT` (configured via `.env`, default `/`).

### `POST /api/v1/auth/logout`

Auth required. Clears the session cookie locally. Does not revoke the Microsoft refresh token (out of scope for now).

### `GET /api/v1/auth/me`

Auth required. Returns the current user.

Response 200:
```json
{
  "user_id": "uuid",
  "firm_id": "uuid",
  "firm_slug": "mc-s-accountants",
  "upn": "elio@mcands.com.au",
  "display_name": "Elio M",
  "role": "owner"
}
```

## Approvals

### `GET /api/v1/approvals/pending`

Auth required.

Query params:
- `limit` (int, default 50, max 200)

Response 200: bare JSON array of approval-item objects (newest first, RLS-scoped to the authenticated firm). Each object mirrors the `approval_items` row with: `id`, `trace_id`, `plugin_name`, `category`, `summary`, `payload`, `status`, `decided_at`, `decided_by_user_id`, `decision_notes`, `last_edited_at`, `last_edited_by_user_id`, `required_approvals`, `approval_signatures`, `confidence`, `created_at`, `updated_at`.

Future tasks will broaden this to `GET /api/v1/approvals?status=<status>&limit=&offset=` returning `{ total, items }`. Until then, only pending items are listable.

### `GET /api/v1/approvals/{item_id}`

Auth required. Returns a single approval item. 404 on unknown id or cross-firm read (RLS hides cross-firm rows).

### `POST /api/v1/approvals/{item_id}/approve`

Auth required. Transitions pending to approved. Optional body: `{ "notes": "string, max 2000" }`. Returns the post-transition row. 409 on non-pending state.

### `POST /api/v1/approvals/{item_id}/reject`

Auth required. Transitions pending to rejected. Same body shape and error behaviour as approve.

### `PUT /api/v1/approvals/{item_id}/payload`

Auth required. Replaces a pending item's payload (wholesale, not patch). Body: `{ "payload": { ... } }`. Item stays pending; only approve / reject move to a terminal state. 409 on non-pending state. Phase 9.3 edit metadata is recorded.

## Mail

### `GET /api/v1/inbox`

Auth required. Reads from Microsoft Graph using the current user's encrypted refresh token. Returns the user's recent inbox messages.

## Webhooks

### `POST /api/v1/webhooks/graph/{firm_slug}`

No auth required (validated via client state from Graph). The Microsoft Graph webhook receiver. Handshake on first call (validationToken query parameter), notification dispatch on subsequent calls.

## Health

### `GET /health`

No auth required. Returns `{ status: "ok", service: "coworker-api", version: "3.0.0", shadow_mode: "True" }`. Notable: this route is **NOT** under `/api/v1` because it is consumed by ops tools (Caddy health checks, uptime monitors) that do not version-pin.

### `GET /`

No auth required. Returns `{ service: "MC & S CoWorker v3" }`. Also not under `/api/v1` for the same reason.

## Deferred (not yet implemented)

These nine areas from the original Front_end_spec.md are not implemented in this task. They will be added in later tasks. The frontend should not call these paths until they are implemented; calling them today returns 404.

- `/api/v1/dashboard/summary`
- `/api/v1/plugins` (list, toggle, run)
- `/api/v1/memory`
- `/api/v1/knowledge-graph/nodes`
- `/api/v1/activity`
- `/api/v1/findings` (list, act, dismiss)
- `/api/v1/chat/history` and `/api/v1/chat/message` (SSE)
- `/api/v1/specialists` (list, get prompt, update prompt)
- `/api/v1/settings/firm`, `/api/v1/settings/user`, `/api/v1/settings/token-usage`

## Deferred (WebSocket)

The WebSocket `/ws/{user_id}` route is not implemented. The Caddy reverse proxy is wired (`handle /ws/* { reverse_proxy localhost:8001 }`), the library is in dependencies, but there is no route in the backend. Implementing it now would push fake events to a frontend reading fake data. Defer until the dashboard, approvals, and findings endpoints have real data flow.

## Removed from the original spec

These items from the original `Front_end_spec.md` are not part of the contract and the frontend should remove them:

- `POST /auth/login` (email + password) — never existed in the backend, will not be built. Microsoft sign-in via the redirect flow is the only auth path.
- `Authorization: Bearer <token>` header pattern — sessions use `HttpOnly` cookies. No bearer token, no `localStorage` storage of JWT.

## Errors

All non-2xx responses use:
```json
{
  "error": {
    "code": "STRING_CODE",
    "message": "human-readable message"
  }
}
```

## Pagination

All list endpoints accept `limit` and `offset` query parameters and return `{ total: N, items: [...] }`.
