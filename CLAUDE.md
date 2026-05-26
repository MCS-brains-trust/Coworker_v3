# MC & S CoWorker v3 — Claude Code Context

Read this file at the start of every session. It is the project's state-of-the-build anchor, written 2026-05-24 from the discovery audit at `~/discovery_report_phase1.md`. Re-read the audit if any deployed-state claim here feels off.

## 1. What this project is

MCS CoWorker v3 is a server-native AI accounting practice automation system, built first for MC & S Pty Ltd's Australian tax-and-business-advisory work and designed from day one to license to other Australian accounting practices as a multi-tenant SaaS. It replaces the v2.2 Windows desktop app, which keeps running until cutover. Backend is FastAPI on Python 3.12, Postgres 16 with pgvector, Redis 7, all on a single Ubuntu 24.04 DigitalOcean droplet (`coworker-v3-prod-syd1`, SYD1) behind Caddy. Frontend is React 19 + Vite 6 + Tailwind v4 + TanStack Query 5 + React Router 7. Production domain: `coworker.mcands.com.au`.

## 2. Where the code lives

Monorepo at `github.com/faceless-truth/Coworker_v3`, default branch `main`. Top-level directories: `backend/`, `frontend/`, `infra/`, `docs/`, `scripts/`, `tasks/` (the last is gitignored — task spec files only). Backend Python source under `backend/coworker/` with `setuptools.packages.find` rooted at `backend/`. Frontend source under `frontend/src/`. Built frontend bundle is uploaded directly to `/opt/coworker/shared/frontend/` (Caddy serves it as static files; no node service in production). The deployed backend lives at `/opt/coworker/current`, a symlink into `/opt/coworker/releases/<sha>/` maintained by the deploy script.

## 3. What is actually built today

Schema is at Alembic HEAD `e7f8a9b0c1d2`. Seventeen migration files span Phase 2 through Phase 12 and every one is applied to the production DB. The seventeen tables are: `firms`, `users`, `audit_log`, `entities`, `entity_relationships`, `documents`, `client_interactions`, `lessons`, `deadlines`, `jobs`, `agent_traces`, `agent_trace_steps`, `token_usage`, `plugin_installations`, `approval_items`, `graph_subscriptions`, plus `alembic_version`. Every tenant-scoped table has `FORCE ROW LEVEL SECURITY` enabled with four-policy `firm_id` isolation. pgvector + HNSW indexes are wired on `documents.embedding`, `client_interactions.embedding`, `lessons.embedding` (1024-dim). GIN trigram + tsvector indexes with `*_tsv_trigger` keepers cover hybrid search. The `firms` table has encrypted-bytea columns for every external service: Azure client secret, Anthropic API key, XPM client secret + access token + refresh token, FuseSign API key, Teams webhook URL.

Data state on the production DB (2026-05-24): one row in `firms` (`mc-s-accountants`, slug = `mc-s-accountants`, `shadow_mode = true`, `is_sandbox = false`, created 2026-05-01). All other tables are empty. Importantly, **no Azure credentials have been populated on the `mc-s-accountants` firm row yet** — every ciphertext column is NULL — so any sign-in attempt right now returns HTTP 409 from `/auth/microsoft/start/{firm_slug}` with detail `firm has no Azure credentials configured`. This must be resolved before end-to-end sign-in works.

Backend routes deployed today (after the URL-contract migration completed 2026-05-24): everything is under `/api/v1/...` with plural resource names. The authoritative list lives in `docs/FRONTEND_CONTRACT.md`. `/health` and `/` are intentionally **not** under `/api/v1` — they are consumed by ops tools (Caddy health checks, uptime monitors) that do not version-pin. The deprecated singular paths (`/auth/...`, `/approval/...`, `/api/inbox`, `/webhooks/graph/...`) no longer exist.

Auth is fully wired against Microsoft Entra OAuth 2.0 Authorization Code + PKCE, MSAL 1.36.0. Per-firm Azure tenant + client ID + client secret stored encrypted in `firms`. State + code_verifier in Redis with 10-minute TTL, replay-protected by `GETDEL`. Session JWT issued as `HttpOnly` cookie. `auth.py` (366 lines, in `backend/coworker/api/routes/auth.py`) implements start, callback, me, logout end-to-end; helpers live under `backend/coworker/security/`.

Phase 9 approval workflow is live with status enum (`pending|approved|rejected|sent|dispatch_failed`), delivery status enum (`unknown|sent|delivered|failed`), confidence 0-1, two-person approval (required_approvals 1-5 + signatures jsonb), delivery-NDR matching by `executed_internet_message_id`, and edit metadata (`last_edited_at`, `last_edited_by_user_id`).

