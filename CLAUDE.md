# MC & S CoWorker v3 — Claude Code Context

> Read this entire file at the start of every session. It is the source of truth for the project's state, conventions, and constraints.

## What this project is

MC & S CoWorker v3 is an **AI accounting practice automation platform** built as a server-native multi-tenant SaaS. It replaces v2.2, a Windows desktop application that we are NOT modifying. v3 will be cut over to once it is proven through shadow mode and pilot.

- **Production target:** DigitalOcean droplet `coworker-v3-prod-syd1` in SYD1.
- **Production domain:** `coworker.mcands.com.au`
- **Repository:** `faceless-truth/Coworker_v3`
- **Architecture document:** Read `docs/architecture.md` if present, otherwise the canonical reference is the `MCS_CoWorker_v3_Architecture.md` document Elio has separately. **The architecture doc is the source of truth — when in doubt, follow it.**

## Current build state (as of this CLAUDE.md being written)

**Completed phases:** 0, 1, 2.

| Phase | What's done |
|-------|-------------|
| 0 | Droplet provisioned & hardened (Ubuntu 24.04, Postgres 16 + pgvector, Redis 7, Caddy, ufw, systemd) |
| 1 | Repo structure, FastAPI skeleton at `coworker.api.main:app`, `/health` endpoint, Alembic, deploy script, age-encrypted env, Caddy reverse proxy, `coworker-api.service` running |
| 2 | Phase 2 — completion pending audit fixes (see docs/audits/2026-05-01-pre-phase-3-audit.md). Primitives in place (encryption, audit log, PII scrubber, MS OAuth scaffolding) but gaps require Stages A–E to close before Phase 3 begins. |

The pre-Phase-3 audit identified seven listed gaps and 18 additional issues spanning critical (committed master key), high (missing RLS policies, broken get_session, stale model strings), and medium/low severity items. These are being worked through systematically in Stages A–E. Stage A (dependency and configuration prerequisites) is in progress.

**Currently working on:** Stage B2 (test infrastructure and DB session fixes) per docs/audits/2026-05-01-pre-phase-3-audit.md. Phase 3 (External Service Connectors) begins after audit fixes are complete and verified.

## Known gaps from Phase 2 that must be closed before Phase 3 begins

These are NOT optional. Phase 3 builds connectors that depend on credentials being correctly stored and retrieved, on tokens being correctly encrypted, and on the OAuth flow producing valid User rows. If these gaps remain, Phase 3 will be debugging Phase 2 issues with five new layers on top.

1. **OAuth implementation is incomplete.** `backend/coworker/api/routes/auth.py` contains `state = "dummy_state"`, `code_verifier = "dummy_verifier"`, and the `/callback` and `/me` routes are placeholders. **A real Microsoft sign-in has never been performed end-to-end against this code.** Implement properly: generate PKCE verifier, store state+verifier in Redis with TTL, validate state on callback, exchange code, encrypt and persist refresh token on `users.ms_refresh_token_ciphertext`, issue session JWT cookie, expose `/me` returning the logged-in user.

2. **No security tests for cross-firm encryption boundary.** `EnvelopeCipher` accepts an `associated_data` parameter bound to firm_id. There must be a test that proves `decrypt_str(encrypt_str("x", firm_id=A), firm_id=B)` raises. Currently `test_encryption.py` exists but I don't know if it covers this.

3. **No tamper-detection test for audit log.** There must be a test that writes N audit entries, mutates one row's `payload`, runs `verify_chain`, and asserts it returns `(False, <broken_id>)`.

4. **CLI `create-firm` is incomplete.** Currently takes only `name`. The architecture doc says it should accept `--slug`, `--abn`, `--timezone`. Add those flags.

5. **README phase status table is stale.** Update it to reflect Phase 0/1/2 as Complete and Phase 3 as Next.

6. **No local Postgres/Redis running in WSL.** Without these, no test can actually exercise the database. We need them set up before the audit can be meaningful.

7. **`slugify` is used in `cli/main.py` but I don't see it in `pyproject.toml` dependencies.** Verify; add `python-slugify` if missing.

## Conventions

### Python style

- **Python 3.12+, async-first.** All database I/O, all HTTP I/O, all Anthropic calls go through async APIs.
- **Type hints are strict.** `mypy --strict` must pass. No `Any` unless interfacing with untyped library code.
- **Pydantic v2 for all models** that cross a boundary (API request/response, tool inputs, configuration). SQLAlchemy 2.x mapped classes for DB models.
- **One module per concern.** A connector is one file. A schema is one file. Don't co-mingle.
- **Loguru for logging.** Structured JSON in production, pretty in dev. Never `print()`.
- **No bare `except`.** Always specify the exception type. Always log with stack trace before re-raising.
- **`async with` for sessions.** All database sessions are async-context-managed. The pattern is in `coworker.db.session.get_session`.

### Database

