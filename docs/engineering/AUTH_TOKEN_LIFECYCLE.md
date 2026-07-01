# Auth Token Lifecycle (Phase 3 Slices α2a + α2b)

Status: implemented in α2a (register + login) and α2b (refresh + logout).
Version: `v0.4.2-phase3-alpha2b`.

This document is the operational spec for how access + refresh tokens
are minted, rotated, revoked, and inspected across the auth surface.
It is deliberately more concrete than the API contract: it names the
JWT claims, the DB columns, and the invariants that every use case in
`app/application/use_cases/auth/` upholds.

## 1. Concepts

| Term | Meaning |
| --- | --- |
| **Session** | One row in `sessions`. Represents a single "live" refresh token. Identified by `id` (UUID). Belongs to a **family** and a **user**. |
| **Family** | A chain of sessions produced by rotation from a single login. Identified by `family_id` (UUID). Login mints a new family; refresh preserves the family. Reuse detection revokes the entire family in one shot. |
| **Access token** | Short-lived JWT (`kind=access`, default 15 min). Carries `sub` (user id), `sid` (session id), `fam` (family id), `iat`, `exp`. Presented as `Authorization: Bearer …` on protected endpoints. |
| **Refresh token** | Long-lived JWT (`kind=refresh`, default 30 days). Same claim shape as the access token. Its **SHA-256 hash** — not the raw JWT — is stored in `sessions.token_hash` so a database dump does not equal a session hijack. |

Both token kinds share the same signing secret and the same `sid`/`fam`
identity: an access token and its sibling refresh token both refer to
the same session row.

## 2. Session state machine

```
                              ┌────────────────────────────┐
                              │                            │
                              ▼                            │
 ┌─────────────┐   login   ┌────────┐   refresh   ┌───────┴───────┐
 │  (no row)   │ ────────▶ │  LIVE  │ ──────────▶ │ REVOKED (old) │
 └─────────────┘           └───┬────┘             └───────────────┘
                               │       ┌──────────────────────────┐
                               │       │ replay of any old token  │
                               │       │        in family         │
                               │       ▼                          │
                               │ ┌────────────────────────────┐   │
                               │ │  ENTIRE FAMILY → REVOKED   │   │
                               │ └────────────────────────────┘   │
                               │                                  │
                       logout  ▼                                  │
                        ┌──────────────┐                          │
                        │   REVOKED    │                          │
                        └──────────────┘                          │
                                                                  │
                          (rotated child) ─────────────────────────
```

A session row is **LIVE** iff `revoked_at IS NULL` **and**
`expires_at > now()`. Every state transition to REVOKED goes through
`ISessionRepository.revoke`, which is a compare-and-swap: only the
first caller that observes `revoked_at IS NULL` succeeds.

## 3. Endpoint flows

### 3.1 `POST /auth/register` — new user + new family

```
Client                 API                     DB
  │  {email,pw,name}    │                       │
  │────────────────────▶│                       │
  │                     │ INSERT users          │
  │                     │────────────────────  ▶│
  │                     │ INSERT tenants        │
  │                     │────────────────────  ▶│
  │                     │ INSERT sessions       │
  │                     │  (new family_id)      │
  │                     │────────────────────  ▶│
  │  201 {access,       │                       │
  │       refresh}      │                       │
  │◀────────────────────│                       │
```

### 3.2 `POST /auth/login` — existing user, **new family**

Each login mints a fresh `family_id`. Two devices logging in from the
same account get independent families, so a rotation on device A never
affects device B. See `test_login_two_devices_creates_distinct_families`.

### 3.3 `POST /auth/refresh` — rotate within a family

```
Client                 API                        DB
  │ {refresh_token}      │                         │
  │─────────────────────▶│ verify_refresh(jwt)     │
  │                      │ hash = sha256(jwt)      │
  │                      │ SELECT sessions         │
  │                      │  WHERE token_hash = h   │
  │                      │─────────────────────── ▶│
  │                      │                         │
  │                      │ if row.revoked_at:      │
  │                      │   revoke_family(); 401  │
  │                      │                         │
  │                      │ if row.id != jwt.sid:   │
  │                      │   401 (A12)             │
  │                      │                         │
  │                      │ users.get_by_id()       │
  │                      │─────────────────────── ▶│
  │                      │ if user is None:        │
  │                      │   revoke row; 401       │
  │                      │                         │
  │                      │ UPDATE sessions         │
  │                      │  SET revoked_at = now   │
  │                      │  WHERE id = row.id AND  │
  │                      │        revoked_at IS    │
  │                      │        NULL             │
  │                      │─────────────────────── ▶│
  │                      │ INSERT new session      │
  │                      │  (same family_id)       │
  │                      │─────────────────────── ▶│
  │ 200 {access,         │                         │
  │      refresh}        │                         │
  │◀─────────────────────│                         │
```

