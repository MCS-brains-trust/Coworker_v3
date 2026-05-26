# Firm Bootstrap Runbook

## Purpose

Onboard a new firm to the MCS CoWorker v3 system. This procedure populates the `firms` table row with the firm's Azure AD credentials, enabling its staff to sign in via Microsoft Entra OAuth, and promotes the first signed-in user to the `owner` role.

Use this runbook for the very first MC & S bootstrap and for every licensed-deployment onboarding.

## Prerequisites

- The firm has its own Azure AD tenant.
- The firm has registered the MCS CoWorker application within their tenant (see "Azure App Registration" below). The redirect URI on the app registration must use the **Web** platform (confidential client), not Single-page application; the backend exchanges the auth code using a stored client secret, which the SPA platform does not allow.
- The firm administrator has supplied:
  - `tenant_id` (GUID)
  - `client_id` (GUID)
  - `client_secret` (string; treat as sensitive)
- The backend's deployed state is operational: `coworker-api.service` is `active`, `/health` returns `status: ok`, schema is at Alembic HEAD.
- The firm slug has been chosen (URL-safe identifier, e.g., `mc-s-accountants`).
- The droplet's `MASTER_ENCRYPTION_KEY` is the one the live `coworker-api` process loaded. Confirm with:
  ```
  sudo cat /proc/$(systemctl show -p MainPID coworker-api.service --value)/environ | tr '\0' '\n' | grep MASTER_ENCRYPTION_KEY
  ```

## Procedure

### 1. Check whether the firm row already exists

```
sudo -u postgres psql -d coworker -c "SELECT slug FROM firms WHERE slug = '<firm_slug>';"
```

If a row exists, this is an update path. The `bootstrap-firm` CLI handles both create and update through one command (UPSERT keyed on `--slug`). On an existing row it refreshes only the three Azure credential fields; name/abn/timezone are left untouched.

If no row exists and you want different metadata than what `bootstrap-firm` will write (e.g., custom ABN or timezone), create the firm first with `coworker create-firm`, then run `bootstrap-firm`. Otherwise `bootstrap-firm` alone is sufficient.

### 2. Bootstrap the Azure credentials

```
sudo -u coworker bash -c "set -a; source /opt/coworker/shared/credentials/coworker.env; set +a; cd /opt/coworker/current && .venv/bin/coworker bootstrap-firm \
    --slug <firm_slug> \
    --name '<Firm Name>' \
    --azure-tenant-id <TENANT_GUID> \
    --azure-client-id <CLIENT_GUID> \
    --azure-client-secret <CLIENT_SECRET>"
```

`/opt/coworker/shared/credentials/coworker.env` is the canonical env file (systemd `EnvironmentFile`); it has the same `MASTER_ENCRYPTION_KEY` the running `coworker-api` process is using, so the ciphertext written here will be decryptable by the OAuth callback.

The CLI prints the firm's UUID on success.

### 3. Verify the encrypted columns populated

```
sudo -u postgres psql -d coworker -c "SELECT slug, azure_tenant_id, azure_client_id, octet_length(azure_client_secret_ciphertext) AS secret_octets FROM firms WHERE slug = '<firm_slug>';"
```

Expected: `azure_tenant_id` and `azure_client_id` are visible plaintext GUIDs; `secret_octets` is non-zero and typically > 100 (the AES-256-GCM envelope adds ~92 bytes of overhead on top of the plaintext secret length).

### 4. Optional decrypt sanity check

If there's any doubt that the encryption used the live `MASTER_ENCRYPTION_KEY` (e.g., after a key rotation, or when bootstrapping shortly after env-file edits), run a one-shot probe:

```python
# /tmp/decrypt_probe.py — delete after running
import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

from coworker.security.encryption import decrypt_str


async def main() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as conn:
        await conn.execute(text("ALTER TABLE firms NO FORCE ROW LEVEL SECURITY"))
        try:
            result = await conn.execute(
                text(
                    "SELECT id, azure_client_secret_ciphertext "
                    "FROM firms WHERE slug = :slug"
                ),
                {"slug": "<firm_slug>"},
            )
            row = result.first()
        finally:
            await conn.execute(text("ALTER TABLE firms FORCE ROW LEVEL SECURITY"))
    if not row or not row[1]:
        print("No ciphertext present")
        return
    firm_id, ciphertext = row
    plaintext = decrypt_str(bytes(ciphertext), firm_id=str(firm_id))
    print(f"Decrypt OK. plaintext_len={len(plaintext)} first_8_chars={plaintext[:8]!r}")
    await engine.dispose()


asyncio.run(main())
```

Run with the production env loaded:

```
sudo -u coworker bash -c "set -a; source /opt/coworker/shared/credentials/coworker.env; set +a; cd /opt/coworker/current && .venv/bin/python /tmp/decrypt_probe.py"
rm /tmp/decrypt_probe.py
```

Expected: `Decrypt OK. plaintext_len=<N> first_8_chars='<first 8 chars>'`. If decrypt fails, stop. The encryption did not use the live master key; diagnose before letting users sign in (the OAuth callback will fail in the same way).

### 5. Confirm the Azure portal redirect URI

The firm's Azure app registration **Authentication** blade must include this URI under **Platform configurations → Web → Redirect URIs**:

```
https://coworker.mcands.com.au/api/v1/auth/microsoft/callback
```

(For licensed deployments with their own subdomain, substitute that domain.)

Under **Implicit grant and hybrid flows**, both ID-token and access-token checkboxes must be **unchecked** — we use authorization code flow, not implicit/hybrid.

### 6. First sign-in verification

The firm administrator (typically the first user) signs in via an **incognito browser**:

1. Navigate to `https://coworker.mcands.com.au/api/v1/auth/microsoft/start/<firm_slug>`.
2. Complete the Microsoft sign-in (MFA + consent if first time).
3. Confirm the redirect back to the post-login target succeeds and a session cookie is set.

Then verify in the database:

```
sudo -u postgres psql -d coworker -c "SELECT id, upn, display_name, role, azure_object_id FROM users WHERE firm_id = (SELECT id FROM firms WHERE slug = '<firm_slug>');"
sudo -u postgres psql -d coworker -c "SELECT id, action, actor_type, actor_id FROM audit_log WHERE firm_id = (SELECT id FROM firms WHERE slug = '<firm_slug>') ORDER BY occurred_at DESC LIMIT 5;"
```

Expected: one user row with the administrator's UPN and a real `azure_object_id` (GUID from Microsoft); one `auth.callback.success` audit entry whose `actor_id` matches the user row's `id`.

### 7. Promote the first user to `owner`

The OAuth callback creates every new user with `role = 'accountant'`. This is the correct default for normal staff but means the **firm's first user — the administrator — needs a one-row promotion** before any owner-gated endpoint will accept them.

Use a small Python one-shot rather than raw SQL so the audit chain stays intact (raw `UPDATE` + raw audit `INSERT` would break the `prev_hash` linkage; the canonical `append_audit` helper acquires the per-firm advisory lock and computes the chain hash correctly):

```python
# /tmp/promote_owner.py — delete after running
import asyncio
import uuid

from sqlalchemy import select

from coworker.db.models.tenancy import User
from coworker.db.session import SessionLocal, firm_context
from coworker.security.audit import append_audit

USER_ID = uuid.UUID("<user-uuid-from-step-6>")
FIRM_ID = uuid.UUID("<firm-uuid>")
PREVIOUS_ROLE = "accountant"
NEW_ROLE = "owner"


async def main() -> None:
    async with SessionLocal() as session, firm_context(FIRM_ID):
        result = await session.execute(select(User).where(User.id == USER_ID))
        user = result.scalar_one()
        if user.role == NEW_ROLE:
            print(f"already {NEW_ROLE}, nothing to do")
            return
        if user.role != PREVIOUS_ROLE:
            raise RuntimeError(
                f"unexpected current role {user.role!r}; refusing to overwrite"
            )
        user.role = NEW_ROLE
        await session.flush()
        entry = await append_audit(
            session,
            firm_id=str(FIRM_ID),
            actor_type="system",
            actor_id="cli:bootstrap-owner-promote",
            action="user.role_changed",
            target_type="user",
            target_id=str(USER_ID),
            payload={
                "previous_role": PREVIOUS_ROLE,
                "new_role": NEW_ROLE,
                "rationale": "first-user post-bootstrap admin promotion",
            },
        )
        await session.commit()
        print(f"promoted; audit_log id={entry.id}")


asyncio.run(main())
```