- **Every domain table has `firm_id` with FK to `firms.id` and an index.** No exceptions.
- **RLS policies on every tenant-scoped table.** Use `with_firm_scope` to set `coworker.current_firm_id` at the start of every request handler's session.
- **Migrations are forward-only.** Each migration is small enough to be reviewable. Use Alembic.
- **Never store raw TFNs.** Hash them or store last-4 only.
- **Encrypt all credentials at rest** with `EnvelopeCipher` and `associated_data=firm_id`.

### Multi-tenancy

- **`firm_id` is in every query.** If you forget, RLS catches you, but the application code should still always filter explicitly.
- **No global state that crosses firms.** No singleton clients holding firm-specific tokens. Every connector is constructed per-firm.

### Security

- **PII scrubbing before every Anthropic call.** Use `PIIScrubber.scrub()`. Restore placeholders on response text.
- **Audit log every meaningful action.** Use `append_audit()`. Especially: connector calls, OAuth events, approval decisions, shadow-mode changes, credential rotations.
- **Shadow mode is enforced at the connector layer**, not the plugin layer. Every write method on a connector must call `_guard_writable()` first.
- **Never hardcode model strings.** Read from `Settings.ANTHROPIC_MODEL_DEFAULT` / `_REASONING` / `_FAST`.
- **Never log secrets.** The Loguru patcher should redact known secret patterns. Be careful with new patterns.

### Tests

- **Tests live in `backend/tests/`.** `unit/` for pure-Python tests, `integration/` for tests that touch Postgres/Redis.
- **`pytest-asyncio` mode is auto.** No `@pytest.mark.asyncio` decorator needed.
- **Every security primitive has a test.** Encryption, audit, PII scrubbing, RLS, OAuth — all need both happy-path and adversarial tests.
- **Connectors need mocked HTTP tests** using `respx` (not yet a dependency — add it).

### Git

- **One concept per commit.** A migration + the model + the test + the route. Not five unrelated things.
- **Commit messages start with phase and area:** e.g. `Phase 3: anthropic connector with PII scrubbing` or `fix: CLI create-firm uses SessionLocal not get_session_maker`.
- **Never commit `.env`.** `.env.example` is the only env file in the repo.
- **Never commit secrets.** Even in tests. Use fixtures with deterministic dummy values.

## Repository layout

```
mcs-coworker-v3/
├── pyproject.toml          # uv-managed; package discovery rooted at backend/
├── README.md
├── .env.example
├── .gitignore
│
├── backend/
│   ├── alembic.ini
│   ├── migrations/         # NOTE: Alembic migrations are HERE, not coworker/db/migrations/
│   │   ├── env.py
│   │   └── versions/
│   ├── coworker/
│   │   ├── config.py       # Pydantic Settings, env-driven
│   │   ├── logging.py
│   │   ├── api/
│   │   │   ├── main.py     # FastAPI app
│   │   │   └── routes/
│   │   │       └── auth.py # OAuth routes (currently stubs — see gaps above)
│   │   ├── cli/
│   │   │   └── main.py     # `coworker` CLI entrypoint
│   │   ├── connectors/     # Phase 3 — currently empty
│   │   ├── db/
│   │   │   ├── base.py
│   │   │   ├── session.py  # SessionLocal, get_session
│   │   │   └── models/
│   │   │       ├── tenancy.py  # Firm, User
│   │   │       └── audit.py    # AuditLogEntry
│   │   ├── knowledge_graph/  # Phase 4 — empty
│   │   ├── memory/           # Phase 4 — empty
│   │   ├── orchestrator/     # Phase 5 — empty
│   │   ├── plugins/          # Phase 6 — empty
│   │   ├── security/
│   │   │   ├── audit.py        # append_audit, verify_chain
│   │   │   ├── auth.py         # MSAL helpers (build_auth_url, exchange_code)
│   │   │   ├── encryption.py   # EnvelopeCipher
│   │   │   ├── pii.py          # PIIScrubber with AU recognisers
│   │   │   └── rls.py          # with_firm_scope helper
│   │   ├── specialists/      # Phase 8 — empty
│   │   ├── vision/           # Phase 7 — empty
│   │   └── workers/          # Phase 6 — empty
│   ├── scripts/
│   └── tests/
│       ├── conftest.py
│       ├── test_audit.py
│       ├── test_encryption.py
│       ├── unit/
│       └── integration/
│
├── frontend/                 # Phase 10
├── docs/
│   └── decisions/            # ADRs
└── infra/
    ├── caddy/
    ├── monitoring/
    ├── postgres/
    ├── redis/
    ├── systemd/
    │   └── coworker-api.service
    └── backup/
```

## Local development setup

The team works in WSL2 (Ubuntu 24.04) on Windows. The droplet runs the same OS so behaviour matches.