Phase 11 Graph webhook receiver at `/api/v1/webhooks/graph/{firm_slug}` handles subscription handshake, encrypted-client-state notification validation, subscription lifecycle events (`subscriptionRemoved`, `reauthorizationRequired`, `missed`), and missed-notification backfill triggering.

Workers and timers run on the same droplet: `coworker-api.service` (uvicorn, always-on), `coworker-worker.service` (plugin event worker, always-on), plus five timer-activated oneshots — `coworker-dispatch.service`, `coworker-scheduler.service`, `coworker-subscribe.service`, `coworker-backfill.service`, `coworker-delivery-confirm.service`. All twelve unit files ship in `infra/systemd/` and are installed idempotently by the deploy script.

The deploy script is `backend/scripts/deploy.sh` (567 lines, mature). Two execution paths — LOCAL (default, runs on the droplet via `git archive`) and PUSH (legacy workstation push via ssh + rsync). Six numbered phases: sync code, install deps + run alembic upgrade, install systemd units + swap `current` symlink + restart services, failure gate (refuses success if any `coworker-*` unit is failed), `/health` smoke test, final state printout. Test seams are `DEPLOY_*` env vars; production behaviour with all seams unset is byte-for-byte identical to the pre-seam script.

## 4. What is not built yet

Nine of eleven functional areas from the original `Front_end_spec.md` are not implemented yet: `dashboard`, `plugins`, `memory`, `knowledge-graph`, `activity`, `findings`, `chat`, `specialists`, `settings`. Calling any of these paths today returns 404. They are deferred to a later task and documented in `docs/FRONTEND_CONTRACT.md` § Deferred.

The WebSocket route `/ws/{user_id}` is not implemented. Caddy is wired (`handle /ws/* { reverse_proxy localhost:8001 }`), `websockets>=14.1` is in deps, but no `@app.websocket(...)` source exists. Deferred until dashboard / approvals / findings have real data flow — pushing fake events at fake data is not worth doing.

## 5. Operational state and known issues

- **No firm Azure credentials populated yet (sign-in blocker).** As of 2026-05-25 the `mc-s-accountants` firm row has NULL `azure_tenant_id`, NULL `azure_client_id`, and NULL `azure_client_secret_ciphertext`. `/api/v1/auth/microsoft/start/{firm_slug}` checks all three and returns HTTP 409 `firm has no Azure credentials configured` until they are populated. The fix is the existing `coworker bootstrap-firm` CLI, which is idempotent on `--slug` and refreshes only the three Azure fields on an existing slug: `coworker bootstrap-firm --slug mc-s-accountants --name "MC & S Accountants" --azure-tenant-id <GUID> --azure-client-id <GUID> --azure-client-secret <SECRET>`. The Azure portal redirect URI must also include `https://coworker.mcands.com.au/api/v1/auth/microsoft/callback` before sign-in will succeed.
- **WAL archiver was failing** on Postgres (pgbackrest stanza `main` was uninitialised). `archive_mode` was set to `off` in `/etc/postgresql/16/main/conf.d/coworker.conf` on 2026-05-25 to stop the noise and prevent eventual WAL accumulation. PITR is therefore NOT currently working. Phase 14 (Backups/DR) will properly initialise the pgbackrest stanza and turn archiving back on.
- **Two env files** must be kept in sync: `/opt/coworker/shared/credentials/coworker.env` (systemd `EnvironmentFile`, canonical) and `/opt/coworker/current/.env` (release-local copy made by `deploy.sh`). They are independent files with identical content, not symlinks. The deploy script overwrites the latter on each release.
- **`MASTER_ENCRYPTION_KEY` was duplicated** in both env files until 2026-05-25, with two different values. The line that came second (last-wins via systemd `EnvironmentFile` semantics) is the live key. The duplicate was removed safely because **no ciphertexts existed in the DB** at the time (every encrypted column on `firms` was NULL, and `users` / `graph_subscriptions` were empty). If you ever see two definitions again, confirm which the running process has loaded (`sudo cat /proc/$(systemctl show -p MainPID coworker-api.service --value)/environ | tr '\0' '\n' | grep MASTER`) before touching anything. Backups of the pre-dedupe files live at `/opt/coworker/{current/.env,shared/credentials/coworker.env}.backup-20260525-*`.
- **`ANTHROPIC_API_KEY` is exported in the `elio` shell environment** on the droplet (discovered via a stray pydantic traceback). Source is probably `~/.bashrc` or direnv; worth confirming it isn't in command history.
- **Postgres has occasional idle-in-transaction connections.** Not blocking, but worth a look at `get_session`'s commit lifecycle if connection-pool contention shows up.
- **Caddy `/webhooks/*` block was dropped** as part of the URL-contract migration. Webhook traffic now flows through the `/api/*` reverse-proxy block to `localhost:8001`. The old `localhost:8002` target was dangling — no process ever listened there.
- **Postgres role privileges + pgvector trust flag**: the `coworker` role needs `CREATEDB` for the test fixture to create `coworker_test` (`sudo -u postgres psql -c "ALTER ROLE coworker CREATEDB;"`). The `vector` extension must be marked trusted (`/usr/share/postgresql/16/extension/vector.control` ends with `trusted = true`) so the non-superuser `coworker` role can `CREATE EXTENSION vector` during migrations. Both were one-time fixes applied 2026-05-25 mid-task when running the test suite from this clone for the first time.

