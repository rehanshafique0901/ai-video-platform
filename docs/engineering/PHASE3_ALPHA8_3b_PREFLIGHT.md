# Phase 3 · α8.3b — Webhook Completion Ingress — PRE-FLIGHT

- **Status:** SIGNED OFF (2026-07-22) — A2 · B1 · C1 · D approved; version `v0.4.24-phase3-alpha8.3b`; added invariant **W8.3b.1** + freeze-guard acceptance criterion
- **Slice:** α8.3b (the *thin second ingress* the α8.3 completion engine was designed for)
- **Target version (on implementation):** `0.4.24-phase3-alpha8.3b`
- **Predecessors:** α8.3 Completion Engine (`v0.4.23`), **ADR-0042 Orchestration Platform Freeze**
- **Contract source:** ADR-0041 **D7** (webhook lifecycle → same idempotent `complete()`), **D8** (`workflow_run:<id>` lease); ADR-0031 (idempotency-keys FSM); ADR-0042 (freeze — this slice must be **additive**)

---

## 0. One-paragraph thesis

α8.3 shipped the single idempotent `CompletionEngine.complete(project_id, workflow_run_id)` and the polling ingress. α8.3b adds the **second ingress** ADR-0041 D7 always intended: an authenticated HTTPS endpoint that a provider (Fal) calls when a job finishes, which — after signature verification and idempotent dedup — **triggers the exact same `complete()`**. The webhook is a *signal, not a source of truth*: it tells us "run X's job is done, look now," and `complete()` does the authoritative resolve through the already-frozen `resolve_job` path. That framing keeps α8.3b entirely **additive** to the ADR-0042 frozen surface — new router + new ingress use case + a provider-specific verifier in the (non-frozen) Fal adapter + one additive repository lookup — with **zero changes** to `CompletionEngine`, `ResumeWorkflowRun`, the runner, the dispatcher, or the provider protocol, and **zero migrations**.

---

## 1. Grounding (verified against current code)

### 1.1 The completion entrypoint we must reach
`completion_engine.py:109` — `async def complete(self, *, project_id: UUID, workflow_run_id: UUID) -> CompletionOutcome`. It acquires the `workflow_run:<id>` lease, reads the `_paused` handoff, calls `dispatcher.resolve_job(...)`, and delegates a terminal result to `ResumeWorkflowRun`. It is **idempotent and exactly-once** by construction (lease + `paused → running` CAS). **This is frozen (ADR-0042 §D1) and must not change.**

### 1.2 The lookup gap — `provider_job_id` is **not** a column
The webhook payload/headers give us the **Fal `request_id` = our `provider_job_id`**, but `complete()` needs `(project_id, workflow_run_id)`. Grounding shows `provider_job_id` is **not** a column on `workflow_runs` (`db/models/workflows.py:33–62`); it lives only inside `workflow_checkpoints.state["_paused"]["provider_job_id"]` (JSONB, `:98–113`). ⇒ α8.3b needs a **provider_job_id → run** resolution step. This is the central design decision (Fork A).

### 1.3 `webhook_deliveries` is **OUTBOUND** — do not use it here
Correction to the roadmap shorthand: `db/models/webhooks.py` (`WebhookDelivery`) models *outbound* deliveries **we send** (`destination_url`, `attempts`, `delivered_at`, `next_attempt_at`). It is **not** an inbound-receipt table. Inbound provider-webhook idempotency belongs in **`idempotency_keys`** (below), exactly as ADR-0041 D7 says.

### 1.4 `idempotency_keys` already fits inbound dedup (zero-migration)
`db/models/operations.py:29–68` — `IdempotencyKey(tenant_id, key, resource_type, resource_id, request_hash, response_hash, status, expires_at)` with **`UNIQUE(tenant_id, key, resource_type)`** and an FSM enum (ADR-0031). ⇒ A webhook receipt is `resource_type='webhook'`, `key=<fal request_id>`, `resource_id=<workflow_run_id>`, tenant-scoped. **No schema change required.**

### 1.5 Lease + CAS is the real exactly-once seam
`completion_engine.py:116–124` acquires `workflow_run:<id>`; `ResumeWorkflowRun` does the `paused → running` CAS. A webhook racing the poller (or a Fal retry racing itself) contends on the same lease, and the CAS is the backstop (ADR-0042 **G3**). ⇒ Webhook idempotency (`idempotency_keys`) is a **fast-path dedup + audit record**, not the correctness mechanism.

