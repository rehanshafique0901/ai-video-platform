# Backup & Restore Strategy

> Operational commitments for database durability and recoverability. Drills are mandatory; an untested backup is not a backup.

---

## 1. Recovery Objectives

| Metric | Target |
|---|---|
| **RPO** (Recovery Point Objective) | ≤ 1 minute |
| **RTO** (Recovery Time Objective) | ≤ 30 minutes for prod, ≤ 4 hours for staging |
| **Backup retention (hot)** | 30 days of full + WAL |
| **Backup retention (cold)** | 365 days of monthly fulls |
| **Cross-region replicas** | At least one (read-only, async) |
| **Point-in-Time Recovery window** | 30 days |

These targets are revisited each phase; current targets are appropriate through M1 (Public Beta) per `ROADMAP.md`.

---

## 2. Topology

```
                ┌──────────────────────────┐
                │ Primary Postgres 15      │
                │ (region A — write)       │
                └───────────┬──────────────┘
                            │ streaming replication (sync)
                            ▼
                ┌──────────────────────────┐
                │ Standby (region A)       │
                │ — promotion target       │
                └───────────┬──────────────┘
                            │ async logical / physical
                            ▼
                ┌──────────────────────────┐
                │ Read replica (region B)  │
                │ — DR + read offload      │
                └──────────────────────────┘

       ┌────────────────────────────┐
       │  Object Storage (R2 / S3)  │
       │  WAL archive + base backup │
       │  Versioned, encrypted      │
       └────────────────────────────┘
```

- **Synchronous replication** to a same-region standby (low-latency).
- **Asynchronous replication** to a cross-region read replica (DR).
- **WAL archive** continuously shipped to object storage (`archive_command`).

---

## 3. Backup Mechanisms

### 3.1 Continuous WAL Archiving

- `wal_level = replica`
- `archive_mode = on`
- `archive_command` ships every completed WAL segment to `r2://prod-pg-wal/<cluster>/`
- WAL segments retained for 30 days hot, then deleted (cold storage is base + 30 days WAL only).

### 3.2 Daily Physical Base Backups

- `pg_basebackup --format=tar --gzip --checkpoint=fast` runs nightly at 02:00 UTC.
- Stored at `r2://prod-pg-base/<cluster>/<YYYY-MM-DD>/`.
- Retention: 30 daily, 12 monthly, 5 yearly (GFS).
- Each backup is checksummed and the checksum is stored separately.

### 3.3 Logical Backups

- `pg_dump --format=custom --jobs=8` runs **weekly** for schema + key reference data (plans, feature flags, AI models).
- Used for spinning up scratch environments and as a last-resort restore path.

### 3.4 Object Storage Backups

- The R2 / S3 buckets storing media assets and library assets carry their own versioning and lifecycle (separate from DB backups).
- Each object stores its DB-side checksum (`media_assets.checksum_sha256`); a periodic job verifies the round-trip.

---

## 4. Restore Procedures

### 4.1 Point-in-Time Recovery (PITR)

```
1. Provision a fresh Postgres host.
2. Restore the most-recent base backup older than the target time.
3. Configure recovery to replay WAL up to the target time (`recovery_target_time = '…'`).
4. Promote.
5. Run integrity verification job (see §6).
6. Cut over: update DNS / connection-string secret.
```

Tested manually quarterly and automated in staging weekly.

### 4.2 Full Cluster Restore (Disaster Recovery)

```
1. Promote the cross-region read replica to primary.
2. Apply any unshipped WAL from the object archive (if available).
3. Verify integrity.
4. Update DNS.
5. Provision a new replica in the original region once it is healthy.
```

### 4.3 Single-Table or Single-Tenant Restore

When a partial restore is needed (corrupted table, accidental tenant deletion):

```
1. Stand up an isolated scratch cluster from the most-recent base backup.
2. PITR to just before the corruption.
3. `pg_dump --table=<t>` or `--data-only` for the relevant rows.
4. Apply into prod with a wrapping migration (so the restore is auditable).
```

The full procedure is rehearsed once per quarter against a real backup.

### 4.4 Logical Restore (Last Resort)