## 6. How to run things

This repo is checked out at `/home/elio/code/mcs-coworker-v3` on the droplet (same machine as production). The dev clone has no `.env` of its own; production config lives at `/opt/coworker/current/.env` (mode `0640`, `root:coworker`) and the systemd canonical at `/opt/coworker/shared/credentials/coworker.env`.

- **Local API server (against prod DB — use carefully):** `cd backend && uv run uvicorn coworker.api.main:app --reload --port 8001`. Requires `DATABASE_URL`, `REDIS_URL`, `MASTER_ENCRYPTION_KEY`, `SESSION_JWT_SECRET` exported in your shell.
- **Tests:** `cd /home/elio/code/mcs-coworker-v3 && uv run pytest backend/tests`. The conftest creates a `coworker_test` DB; the `coworker` role needs `CREATEDB` (one-time `ALTER ROLE coworker CREATEDB;` if missing).
- **Deploy:** `cd /home/elio/code/mcs-coworker-v3 && ./backend/scripts/deploy.sh`. Default release tag is `git rev-parse --short HEAD`. Refuses a dirty tree unless `DEPLOY_SKIP_GIT_CHECK=1`.

One-time host setup the migrations expect: pgvector must be marked trusted (`sudo sed -i 's/^trusted = false/trusted = true/' /usr/share/postgresql/16/extension/vector.control`) because the `create_extensions` migration runs as the non-superuser `coworker` role. `pg_trgm` and `pgcrypto` are trusted by default.

## 7. House style for code

- Python 3.12+, async-first. SQLAlchemy 2.x asyncio. Pydantic v2 for boundary models. Loguru for logging (never `print`). No bare `except`. `async with` for sessions.
- **Strict types.** `mypy --strict` must pass. No `Any` unless interfacing with an untyped library.
- **No em dashes** in code, comments, docstrings, or commit messages. Use colons, semicolons, parentheses. (User-facing copy is the only exception, and even there prefer plain prose.)
- **Australian English** in all user-facing strings.
- Ruff lint config in `pyproject.toml`: `select = ["E", "F", "W", "I", "N", "UP", "B", "C4", "SIM", "RUF"]`, `ignore = ["E501"]`, line length 100, target-version py312.
- Test conventions: `pytest-asyncio` in `auto` mode (no decorator). `factory-boy` for fixtures, `respx` for httpx mocking. Tests live in `backend/tests/{unit,integration}/`.
- **Audit log is append-only and chain-verified.** Use `coworker.security.audit.append_audit(...)`; never raw-insert into `audit_log`. Every entry has `firm_id NOT NULL`. Events that never reached a firm (invalid OAuth state, unknown slug) go to **Loguru** with structured fields, not the audit chain. The two are observable separately for a reason.
- **PII scrubbing before every Anthropic call.** Use `coworker.security.pii.PIIScrubber.scrub()`. Restore placeholders on response text.
- **Shadow mode is enforced at the connector layer**, not the plugin layer. Every write method on a connector calls `_guard_writable()` first.
- **Never hardcode model strings.** Read from `Settings.ANTHROPIC_MODEL_DEFAULT` / `_REASONING` / `_FAST`.
- **Never log secrets.** The Loguru patcher redacts known patterns; be careful when adding new credentials.
- Migrations are forward-only. One migration per logical change. Both `upgrade()` and `downgrade()` required (write the destructive downgrade anyway).
- Commit-message convention: `phase X: <area>` or `fix(<area>): <specific>` or `feat(<area>): <specific>` or `docs(<area>): <specific>`. One concept per commit.
- Never commit `.env`. `.env.example` is the only env file in the repo.

## 8. Multi-tenancy and RLS