### 1.6 Fal's webhook scheme (verified against fal.ai docs, 2026)
- **Signature:** ED25519. Public keys from JWKS `https://rest.fal.ai/.well-known/jwks.json` (each key's `x` = base64url ED25519 pubkey). Cacheable, **≤ 24 h** (keys rotate).
- **Headers:** `X-Fal-Webhook-Request-Id`, `X-Fal-Webhook-User-Id`, `X-Fal-Webhook-Timestamp` (unix s), `X-Fal-Webhook-Signature` (hex). Missing any ⇒ invalid.
- **Replay guard:** reject if `|now − timestamp| > 300 s`.
- **Message to verify:** `"\n".join([request_id, user_id, timestamp, sha256_hex(raw_body)])`; verify detached ED25519 over the UTF-8 message against any JWKS key.
- **Body:** `{ "status": "OK" | "ERROR", "payload": {…}, "request_id": … }`. `X-Fal-Webhook-Request-Id` is the Fal `request_id` (= our `provider_job_id`).
- **Note (W8.1.1 nuance):** verification uses Fal's **public** keys fetched from a well-known endpoint — there is no shared secret to inject. This is a real deviation from the "credentials injected, never fetched" adapter pattern and needs an explicit call (Fork B).

### 1.7 Router + DI wiring pattern
Routers live in `app/api/v1/routers/` and are registered in `main.py:37–49`; use cases are built by `app/core/container.py` factories. A new `webhooks` router + a `get_receive_provider_webhook_use_case()` factory follow the existing pattern — all **new** files/surfaces (ADR-0042 growth surface).

---

## 2. Proposed shape (all additive to the ADR-0042 frozen surface)

```
POST /api/v1/webhooks/providers/{provider}         (new router — app/api/v1/routers/webhooks.py)
        │  raw body bytes + X-Fal-Webhook-* headers
        ▼
ReceiveProviderWebhook  (new ingress use case — use_cases/workflow/receive_provider_webhook.py)
        │  1. verify signature            → 401 on failure
        │  2. dedup (idempotency_keys)     → 200 "duplicate" short-circuit
        │  3. resolve provider_job_id → run (Fork A)
        │  4. CompletionEngine.complete(project_id, run_id)   ← FROZEN, unchanged
        ▼
CompletionEngine.complete → ResumeWorkflowRun → AdvanceWorkflowRun   (all frozen, untouched)
```

Provider-specific bits (signature scheme, header names, extracting `request_id`) live behind a small **`WebhookVerifier` port** implemented in the **non-frozen Fal adapter** (`providers/fal/webhook.py`), so the ingress use case stays provider-agnostic (mirrors the α8.3 `resolve` split; ADR-0042 **G4**).

---

## 3. Design decisions requiring sign-off

### Fork A — how the webhook resolves `provider_job_id → run` (the central one)
- **A1 — Scan `list_paused()` + match in Python.** Zero-migration; reuses the poller's read pattern; O(paused runs) per webhook. Simplest, but wasteful if many runs are paused.
- **A2 — Additive repo lookup `find_paused_by_provider_job_id(job_id)` (RECOMMENDED).** New method on `IWorkflowRunRepository`/impl (both **non-frozen** — ADR-0042 growth surface) querying `workflow_checkpoints.state #>> '{_paused,provider_job_id}'`. Targeted, still **zero-migration** for correctness. A supporting GIN/expression index is a *separate perf ADR* only if paused cardinality ever warrants it.
- **A3 — Embed `run_id` in the Fal callback URL at submit time.** ❌ Reject for α8.3b: requires the frozen `VideoProvider.submit()` protocol to accept a webhook URL (contract change → new ADR) and retrofits α8.2's poll-first submit. Exactly the kind of frozen-surface pressure ADR-0042 wants surfaced, not absorbed.

→ **Recommendation: A2.** Freeze-clean (repo layer isn't frozen), targeted, zero-migration. ✅ **SIGNED OFF.** The new method is documented as an **implementation detail of the repository**, *not* a new architectural contract (it exposes no orchestration semantics — just "find the paused run carrying this job id").

### Fork B — signature verification & the "no shared secret" nuance
- **B1 — Fetch + cache Fal JWKS via an injected `httpx` client (RECOMMENDED).** Mirror the α8.2 Fal-client composition: the container builds a pre-configured client + a `FalWebhookVerifier` (JWKS URL + cache TTL ≤ 24 h + 300 s timestamp tolerance from config). Matches Fal's documented, rotation-safe scheme.
- **B2 — Pin a static public key via config env.** Brittle across Fal key rotation; keep only as an offline/test override, not the primary path.
- **Crypto dependency:** prefer the already-present `cryptography` (`Ed25519PublicKey.from_public_bytes` + `.verify`) over adding PyNaCl, *if* it's already a dependency (JWT stack likely pulls it in — to confirm at implementation). Adding a dep is a small, explicit decision.

→ **Recommendation: B1 with `cryptography`.** ✅ **SIGNED OFF.** Documented **W8.1.1 clarification** (not a deviation): *"W8.1.1 applies to credentials and authentication material. Public verification keys obtained from a provider's JWKS endpoint are configuration-independent trust anchors and are therefore permitted."* This keeps the invariant intellectually clean — we fetch **public** verification keys, never secrets.

### Fork C — does the webhook trust the payload, or just trigger `complete()`?
- **C1 — Trigger-only (RECOMMENDED).** The webhook body is used **only** to find the run (via `request_id`); `complete()` then does the authoritative resolve through the frozen `resolve_job` (re-GETs Fal's `status_url`). Costs one extra Fal GET; needs **zero** frozen change and means a spoofed or malformed payload can never corrupt state (the authoritative resolve is unchanged).
- **C2 — Pass the delivered payload into resolve to avoid the re-fetch.** ❌ Requires `resolve_job`/`resolve` (frozen `ports.py`) to accept a webhook body → contract change → new ADR. Not worth one saved GET.

→ **Recommendation: C1.** This is the crux of staying additive. ✅ **SIGNED OFF** (strongly). "Something finished" — not "here's the result."

### Fork D — idempotency scope & HTTP semantics
- **DEFERRED (signed off, 2026-07-22): inbound webhook receipt persistence.** Grounding
  found `idempotency_keys` exists in the schema but has **no application consumer** — using it
  would require a new `IIdempotencyRepository` + impl + `IUnitOfWork.idempotency` + concrete-UoW
  wiring + every fake UoW + DI, i.e. a *new cross-cutting persistence subsystem*, not a thin
  webhook ingress. **Current correctness is guaranteed by `CompletionEngine`'s lease + `paused →
  running` CAS semantics** (G3), and **200-on-duplicate holds without any receipt**: a Fal retry
  after resume finds the run no longer `paused` → `complete()` returns `noop` → 200; a retry
  mid-processing hits the held lease → `locked` → 200. A first-class `IdempotencyRepository` will
  be introduced when **two or more** inbound endpoints require shared receipt/audit semantics
  (Fal webhook + Stripe + publishing/OAuth callbacks) — then it is clearly a reusable platform
  service, not hidden inside α8.3b. The awkward middle (writing the row without the abstraction)
  is explicitly rejected: it would violate the repository/UoW pattern "just this once."
- HTTP contract: **401** bad/missing signature or stale timestamp; **400** malformed; **200/204** for accepted, duplicate, unknown-`request_id`, or not-paused (ack so Fal stops retrying) — always with structured logs. (Acking unknowns/duplicates avoids provider retry storms.)
- **Execution model:** run `complete()` **inline** (library-only, D11 — no broker/worker to hand off to). A slow resolve blocks the webhook response; acceptable at current scale. If this becomes a problem it's an α8.x infra decision (Celery/queue), not part of α8.3b.

### Fork E — config surface (all new keys, non-frozen)
`fal_webhook_jwks_url` (default `https://rest.fal.ai/.well-known/jwks.json`), `fal_webhook_timestamp_tolerance_seconds` (300), `fal_webhook_jwks_cache_ttl_seconds` (≤ 86400), and optionally `webhook_ingress_enabled` (feature-gate the route).

---

→ **Fork D ✅ SIGNED OFF** (401/400/200-ack + inline `complete()`).

---

## 3a. Invariant W8.3b.1 (signed off) — webhook payloads never directly mutate workflow state

The **only** permissible path from an inbound webhook to state change is:

```
verify → lookup paused run → CompletionEngine.complete(...)
```

A webhook handler MUST NEVER, directly:
- mark steps complete,
- write usage,
- emit workflow events,
- resume workflows,
- or otherwise touch the aggregate.

All of that remains **exclusively** inside the already-frozen completion pipeline
(`CompletionEngine` → `ResumeWorkflowRun` → `AdvanceWorkflowRun`). This reinforces
ADR-0042 and makes future reviews trivial: the ingress use case's only write is,
at most, the `idempotency_keys` receipt (dedup/audit) — it holds **no**
orchestration authority.

---

## 4. ADR-0042 freeze compliance (self-check)

**Expected diff — none of it touches a frozen path:**
- `app/api/v1/routers/webhooks.py` (new) + registration in `main.py` (non-frozen)
- `app/application/use_cases/workflow/receive_provider_webhook.py` (new ingress)
- `app/application/interfaces/webhook_verifier.py` (new port) + `providers/fal/webhook.py` (new adapter — non-frozen growth surface)
- `IWorkflowRunRepository.find_paused_by_provider_job_id` + impl (repository layer — **not** in §D1)
- idempotency-key persistence via existing repo/model
- `core/config.py` + `core/container.py` wiring (non-frozen)
- tests

**Tripwire expectation:** `python backend/scripts/check_frozen_platform.py --base main` stays **green** on the α8.3b branch. **If implementation reveals a need to modify `complete()`, `resolve_job`, the provider `ports.py`, or `ResumeWorkflowRun`, STOP** and decide per ADR-0042 §D2 (bug fix vs. new ADR vs. redesign the adapter) — do not add a `Freeze-Override` to push through a shortcut. Current design predicts no such need.

**Acceptance criterion (signed off):** `python backend/scripts/check_frozen_platform.py --base main` must pass **throughout the branch with zero override markers**. If implementation pressures a change to `CompletionEngine`, `AdvanceWorkflowRun`, `ResumeWorkflowRun`, provider `ports.py`, the dispatcher, or the registry — **stop and revisit the design**; do not use a `Freeze-Override`.

## 5. Migration verdict

**Zero migration.** `idempotency_keys` already carries the right unique key; the run-lookup queries existing JSONB. (This corrects the earlier guess that α8.3b would be the first non-zero-migration slice — grounding shows the substrate already suffices; an optional lookup index would be a *separate* perf ADR, not part of this slice.)

## 6. Test plan (unit; integration deferred to the live-DB stages)
- **Verifier:** valid signature passes; tampered body/sig fails; missing header fails; stale timestamp (>300 s) fails; JWKS cache hit avoids refetch; key rotation (2nd key verifies) passes.
- **Ingress use case:** unknown `request_id` → 200 noop; duplicate delivery → dedup short-circuit (single `complete()`); happy path → exactly one `complete(project_id, run_id)` with the resolved run; not-paused run → noop; bad signature → 401 (no `complete()` call).
- **Repo lookup (Fork A2):** `find_paused_by_provider_job_id` returns the paused run; returns `None` for unknown/terminal.
- **Race:** webhook + `poll_once()` on the same run resume **once** (lease + CAS) — asserts G3 holds across ingresses.
- **Router:** raw-body capture (signature is over exact bytes), status-code contract (401/400/200).

## 7. Scope / non-goals
**In:** one Fal webhook route, ED25519+JWKS verification, provider_job_id→run lookup, trigger `complete()` (exactly-once via the frozen lease+CAS). **Out (explicit):** inbound webhook **receipt persistence** (`idempotency_keys` — deferred to a first-class `IdempotencyRepository` with ≥2 consumers; see Fork D), other providers' webhooks (pattern generalises but only Fal ships), any change to frozen orchestration, media/`video_ref` (α8.4), FFmpeg, export (α8.5), broker/worker/async offload, outbound `webhook_deliveries`, new indexes/migrations.

---

## 8. Questions for sign-off
1. **Fork A** — approve **A2** (additive repo lookup on checkpoint JSON), or prefer A1 (scan)?
2. **Fork B** — approve **B1** (JWKS fetch+cache via injected client, `cryptography` for ED25519) and the documented W8.1.1 public-key deviation?
3. **Fork C** — approve **C1** (webhook is trigger-only; `complete()` re-resolves authoritatively)?
4. **Fork D** — approve the HTTP contract (401/400/200-ack-unknowns) and **inline** `complete()` execution (no broker)?
5. **Version** — confirm α8.3b **does** get a runtime version (`v0.4.24-phase3-alpha8.3b`), since it adds a deployable capability (unlike the ADR-0042 governance freeze).
