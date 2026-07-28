# α9.4 — Multi-Destination Publishing — Grounding (read-only)

> **Status:** Read-only discovery grounding. **Facts only** — no implementation. Every claim
> below is grounded in a named file / port / table / event / contract section.
>
> **Baseline:** `v0.4.46-phase3-alpha9.3` (frozen). Selected from
> [`NEXT_VERTICAL_SLICES_DISCOVERY.md`](./NEXT_VERTICAL_SLICES_DISCOVERY.md) **§2.5** — the
> highest-value **completable, additive, low-external-risk** slice remaining after the entire
> discovery top-5 shipped (α8.8 / α8.9a / α8.9b / α9.1 / α9.2) plus α8.9c, α9.0, α9.3.

---

## 1. Selection rationale (why this slice, why now)

The whole publishing investment to date makes a **single publish richer** — deterministic
metadata (α8.6b), a real destination (α8.6c YouTube), notifications (α8.9a), platform-native
scheduling (α8.9b), AI captions (α9.1), and a custom thumbnail (α9.3). The natural next step to
maximise **reach** is one of:

- **§2.6 additional destinations (TikTok/Instagram)** — highest raw product value, but the
  dominant risk is **external** (platform app-review / content-publishing API approval, native
  OAuth without a UI). **Not completable in-repo** → would stall. Deferred.
- **§2.5 multi-destination fan-out** — publish **once** to **all** the creator's connected
  channels. **Completable in-repo, additive, no external dependency**, and it **compounds every
  prior publishing slice** (captions + thumbnail + schedule composed once → applied to N
  channels). **Selected.**

All other discovery candidates are either shipped, external-dependent (§2.7 email/SMTP, §2.18
Stripe billing), ADR-gated greenfield (§2.13 templates' `body` contract, §2.15 team/RBAC,
§2.16 brand kits), quality-not-capability (§2.11/§2.12 verify/repair V2), or blocked/very-large
(§2.14 creator-flow, depends on §2.10 which shipped as α8.8 but the full funnel is large).

---

## 2. Decisive grounding facts

**F1 — N-per-export-to-different-accounts is ALREADY permitted.** Publishing idempotency is keyed
on `(source_media_asset_id, social_account_id)` — `CreatePublishJob.get_active(...)`
(`create_publish_job.py:132`) + the partial-unique backstop (PUB-7, migration `0014`). So two
`PublishJob`s for the **same export** to **different accounts** are distinct, legal rows today.
**N sequential `POST /api/v1/publish-jobs` calls already achieve multi-destination.** The slice
adds the missing **single creator action**, not a new capability in the runtime.

**F2 — Multiple accounts per `(user, platform)` are a v1 guarantee.**
`PUBLISHING_RUNTIME_CONTRACT.md` §9 / Q3: uniqueness is `(user_id, platform, external_account_id)`
— "a creator may connect several YouTube channels or personal + business accounts." So fan-out has
real value **even with only YouTube connected** (multiple channels), and becomes multiplicative
the moment §2.6 adds platforms.

**F3 — `CreatePublishJob` is already a clean, reusable per-account unit.**
`CreatePublishJob.execute(...)` (`create_publish_job.py:73`) takes a single `social_account_id`
plus the shared inputs (`export_job_id`, `title/description/tags/visibility`, `publish_at`,
`thumbnail_media_asset_id`) and returns `CreatePublishJobResult{job, created}`. It already:
owner-gates the account (404) + readiness (422), resolves the export source **once per call**,
validates the thumbnail (404/422), builds the deterministic `ContentPackage`, inserts, and emits
`PublishJobCreated`. **A fan-out is a loop over this exact use case, one account at a time.**

**F4 — Serialisation + execution are unchanged.** Each created job is claimed by the existing
`PublishWorker`/`ProcessPublishJob` under the dual lock (`publish_job:<id>` then
`project_publish:<project_id>`, Q2/DQ5); the `project_publish` lock already serialises multiple
jobs of the same project. **No runtime change is needed** — fan-out only affects *creation*.