Run it:

```
sudo -u coworker bash -c "set -a; source /opt/coworker/shared/credentials/coworker.env; set +a; cd /opt/coworker/current && .venv/bin/python /tmp/promote_owner.py"
rm /tmp/promote_owner.py
```

Then verify:

```
sudo -u postgres psql -d coworker -c "SELECT id, upn, role FROM users WHERE id = '<user-uuid>';"
sudo -u postgres psql -d coworker -c "SELECT id, action, actor_type, actor_id, target_id FROM audit_log WHERE firm_id = '<firm-uuid>' ORDER BY id;"
```

Expected: `role = owner`; two audit rows now exist for the firm — `auth.callback.success` followed by `user.role_changed`.

### 8. Onboard the remaining staff

Each additional staff member signs in via the same `/api/v1/auth/microsoft/start/<firm_slug>` URL. Their user rows are auto-created on first callback with `role = 'accountant'`. No further bootstrap is required unless they also need elevated roles (`principal`, etc.) — in which case repeat step 7 with the appropriate role.

## Azure App Registration (one-time setup per firm)

The firm administrator does this in `portal.azure.com` before the bootstrap CLI runs:

1. Sign in as the firm's tenant administrator.
2. **Entra ID → App registrations → New registration**.
3. Name: `MCS CoWorker` (or whatever brand the firm prefers).
4. Supported account types: **Accounts in this organizational directory only** (single tenant).
5. Redirect URI: select **Web**, then enter the URI from step 5 above. (Do not use the SPA platform; the backend is a confidential client.)
6. After creation, copy the **Application (client) ID** and **Directory (tenant) ID** from the Overview blade — these are the GUIDs for the CLI flags.
7. **Certificates & secrets → New client secret**. Copy the **Value** column immediately (it is only shown once). Note the expiry date for rotation tracking.
8. **API permissions → Add permission → Microsoft Graph → Delegated**:
   - `User.Read`
   - `Mail.Read`
   - `Mail.Send`
   - `Files.Read.All`
   - Plus any others matching the firm's actual scope. Click **Grant admin consent for <tenant>**.
9. **Authentication blade**: confirm under **Implicit grant and hybrid flows** that both checkboxes are unchecked.

These values then go into the `bootstrap-firm` command in step 2.

## Security notes

- The client secret is stored as AES-256-GCM envelope ciphertext in `firms.azure_client_secret_ciphertext`, using `MASTER_ENCRYPTION_KEY` as the KEK and a per-row freshly generated DEK. The firm's UUID is the AEAD associated data, so a ciphertext stolen from one row cannot be decrypted against a different `firm_id`.
- **Rotation:** when the firm rotates their client secret, rerun `bootstrap-firm` with the new value. The CLI overwrites the ciphertext in place.
- Decryption only happens inside the OAuth callback request lifecycle; the plaintext never persists outside that scope.
- The `MASTER_ENCRYPTION_KEY` itself is a 32-byte AES key, base64-encoded in the env file. Keep both `/opt/coworker/shared/credentials/coworker.env` and `/opt/coworker/current/.env` aligned during deploys (the deploy script overwrites the latter from the former on each release).
- The `bootstrap-firm` CLI runs as the `coworker` system user with the production env loaded; do not run it as your own user shell since `ANTHROPIC_API_KEY` and other unrelated secrets in your shell environment will leak into the process environment of any subprocesses.
