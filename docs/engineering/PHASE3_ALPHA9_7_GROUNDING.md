# α9.7 — Generation Ingress (Creator-Triggered Video Generation) — Grounding

> **Status:** read-only grounding. **Baseline:** `v0.4.49-phase3-alpha9.6` (immutable).
> **Verdict: an ADR is genuinely required — STOP before pre-flight.** Proposed **ADR-0052**.
> Facts only; every claim is cited to source.

---

## 1. Why this slice was selected

The three formal roadmap rows in `PLATFORM_STATUS.md` are all blocked or low-value right now:

| Row | Status |
|---|---|
| **α8.4f** render composition | Explicitly **blocked** on the α6.4 Timeline authoring write paths |
| **α8.5b.4′** push / websocket | Needs a `device_tokens` table + mobile-auth decisions; email (the high-value channel) shipped in α9.5 |
| **α8.6d″** further destinations | Instagram / Facebook are gated on **public-URL or resumable-upload prerequisites plus App Review** — the α9.6 grounding already established these as external blockers |

Re-reading `NEXT_VERTICAL_SLICES_DISCOVERY.md`, **every one of its ranked top-5 has shipped**
(§2.10→α8.8, §2.1→α8.9a, §2.2→α8.9b, §2.3→α9.1, §2.9→α9.2), as have §2.4 (α9.3), §2.5 (α9.4),
§2.6 (α9.6), §2.7-email (α9.5) and §2.8 (α8.9c).

**Roadmap priorities have therefore changed.** The discovery report's §2.14 ("Creator Workflow —
the product's core promise") was ranked *unreachable* at the time because it "depends on §2.10 to
make Path B reach export/publish". **§2.10 shipped in α8.8**, and α9.3/α9.4/α9.6 completed the
publishing half. That dependency is now discharged, which promotes the remaining gap to the top.

**The gap:** the platform can render, export, publish, schedule, caption, thumbnail, notify, and
analyse — but a client **cannot ask it to generate a video**. Every other capability has a router;
generation does not.

```
backend/app/api/v1/routers/  →  analytics, auth, dashboard, export_jobs, health, library, media,
notifications, projects, prompts, publish_jobs, publish_metadata, render_jobs, scenes,
social_accounts, timeline, users, versions, webhooks, workflow_runs
```

There is **no generation router**, and no `GenerateVideoDep` in `backend/app/api/v1/deps.py`.

---

## 2. What exists today (verified)

### 2.1 The use case is real, complete, and container-wired

`GenerateVideo` (`backend/app/application/use_cases/generation/generate_video.py:86`) takes
`GenerateVideoRequest` → `GenerateVideoResult` and runs the full pipeline: plan → storyboard →
resolve → per-shot generate/verify/repair → timeline verify → ffmpeg render → probe → persist.

It is wired at `container.get_generate_video_use_case(session)`
(`backend/app/core/container.py:1520`), consumed today only by `scripts/generate_demo.py:92` and
tests.

### 2.2 It is synchronous and long-running

`execute()` blocks until terminal. It loops over shots (~6 for the default 18s/3s scenario) with up
to `DEFAULT_MAX_ATTEMPTS = 3` per shot, calling `IImageGenerator.generate()` **inline** (Pollinations
timeout default **120 s**), then runs ffmpeg inline (`render_timeout_seconds` default **900 s**).
**There is no generation worker** — grep finds no `GenerationWorker` anywhere in `backend/`.

### 2.3 Generation state is **unowned** — by deliberate design

`generations`, `generation_shots`, and `generation_assets` (migration
`0012_execution_runtime.py:99-207`) carry **no `tenant_id`, no `owner_user_id`, no `project_id`**.

This is not an oversight. Two frozen documents say so explicitly:

* **ADR-0046 Q1** — "generation has no tenant/owner/project/publishing context **yet**."
* **α8.8 AP9** (`promote_generation_assets.py:24`) — "**project-asserted, generation-unowned.**
  `generations` carry no ownership (ADR-0046 Q1), so promotion authorizes the *project* (owned by
  the caller) but does not bind the *generation* to an owner; **that is deferred to the future
  generation-trigger slice.**"

**The codebase names this slice and names its blocking decision.**

### 2.4 There is no read surface

`IExecutionRuntimeStore` (`application/interfaces/execution_runtime_store.py:72-133`) is
**write-only** (`begin`, `set_status`, `record_resolution`, `register_asset`, `record_shot`,
`complete`, `fail`). The generation repositories expose **writes only**. The single read port,
`IGenerationReader.load_final_video(generation_id)`
(`application/interfaces/generation_reader.py:73`), takes a bare id — no owner scoping — and feeds
α8.8 promotion only.

No endpoint anywhere returns generation runs, status, shots, or assets.

### 2.5 `/workflow-runs` is a different thing