```bash
# From repo root
uv sync                                                  # install deps including dev extras
uv sync --extra dev

# Local Postgres and Redis via Docker (recommended for dev)
# OR install natively in WSL — see docs/developer-setup.md when written

# Generate a master encryption key
python3 -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"

# Create .env from .env.example, fill in real values
cp .env.example .env

# Run migrations
cd backend && uv run alembic upgrade head

# Start API in dev mode
uv run uvicorn coworker.api.main:app --reload --port 8001

# Run tests
uv run pytest backend/tests
```

## How Claude Code should behave in this repo

### Reading

- **Always read this file first.**
- **Read `docs/architecture.md` (or the architecture doc Elio provides) before writing any new code.** Phase boundaries matter.
- **Read existing code in the area you're modifying.** Conventions live in the code, not just in this file.

### Writing

- **Match the existing code style exactly.** Look at `security/encryption.py` for an example of the bar.
- **Write the test alongside the implementation.** Not as a follow-up commit.
- **Never introduce a new dependency** without checking if there's already something in `pyproject.toml` that does the job.
- **Never disable a type check or a lint rule** to make code pass. Fix the code.
- **Never write code that bypasses RLS, the audit log, the PII scrubber, or shadow mode.** These are inviolable.

### Migrations

- **One migration per logical change.** A new table is one migration. Adding a column is one migration.
- **Always include both `upgrade()` and `downgrade()`.** Even if downgrade is destructive — write it anyway.
- **Test migrations both directions** before committing: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`.

### Commits

- **Commit messages must follow the pattern shown in git history.** `Phase X: <area>` or `fix: <specific>`.
- **Don't commit work-in-progress.** A commit should be a working, tested state.
- **Don't squash unrelated commits.** Multiple commits per PR is fine; one commit doing five things is not.

### When asked to do something risky

- **Schema changes:** propose the migration, explain the risk, ask for confirmation before applying.
- **Production deploys:** never deploy to production without explicit Elio approval in this conversation.
- **Anything touching the audit log, encryption, or RLS:** show your work, write the test first, ask for review.
- **Anything that would write to external systems (Outlook, FuseSign, XPM, Teams):** verify shadow mode is engaged for the firm; never disable shadow mode without explicit instruction.

### When unsure

- **Ask.** Don't guess at architecture. The phases are sequenced for a reason.
- **Refer to the architecture document.** It has the answer.
- **Check git history for precedent.** How was a similar problem solved before in this codebase?

## Anthropic models — current pinning

These are the model strings to use. Always reference via env, never hardcode.

| Variable | Default | Use case |
|---|---|---|
| `ANTHROPIC_MODEL_DEFAULT` | `claude-sonnet-4-6` | Orchestrator default, drafting, classification |
| `ANTHROPIC_MODEL_REASONING` | `claude-opus-4-7` | Specialists, complex multi-step reasoning, vision |
| `ANTHROPIC_MODEL_FAST` | `claude-haiku-4-5-20251001` | Document classification, routing, simple summarisation |

When using extended thinking, default `thinking_budget` is 16000 tokens; specialists use 32000.

## Things this project explicitly does not do

- We do NOT modify v2.2. v2.2 stays running until Phase 16 cutover.
- We do NOT use ChromaDB. All vectors live in Postgres + pgvector.
- We do NOT use Docker in production. systemd-only on the droplet.
- We do NOT use Celery. Redis + APScheduler + custom workers.
- We do NOT auto-send emails. Drafts only, until shadow mode is graduated AND the action is approved AND it's not a two-person category.
- We do NOT use a centralised MC & S Azure app for client firms. Each firm registers their own.
- We do NOT hardcode model strings. Always env-driven.
- We do NOT log full TFNs, full credit cards, or any other unmasked PII. Ever.

## Phases roadmap (read-only here; the architecture doc is canonical)

- Phase 3: External Service Connectors (Anthropic, Graph, XPM, FuseSign, Teams)
- Phase 4: Memory Architecture (pgvector, hybrid retrieval, knowledge graph)
- Phase 5: The Orchestrator (agent loop, tool registry, traces)
- Phase 6: Plugin System (server-native plugins, scheduler, sandbox)
- Phase 7: Vision Pipeline (PDF classification + extraction)
- Phase 8: Specialists & Style Learning
- Phase 9: Approval Queue, Confidence & Autonomy
- Phase 10: Web Frontend
- Phase 11: Real-Time, Reflection & Proactive Intelligence
- Phase 12: Mobile / PWA / Calendar
- Phase 13: Onboarding & Multi-Firm Distribution
- Phase 14: Operations: Monitoring, Backups, DR
- Phase 15: Migration from v2.2
- Phase 16: Shadow → Pilot → Cutover → Decommission

---

When this file's contents conflict with the architecture document, the architecture document wins. When this file's contents conflict with the existing code, treat it as a discrepancy to discuss before changing either.


### Postgres role privileges (local dev)

The `coworker` role needs CREATEDB to let the test fixture create
`coworker_test`:
Stage B1 missed this; corrected mid-Stage-B2 and documented in
`backend/tests/conftest.py`. If you're setting up a fresh WSL/dev
environment, run the ALTER ROLE before running the test suite.