### 3.4 `POST /auth/logout` — terminate a session

```
Client                                 API                       DB
  │ Authorization: Bearer <access>       │                        │
  │─────────────────────────────────────▶│ verify_access(         │
  │                                      │   token,               │
  │                                      │   allow_expired=True)  │
  │                                      │ UPDATE sessions        │
  │                                      │  SET revoked_at = now  │
  │                                      │  WHERE id = jwt.sid    │
  │                                      │    AND revoked_at      │
  │                                      │        IS NULL         │
  │                                      │──────────────────────▶ │
  │ 204 No Content (always, if verify OK)│                        │
  │◀─────────────────────────────────────│                        │
```

**Design point (documented prominently because it surprises people):**
logout accepts an *expired* access token. The user's intent is "I am
done" — forcing them to refresh first before they can log out defeats
the purpose. Signature + `kind == 'access'` + presence of `sid` are
still strictly enforced; only `exp` is relaxed. See
`LogoutSession.execute` docstring for the threat-model analysis.

Logout is **idempotent**: second and subsequent calls with the same
`sid` also return 204, and the original `revoked_at` timestamp is
preserved (the CAS clause `WHERE revoked_at IS NULL` blocks the
second update). Auditors always see the authoritative "logged out at"
timestamp; there is no way for a client to overwrite it later.

## 4. Refresh Family Example

Suppose a user logs in once and refreshes twice:

```
Family F1
─────────
        login
  ────────────────▶  S1 (LIVE)          issued_at = t0
                         │
                         │ refresh(R1)
                         ▼
                     S1 (REVOKED)       revoked_at = t1
                     S2 (LIVE)          issued_at = t1
                         │
                         │ refresh(R2)
                         ▼
                     S2 (REVOKED)       revoked_at = t2
                     S3 (LIVE)          issued_at = t2
```

Now the attacker replays `R1` (which they intercepted at t0):

```
                         │ refresh(R1)   ⚡ REUSE
                         ▼
                     S1 stays REVOKED (already was)
                     S2 → REVOKED       ⚡ family sweep
                     S3 → REVOKED       ⚡ family sweep
                     (401 returned)
```

Every member of F1 is now dead. The user's legitimate active client,
holding `R2`, discovers this on its next refresh attempt (also 401)
and has to log in again — but no attacker action can go undetected
past the next refresh cycle, because the attacker's replay of `R1`
guaranteed the family was nuked.

## 5. Invariants

Every auth use case upholds these; the property tests in
`test_refresh_session.py` and `test_logout_session.py` enforce them
in unit form, and the integration tests in `test_auth.py` enforce
them end-to-end.

1. **Refresh token secrecy.** The raw refresh JWT never round-trips
   through the DB; only its SHA-256 is stored.
2. **CAS revocation.** `revoked_at` is set exactly once per session
   row. The first `revoke` wins; every subsequent one is a no-op.
   Audit tooling can therefore trust `revoked_at` as authoritative.
3. **Rotation preserves the family; login mints a new one.** A
   compromised refresh token cannot cross family boundaries.
4. **Reuse detection is fatal to the whole family.** No partial
   recovery; better UX would break the invariant.
5. **`sid` in the JWT matches `id` in the row.** Defence in depth; a
   mismatch is logged with `security_event=True`.
6. **Access token compromise does not compromise the refresh chain.**
   `/logout` accepts a stolen access token to terminate the session,
   but the attacker cannot mint fresh tokens without the refresh JWT.

## 6. Structured logs

For SIEM / alerting integrations:

| Log event | Level | `security_event` | Emitted by |
| --- | --- | --- | --- |
| `auth.refresh.rotated` | info | — | `RefreshSession` on happy path |
| `auth.refresh.rejected` | warn | `True` iff reason ∈ {`hash_miss`, `sid_mismatch`} | `RefreshSession` |
| `auth.refresh.reuse_detected` | warn | `True` | `RefreshSession` on family sweep |
| `auth.logout.succeeded` | info | — | `LogoutSession` on happy path (incl. idempotent no-op) |
| `auth.logout.rejected` | warn | — | `LogoutSession` on verify failure |

Ops teams should page on `security_event=True` — those are the events
that indicate genuine attacker activity, not routine noise from
expired tokens or client retries.