`POST /api/v1/projects/{id}/workflow-runs` accepts `generate-video@1.0.0`
(`domain/workflow/registry.py:317-328`), but that step emits a `StepCommand` dispatched to
`VideoProvider.submit()` (`infrastructure/ai/dispatcher.py:113`) and typically **pauses** on the
async provider seam. **No code path from HTTP reaches the α8.6 `GenerateVideo` runtime.** The two
are unrelated pipelines (the "Path A / Path B split" of the discovery report).

### 2.6 The async-job precedent is well established

Render, export, and publish all follow the identical shape: `POST` creates a **`queued`** row, a
poll-ingress worker claims it, and a `GET` returns status. Cited: `CreateRenderJob` +
`RenderWorker.run_once()` (`render_worker.py:35`), `CreateExportJob`, `CreatePublishJob` +
`get_publish_worker()` (`container.py:2000`). Generation has **no** analogous queued table, worker,
or poll endpoint.

### 2.7 Test coverage drives the use case directly, never HTTP

CI **Stage 13** (`ci_gate.py:546-568`) runs
`tests/integration/infrastructure/generation/test_generation_end_to_end.py`, which composes the use
case by hand (`:234`). No test exercises generation over HTTP.

---

## 3. The architectural decisions this slice forces

These are the reasons an ADR is **genuinely required** rather than a matter of taste.

### D1 — Generation ownership (the blocking decision)

To expose generation to a creator, the platform must answer a question the frozen schema
deliberately left open: **who owns a generation?** Owner-scoped reads (`GET /generations/{id}`,
"list my generations") are impossible today because no column can scope them, and every other read
surface in the platform derives `tenant_id` + `owner_user_id` from `CurrentUserDep`.

Candidate shapes, each with different blast radius:

1. **Add `tenant_id` / `owner_user_id` / `project_id` to `generations`** — direct, but amends a
   table frozen under ADR-0046 and contradicts Q1's stated rationale.
2. **A new owned "generation job" wrapper table** (mirroring `publish_jobs`) that references the
   unowned `generations` row — preserves ADR-0046 Q1 verbatim, adds a table and a lifecycle.
3. **Project-asserted only** (reuse the α8.8 AP9 pattern) — no ownership binding; the caller asserts
   a project they own. Cheapest, but leaves "list my generations" unbuildable and repeats a pattern
   AP9 itself flagged as temporary.

This **cannot** be settled inside a pre-flight: it reverses or re-affirms a recorded ADR ruling and
determines whether a migration is needed at all.

### D2 — Execution model

`GenerateVideo.execute()` can block for **minutes**. Options: run inline in the request (violates
the platform's own async-job convention and risks gateway timeouts), or introduce a **queued job +
poll-ingress worker** matching render/export/publish. The latter is almost certainly right, but it
means a **new job lifecycle and a new worker** — a structural addition, not an adapter.

### D3 — Progress and cancellation semantics

Render/export/publish expose coarse status plus attempt counts. Generation has genuinely richer
intermediate state (per-shot accept/repair, resolution ledger). What is contractual versus internal
must be decided before an API shape is drawn, or the read model will leak runtime internals the way
α9.5 nearly leaked its `_email` namespace.

### D4 — Idempotency

Every other create path has an idempotency story (`publish_jobs` unique constraint, body-level
idempotency keys). `GenerateVideoRequest.generation_id` is caller-suppliable
(`request.py:14-39`) — whether that becomes the idempotency key, and how a replay behaves, is a
correctness decision with cost consequences (a duplicate generation burns real provider spend).

---

## 4. Migration assessment

**Unavoidably migration-bearing under options 1 and 2 of D1.** Only option 3 (project-asserted,
no ownership) avoids a migration, and it cannot deliver an owner-scoped read surface. This is
therefore the first slice since α9.0 that likely requires a migration, which reinforces the ADR.

---

## 5. Scope boundaries observed during grounding

Not in scope for whatever α9.7 becomes, and recorded here so the pre-flight cannot drift: the
Path A / Path B pipeline unification (§2.14 "wire the whole funnel"), Verification V2 (§2.11),
Repair V2 (§2.12), a generation UI, and billing/credit debiting for generation spend (§2.18, itself
Very Large with its own ADR).

---

## 6. Verdict

**STOP — ADR required.**

The slice is correctly selected: it closes the platform's single largest product-facing gap (the
core promise, "prompt → video", is unreachable by any client) and its prerequisite — the α8.8 asset
promotion bridge — has shipped. But it cannot proceed to pre-flight because **D1 (generation
ownership) is an open question that a frozen ADR deliberately deferred to exactly this slice**, and
D2 adds a new worker and job lifecycle.

Proposed: **ADR-0052 — Generation ingress: ownership, execution model, and read contract**,
deciding D1–D4 with the usual comparison tables, consequences, and rejected alternatives.
