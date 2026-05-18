# ADR-0002: production runs plain EnvironmentFile, not age (deferred)

## Status

Accepted, 2026-05-18. Effective immediately for all `coworker-*`
systemd units. Defers — but does not abandon — the age-encrypted
credential design described in the architecture doc §2.3.
Supersedes the implicit "production uses age-encrypted env" posture
that the repo's `infra/systemd/` unit files and §2.3 jointly
implied through Phase 2. The architecture doc §2.3 is updated
separately to cross-reference this ADR.

## Context

The repo `infra/systemd/coworker-*.service` units prescribed
age-encrypted credentials via the systemd
`LoadCredentialEncrypted=` / `ExecStartPre=age -d …` /
`EnvironmentFile=/run/credentials/<unit>/coworker.env` pattern,
backed by `/etc/coworker/age.key` (the host key) and
`/opt/coworker/secrets/production.env.age` (the encrypted env
file). This matched §2.3 of the architecture doc.

The droplet's actual state diverged silently:

- `/usr/local/bin/age` is installed.
- `/etc/coworker/age.key` does **not** exist.
- `/opt/coworker/secrets/` does **not** exist at all — no
  encrypted env, no parent directory.
- `/opt/coworker/shared/credentials/coworker.env` does exist
  (`root:coworker`, mode `0640`, plaintext) and is the file
  `coworker-api` actually reads.
- The deployed `/etc/systemd/system/coworker-api.service` was
  hand-edited during the v3.0.0 ship to drop the
  `LoadCredentialEncrypted` / `ExecStartPre` lines and point
  `EnvironmentFile=` at the plaintext file. The repo unit was
  never updated to match.
- The 6 sibling units (`coworker-worker`, `-dispatch`,
  `-scheduler`, `-subscribe`, `-backfill`, `-delivery-confirm`)
  were installed from the repo unit files (still on the age form)
  and have therefore never been able to start — `age -d` against
  a non-existent key file fails at `ExecStartPre`. They are
  enabled-equivalent installed-but-disabled and have not run
  since Phase 11 / Phase 13 introduced them.

Production has run on this divergent posture since the v3.0.0
cut on 2026-05-01 without incident, because only the api unit was
ever started and it was the one that got hand-edited.

This ADR records the deliberate decision to make the repo, the
deployed units, and the architecture doc agree on the
plain-`EnvironmentFile` form — rather than build out the age
machinery now to match the repo's pre-existing prescription.

## Decision

All `coworker-*` systemd units — `coworker-api.service`,
`coworker-worker.service`, the 5 timer-activated oneshots
(`coworker-dispatch`, `-scheduler`, `-subscribe`, `-backfill`,
`-delivery-confirm`), and the 5 corresponding timers — use a
single plain `EnvironmentFile=` pointing at a root-owned,
`coworker`-group-readable plaintext file:

```ini
[Service]
User=coworker
Group=coworker
WorkingDirectory=/opt/coworker/current/backend
EnvironmentFile=/opt/coworker/shared/credentials/coworker.env
ExecStart=/opt/coworker/current/.venv/bin/<binary> …
```

File ownership and mode:

```
-rw-r----- root:coworker /opt/coworker/shared/credentials/coworker.env
                         (mode 0640)
```

No `LoadCredentialEncrypted=`, no `ExecStartPre=`, no
`/run/credentials/<unit>/` path. The repo `infra/systemd/`
sibling units have been rewritten in this same change to match
the working api unit byte-for-byte across the five canonical
directives (`User=`, `Group=`, `WorkingDirectory=`,
`EnvironmentFile=`, `ExecStart=` interpreter prefix).

## Rationale

The age-encrypted path's safety property is "compromise of the
deployed filesystem alone does not yield the master key" — i.e.
encryption at rest. That property only holds if the host key
`/etc/coworker/age.key` is itself recoverable from somewhere that
isn't the same droplet: a backup, an offline copy, a key
custodian, a KMS. The Phase 14 key-backup tooling that would
provide that recoverability is **not yet built**.

Provisioning `/etc/coworker/age.key` now, on a host with no
backup of the key, trades a survived risk (plaintext env on a
locked-down droplet, which has been the production posture
without incident since v3.0.0) for a new and unmitigated risk
(an irretrievable key on a single host — droplet loss now means
the encrypted env is also lost, with no recovery path). That is
a worse posture than the one it would replace.

The repo and §2.3 currently lie about production. The two
non-lying options are: (a) build the age machinery + key-backup
tooling now and bring everything onto it, or (b) record the
plain-env reality and align the artifacts to it. Option (b) is
cheap, reversible, and unblocks the systemd fleet (worker + 5
timers) immediately. Option (a) is in scope for Phase 14
regardless; doing it now would be Phase 14 work brought forward
under deploy-reconciliation pressure, which is the wrong reason
to take on irreversible key material.

## Consequences

- **Protection of the master key is now filesystem permissions +
  droplet hardening, not encryption at rest.** The
  `coworker.env` file is `root:coworker 0640`, readable only by
  the application user and writable only by root. The droplet
  is ufw-firewalled, ssh is key-only on a non-standard port, and
  the systemd hardening directives (`ProtectSystem=strict`,
  `NoNewPrivileges=true`, `PrivateTmp=true`, etc.) remain on
  every unit. This is the same protection envelope the api unit
  has been running under since v3.0.0.
- **The deployment story is one mechanism, not two.** Every
  `coworker-*` unit reads the same env file via the same
  directive. No per-unit `/run/credentials/<unit>/coworker.env`
  decryption side-channel to debug, no
  `LoadCredentialEncrypted` failure mode that masks a missing
  key as a service crash.
- **Backup posture changes.** The plaintext `coworker.env` must
  be included in encrypted backups (Phase 14) — losing the
  droplet without that backup means re-issuing every secret in
  the file. Today this is the same exposure the api unit
  already has; the ADR just makes it explicit and the same for
  all units.
- **Repo, deployed units, and architecture doc §2.3 now agree.**
  The drift this ADR records is exactly the drift this ADR
  closes.
- **The 6 sibling services and 5 timers become startable.** The
  failure mode that has prevented them from running
  (`ExecStartPre=age -d` against a non-existent key) is gone.

## Revisit trigger

Revisit and migrate to the age path when **either** of the
following becomes true, whichever first:

1. Phase 14 key-backup tooling exists — meaning
   `/etc/coworker/age.key` can be re-provisioned on a fresh host
   from an offline backup or KMS-managed source, with a tested
   restore path.
2. We onboard the first external client firm (the start of
   multi-firm distribution). Plaintext env at rest is acceptable
   for an internal MC & S deployment with single-firm scope; it
   is not the posture to ship to other firms' droplets.

At that point this ADR is superseded by an "ADR-NNNN: production
migrates to age-encrypted env" that introduces the key, the
encrypted file, and updates every unit. Until then the architecture
doc's §2.3 age design is the intended target, not the current
state.