**F5 — Downstream fan-out already generalises.** Notifications (α8.9a) + analytics (α9.0) consume
the terminal `PublishJob*` events per job; N jobs → N independent notifications/analytics rows,
already correct with **no change**.

**F6 — No schema is required for pure fan-out.** Each destination is an independent existing
`publish_jobs` row. A grouped/batch identity (`publish_batch_id`) would need a migration + a
`PublishBatch` concept — **explicitly out of scope** for this slice (status is already available
per-job via `GET /publish-jobs/{id}` and `GET /publish-jobs`).

**F7 — The response/envelope + owner-scoping seams are reusable.** `envelope(...)`
(`api/v1/helpers.py`), `CurrentUserDep`, and `PublishJobPublic` / `_to_public`
(`routers/publish_jobs.py:40`) already exist; the fan-out returns a **list** of the same DTO plus
a per-account outcome.

---

## 3. Missing pieces (what the slice adds)

- *Domain:* **none** (`PublishJob` / `ContentPackage` / `SocialAccount` unchanged).
- *Application:* a thin **`CreatePublishJobs` (plural) fan-out orchestrator** that resolves the
  **shared** inputs once (export readiness, thumbnail ownership), then invokes the existing
  per-account create logic for each `social_account_id`, collecting a per-account result. No new
  port; no new repository method (reuses `CreatePublishJob`'s repository calls).
- *Infrastructure:* **none**.
- *API:* a **new additive endpoint** for the batch action (the existing single-create endpoint is
  untouched — backward compatible), returning per-account outcomes.
- *Persistence:* **none** (no migration — F6).
- *Testing:* unit (fan-out orchestration: mixed success/replay/per-account error, metadata-once)
  + integration (Stage: one action → N real `publish_jobs` rows, worker drains all).

---

## 4. Architectural-decision check → **NO NEW ADR REQUIRED**

Applying the stop test ("a genuine architectural decision" / "a frozen boundary would be
crossed"):

| Question | Finding |
|---|---|
| Frozen path touched? | **No** — ADR-0042/0043/0044/0045/0046/0047 paths untouched; publishing runtime + adapters + `ContentPackage` unchanged. |
| New bounded context / plane? | **No** — same Publishing context. |
| New port / DTO boundary change? | **No** — reuses `CreatePublishJob` + existing DTOs; only an additive request/response shape. |
| Migration / schema change? | **No** (pure fan-out, F6). |
| New failure semantics vs PUB-11 / idempotency? | **No** — each job is the *existing* publish path; per-account idempotency (PUB-7) and PUB-11 apply unchanged. |
| Cross-context dependency (e.g. AI, credentials)? | **No.** |

The only genuinely new choices are **API-contract / product** decisions (all-or-nothing vs
best-effort fan-out; validate-shared-first; response shape; ordering; a sane `social_account_ids`
cardinality cap). These are the same *class* of decision resolved in the α8.9b / α9.3 pre-flights
**without** an ADR. Per `PUBLISHING_RUNTIME_CONTRACT.md` §2 (v1 scope = "one destination") this is
a **scope expansion documented by a small contract addendum**, not an architectural boundary
change — an addendum is documentation, not an ADR.

**Conclusion:** No new ADR. Continue automatically into the pre-flight.

---

## 5. Open questions for the pre-flight to rule (not architectural)

1. **Failure model** — all-or-nothing vs **best-effort per account** (recommend best-effort:
   validate the *shared* inputs once → fail fast 404/422; then per-account outcomes so one bad
   account never blocks the rest).
2. **Endpoint shape** — dedicated `POST /api/v1/publish-jobs/batch` vs a plural field on the
   existing endpoint (recommend a dedicated additive endpoint to keep the single-create contract
   pristine).
3. **Response shape** — a `data` array of `{ social_account_id, publish_job?, created, error? }`
   items under the standard envelope; overall `201`.
4. **Cardinality cap** — a bounded `social_account_ids` list (dedupe; reject empty; cap length).
5. **Metadata-once** — the shared `ContentPackage` inputs are built per-account from identical
   inputs (deterministic — PUB-9), so all N jobs carry equivalent metadata by construction.