Every domain table has `firm_id` with FK to `firms.id` and an index. RLS policies use `NULLIF(current_setting('app.firm_id', true), '')::uuid` — the `NULLIF(..., '')` is load-bearing because both `SET LOCAL` post-COMMIT and `RESET app.firm_id` leave the GUC at empty string rather than NULL, and `''::uuid` would raise on every subsequent transaction without a firm context. `FORCE ROW LEVEL SECURITY` is enabled on every tenant table so the application role (`coworker`, which owns the tables) is also subject to RLS.

Application code declares firm scope with `async with firm_context(firm_id): ...`. A SQLAlchemy `after_begin` listener reads the contextvar at transaction start and issues `SELECT set_config('app.firm_id', :firm_id, true)`. If a code path forgets to enter `firm_context`, the listener does nothing, the GUC stays unset, every RLS predicate evaluates to NULL, and queries return zero rows — not every row. That is the property that makes "forgot to set the firm context" a 0-row response rather than a data leak.

A pool-checkin handler (`_attach_pool_listeners` in `coworker.db.session`) issues `RESET app.firm_id` when a connection returns to the pool, so even a buggy `set_config(..., is_local=false)` cannot leak firm context across requests. This is actively tested in `backend/tests/integration/test_rls.py::test_rls_pool_reuse_does_not_leak_firm_context`.

Tests that need cross-firm data use the `ALTER TABLE ... NO FORCE / INSERT / FORCE` bracket inside a single transaction as the table owner. The `coworker` role is intentionally not granted `BYPASSRLS`; backups and break-glass admin maintenance use a Postgres superuser.

For inspecting tenant tables via raw psql as `coworker`, set the GUC manually first: `SET app.firm_id = '<uuid>';`. Otherwise `SELECT * FROM firms` returns zero rows.

## 9. Known schema notes

- **`users.azure_object_id` is globally UNIQUE**, not composite-unique on `(firm_id, azure_object_id)`. The OAuth callback selects users by `oid` only and relies on RLS to scope the SELECT to the current firm. Cross-firm collisions are rejected at INSERT with an `IntegrityError` and surface as HTTP 409. The fix is a future migration that drops the global UNIQUE and replaces it with `UNIQUE(firm_id, azure_object_id)`, plus updating the OAuth callback's lookup to `WHERE firm_id = ? AND azure_object_id = ?`.

## 10. The frontend contract

`docs/FRONTEND_CONTRACT.md` is the canonical source of truth for the URL contract between backend and frontend. The original `Front_end_spec.md` predates the contract and is superseded. If the frontend is ever ahead of or behind the contract, the contract wins. The frontend team owns its own spec document derived from this one.

## 11. Discovery audit

`~/discovery_report_phase1.md` (on the droplet, 55 KB, 2026-05-24) is the forensic state-of-the-build audit that this CLAUDE.md was re-grounded from. Re-read it if anything about the actual deployed state is unclear before doing new work. New audits should be saved alongside it and the latest one referenced from here.

## 12. Task workflow

New work arrives as task files in `tasks/` (gitignored, droplet-local). Each task is mechanical, has its own preflight, and ends with a deploy + verification step. Do not improvise; if the task file is unclear, pause and ask before changing scope.

When asked to do something risky — schema changes, production deploys, anything touching audit / encryption / RLS, anything that writes to external systems — show your work, confirm shadow mode is engaged, and check with Elio before proceeding. A user approving an action once does not approve it in all contexts.

## 13. Things this project explicitly does not do

- We do **not** modify v2.2. v2.2 stays running until Phase 16 cutover.
- We do **not** use ChromaDB. All vectors live in Postgres + pgvector.
- We do **not** use Docker in production. systemd-only on the droplet.
- We do **not** use Celery. Redis + APScheduler + custom workers.
- We do **not** auto-send emails. Drafts only, until shadow mode is graduated AND the action is approved AND it is not a two-person category.
- We do **not** use a centralised MC & S Azure app for client firms. Each firm registers their own.
- We do **not** hardcode model strings.
- We do **not** log full TFNs, full credit cards, or any other unmasked PII. Ever.

## 14. Anthropic model pinning

Always read from settings; never hardcode.

| Variable | Default | Use case |
|---|---|---|
| `ANTHROPIC_MODEL_DEFAULT` | `claude-sonnet-4-6` | Orchestrator default, drafting, classification |
| `ANTHROPIC_MODEL_REASONING` | `claude-opus-4-7` | Specialists, complex multi-step reasoning, vision |
| `ANTHROPIC_MODEL_FAST` | `claude-haiku-4-5-20251001` | Document classification, routing, simple summarisation |

Extended-thinking default `thinking_budget` is 16000 tokens; specialists use 32000.