If WAL + base is unrecoverable: restore the latest `pg_dump`. Acknowledged data loss = up to 1 week. Requires post-mortem + customer communication.

---

## 5. Encryption & Access

- All backups encrypted at rest (object-storage SSE with customer-managed key).
- WAL is also gzip-compressed before encryption to reduce egress.
- Restore credentials live in the secret manager; access is audited.
- The backup bucket is in a **separate cloud account** from production to prevent compromise propagation.

---

## 6. Integrity Verification After Restore

Mandatory post-restore checks:

1. `SELECT count(*) FROM credit_ledger;` matches the expected count (recorded at backup time in a sentinel table `_backup_sentinel`).
2. `verify_credit_ledger_integrity` job (see `RETENTION_POLICY.md` §6) passes.
3. `verify_immutable_tables_no_changes` job passes.
4. A canary record present in `_backup_sentinel` (written at backup time) is visible.
5. `pg_amcheck` on every relation reports zero corruption.
6. Vacuum + analyze run on the restored cluster before traffic is switched.

The sentinel table is created in the baseline migration. The implemented
column shape (matches `app/infrastructure/db/models/sentinel.py`) is:

```
_backup_sentinel (
  id uuid PK,
  inserted_at timestamptz NOT NULL DEFAULT now(),   -- when the canary row was written
  label text NOT NULL,                              -- short label (e.g. backup id, drill id)
  notes text                                        -- optional free-form annotation
)
```

> Phase 2D documentation reconciliation: the original draft of this section
> proposed columns `taken_at` and `marker`. The implementation chose
> `inserted_at` / `label` / `notes` (label can hold the random per-backup
> marker; `notes` carries any human-readable backup metadata). This
> document was updated to match the shipped schema; no migration was
> performed.

---

## 7. Drill Schedule

| Drill | Frequency | Where |
|---|---|---|
| PITR to 12 hours ago | Weekly | staging |
| PITR to 7 days ago | Monthly | staging |
| Full DR promotion | Quarterly | DR region (read replica promoted, then demoted) |
| Single-tenant restore | Quarterly | scratch cluster |
| Logical (`pg_dump`) restore | Bi-annual | scratch cluster |
| Encryption-key rotation | Annual | all environments |

Drill outcomes are recorded in `docs/database/drill-log.md` (Phase 9 deliverable).

---

## 8. Migration Safety Interaction

- Every Alembic migration runs against a **restored copy of prod** in staging before being applied to prod.
- Migrations are reversible (`downgrade()` mandatory) except those documented as destructive (rare; require ADR).
- Long migrations (> 5 min) are split into chunks and use `CREATE INDEX CONCURRENTLY` / `ALTER TABLE … DETACH … CONCURRENTLY`.

---

## 9. Monitoring & Alerting

| Metric | Alert threshold |
|---|---|
| WAL archive lag | > 60 seconds |
| Replication lag (sync standby) | > 5 seconds |
| Replication lag (async DR) | > 5 minutes |
| Most-recent base backup age | > 26 hours |
| Backup verification failure | any |
| Restore drill pass rate | < 100% rolling 90 days |

Alerts feed into the on-call rotation.

---

## 10. Customer-Visible Commitments

For inclusion in the SaaS Terms (legal review pending):

- **Data durability:** ≥ 99.999999999% (eleven nines) annualised for committed records.
- **Recovery from disaster:** ≤ 30 minutes RTO, ≤ 1 minute RPO.
- **Data export on request:** within 7 days for any active subscriber.
- **Data deletion on account closure:** complete within 30 days (Class A/B) with a signed receipt.

These commitments map directly to the mechanisms above and to `RETENTION_POLICY.md`.

---

## 11. Open Questions Carried to Step B Review

1. Do we use a managed Postgres (RDS / Cloud SQL / Crunchy / Neon) or self-hosted with `pgbackrest`? *(Both supported; default for v1: managed.)*
2. Confirm separate-account backup bucket — which secondary cloud provider for cross-cloud redundancy?
3. Should `agent_memory` long-term embeddings be backed up nightly or excluded from backups (re-derivable)?
