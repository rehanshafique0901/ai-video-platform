# Phase 3 Slice α6.1 — Prompt Aggregate — Pre-flight

> Status: **SIGNED OFF — α6.1 READY.** **Q1 + Q8 = Option A** (prompts are
> generation **inputs**: no per-row OCC, no `projects.version` bump, and
> **excluded** from version snapshots/restore/diff); **Q2–Q13 accepted as
> drafted**. The architectural principle is recorded verbatim in **ADR-0036**
> (§7 Q13). Branch `phase3/alpha6.1-prompts`; implementation follows §10.
>
> Mirrors the α5a/α5b/α5c/α5d discipline: ground in the physical schema →
> lock decisions → sign-off → branch → implement → CI → merge → tag. Read-only
> planning artefact per `docs/engineering/RUNBOOK_WAVE.md` §1.
>
> **Predecessors.**
> α5a (`v0.4.5`) Projects create+read · α5b (`v0.4.6`) Projects update+soft-delete
> · α5c (`v0.4.7`) Scenes CRUD+reorder · α5d.1 (`v0.4.8`) Version capture+read ·
> α5d.2 (`v0.4.9`) Restore+diff + **Aggregate OCC Rule** · α5d.3 (`v0.4.10`)
> Branch (fork-to-new-project).
>
> **Companion design docs.**
> * `docs/domain/PROJECT_AGGREGATE.md` §6/§8 — parent aggregate + the
>   **Aggregate OCC Rule** invariant (#6). **Read Q1 against it.**
> * `docs/domain/SCENE_AGGREGATE.md` — the sibling child aggregate; prompts
>   were explicitly deferred *from* α5c to here (`SCENE_AGGREGATE.md` §4, D2).
> * `docs/decisions/ADR-0035-project-version-snapshots.md` — defines what the
>   version ledger snapshots (project root + scenes). Q8 decides whether
>   prompts join that set (recommendation: **no**).
>
> **Reference implementations to mirror.**
> * Two-level ownership gate (project → child), 404-anti-enumeration → α5c
>   `GetScene` / two-level D6.
> * Owner-scoped soft-delete, idempotent-by-404 → α5c `DeleteScene` /
>   `soft_delete_owned` (D13).
> * Nested router under a project, DI factories + `deps` aliases, UoW child
>   repo wiring → α5c `routers/scenes.py` + `ISceneRepository`.
> * Tri-state PATCH via `model_fields_set` → α5b/α5c `*UpdateRequest`.
>
> **Baseline versioning.** `main` is at `0.4.10` (tag
> `v0.4.10-phase3-alpha5d3`). First α6.1 commit bumps `app/main.py` →
> `"0.4.11-phase3-alpha6.1-dev"`; release tag `v0.4.11-phase3-alpha6.1` drops
> `-dev` on merge.

---

## Section 1 — Scope

### 1.1 One-line thesis

α6.1 introduces the **Prompt aggregate** — the first *generation-input*
content — as an owner-scoped CRUD surface under a project
(`/projects/{id}/prompts`), backed by the existing `prompts` table with a
**nullable `scene_id`** (project-level *or* scene-scoped prompts) and a
**typed `kind`** (the `prompt_kind` enum). It reuses every pattern from α5c
(CurrentUserDep, owner+tenant scoping via the project gate,
404-anti-enumeration, two-level visibility, soft-delete-idempotent-by-404) and
resolves **one new architectural question**: the `prompts` table has **no
`version` column** (baseline omitted `VersionMixin`), so α6.1 must decide the
concurrency model without inventing a migration. **α6.1 ships zero migrations**
(table + all three indexes exist in baseline `0001`).

### 1.2 What's in

1. **`POST /api/v1/projects/{project_id}/prompts`** — create a prompt
   (`kind` + `text_content`, optional `scene_id` / `model_id`). `201` +
   `PromptPublic`.
2. **`GET /api/v1/projects/{project_id}/prompts`** — list the project's live
   prompts, newest-first, with optional `?kind=` and `?scene_id=` filters.
   `200` + `{ data: [PromptPublic], meta }`. Side-effect-free.
3. **`GET /api/v1/projects/{project_id}/prompts/{prompt_id}`** — single
   prompt. `200` / `404`.
4. **`PATCH /api/v1/projects/{project_id}/prompts/{prompt_id}`** — partial
   content update (`text_content` / `kind` / `model_id` / `extra`). `200` /
   `404` / `422` (+ `412` **iff** Q1 = aggregate-OCC).
5. **`DELETE /api/v1/projects/{project_id}/prompts/{prompt_id}`** —
   owner-scoped soft delete, unconditional, `204`, idempotent-by-404.
6. **Domain:** `app/domain/prompts/prompt.py` — frozen `Prompt` entity
   (slim surface, `dataclasses.replace` mutators).
7. **`IPromptRepository`** + `SqlAlchemyPromptRepository`: `add`,
   `list_owned` (+ filters), `get_owned`, `update_owned`, `soft_delete_owned`
   — all project-gated.
8. **Use cases** (`app/application/use_cases/prompts/`): `CreatePrompt`,
   `ListPrompts`, `GetPrompt`, `UpdatePrompt`, `DeletePrompt`.
9. **DTOs** (`app/api/v1/schemas/prompts.py`): `PromptCreateRequest`,
   `PromptUpdateRequest` (tri-state, `extra="forbid"`), `PromptPublic`.
10. **Router** `app/api/v1/routers/prompts.py`, prefix
    `/projects/{project_id}/prompts`; mounted alongside the scenes router.
11. **DI**: container factories + `deps.py` aliases; `.prompts` on the UoW
    (+ `_TestUnitOfWork`, + `FakeUnitOfWork`).
12. **Fakes**: `FakePromptRepository`.
13. **Docs**: `API_CONTRACT.md` prompts subsection (reconcile the
    `/prompts/{id}` stub → nested, Q2); `CHANGELOG.md`; version bump;
    `ROADMAP.md`; `PROJECT_AGGREGATE.md` §8; **ADR-0036** (Q1/Q8 precedent).

### 1.3 Non-goals (explicit, will NOT ship in α6.1)

* **Media generation** — no `media_assets`, no provider calls; a prompt is
  authored/stored text, nothing renders it (α6.2).
* **AI-authored prompts** — `generated_by_agent` stays server-`NULL`; the
  AI-generation seam (script/storyboard agents) is α8. Not client-supplied.
* **Model validation beyond existence** — `model_id` is optionally linked to
  the `ai_models` registry (existence + not-retired check, Q4); no
  capability/kind-compatibility matching (that belongs to generation, α6.2).
* **Prompts in version snapshots** — the `project_versions` snapshot stays
  {project root + scenes}; prompts are **not** captured/restored/diffed in
  α6.1 (Q8, ADR-0036). Prompts are generation inputs, not editorial content.
* **Prompt ordering / templates / variables** — no `position`, no
  `{{variable}}` interpolation, no prompt-template library (`templates` table
  is untouched).
* **Cursor pagination** — full ordered array with filters (Q11); cursor
  deferred.
* **Re-parenting** — `scene_id` is set at create and immutable thereafter
  (Q10); moving a prompt between scenes is not a use case yet.
* **Migrations** — none. If the slice appears to need one, stop and re-scope.

### 1.4 Anti-scope-creep envelope

* *"Add a `version` column so prompts get OCC like scenes."* — No; that is a
  migration. Q1 resolves concurrency **within the existing schema**.
* *"Let a prompt generate an image while we're here."* — No; that is α6.2
  (media assets) and needs a provider seam.
* *"Snapshot prompts into the project version."* — No; prompts are generation
  inputs, out of the editorial snapshot (Q8/ADR-0036).
* *"Accept `generated_by_agent` from the client."* — No; provenance is
  server-owned, and AI authorship is α8.
* *"Add prompt templates / variable substitution."* — No; separate feature.

---

## Section 2 — Foundational facts (grounded in the physical schema)

Read straight off `0001_baseline.py` + `models/scenes.py` (the `Prompt` model
lives there) + `enums.py`. These are **not** decisions — they are the
constraints α6.1 must honour.

### F1 — `prompts` columns (baseline `0001`)
```
id                  uuid PK default gen_random_uuid()
project_id          uuid NOT NULL REFERENCES projects(id)  ON DELETE CASCADE
scene_id            uuid          REFERENCES scenes(id)    ON DELETE SET NULL   -- nullable
kind                prompt_kind NOT NULL
text_content        text NOT NULL
model_id            uuid          REFERENCES ai_models(id) ON DELETE SET NULL   -- nullable
generated_by_agent  text                                                         -- nullable
extra               jsonb NOT NULL DEFAULT '{}'::jsonb
created_at          timestamptz NOT NULL DEFAULT now()
updated_at          timestamptz NOT NULL DEFAULT now()
deleted_at          timestamptz                                                  -- soft delete
```
Indexes: `ix_prompts_project_id_kind (project_id, kind)`,
`ix_prompts_scene_id (scene_id)`, `ix_prompts_model_id (model_id)`.

### F2 — **No row-OCC `version` column.**
`Prompt` uses `UUIDPrimaryKeyMixin + TimestampMixin + SoftDeleteMixin` — **no
`VersionMixin`**. `prompts` is **absent** from `_VERSION_BUMP_TABLES`; it has
only `tg_prompts_biu_touch_updated_at` (auto `updated_at`) and is **not**
immutable. → There is no per-row concurrency token on a prompt. This is the
crux of **Q1**.

### F3 — No `tenant_id` / `owner_user_id`.
Ownership is **derived** through `project_id → projects.(tenant_id,
owner_user_id)` — exactly the α5c scene model (scene → storyboard → project).
Every endpoint gates on the project first.

### F4 — No uniqueness constraint.
Multiple prompts of the same `kind` on the same scene/project are legal. No
dedupe, no collision path (contrast the scene `scene_number` partial-unique).

### F5 — `prompt_kind` enum (8 values, generation-target-oriented).
`image, video, animation, negative, camera, motion, lighting, style`. These
are **modality/aspect** kinds, **not** chat-style `system`/`user`. The DTO
validates against exactly this set.

### F6 — FKs degrade softly.
`scene_id` and `model_id` are `ON DELETE SET NULL`; `project_id` is `ON DELETE
CASCADE`. Note **SET NULL fires only on a hard `DELETE`** of the parent row —
scenes are **soft-deleted** (α5c), so a prompt's `scene_id` link **survives**
scene soft-delete and even survives a version restore (which soft-deletes then
revives scenes under the same `id`). Relevant to Q8.

---

## Section 3 — Implementation decisions (α6.1-specific, proposed)

> These follow directly from §2 + the α5c precedent. Load-bearing choices are
> escalated to **Q1–Q13** in §7; the rest are mechanical mirrors.

### D1 — New `prompts` bounded context, mounted under projects
Add `domain/prompts`, `use_cases/prompts`, `schemas/prompts.py`,
`routers/prompts.py`, `SqlAlchemyPromptRepository`. Router prefix
`/projects/{project_id}/prompts` (nested), mirroring α5c scenes. Package lives
in its own bounded context (not inside projects), route nests under the parent.

### D2 — Two-level visibility gate (project → prompt), 404 on both
1. **Project gate** — `ProjectRepository.get_owned(project_id, tenant, owner)`;
   `None` → `404` (reuse α5a, no new project code).
2. **Prompt gate** — the prompt must be live **and** have
   `project_id == resolved project`; else `404` (anti-enumeration, a
   `prompt_id` under another user's project is a uniform `404`).

### D3 — Slim domain over the physical row
Domain `Prompt = {id, project_id(internal), scene_id, kind, text_content,
model_id, extra, created_at, updated_at, deleted_at}`. `generated_by_agent`
stays physical + server-`NULL` (not in the domain surface for α6.1). Frozen
dataclass + `replace`-based mutators (α5c `Scene` pattern).

### D4 — DELETE = unconditional soft delete, `204`, idempotent-by-404
Exactly α5c D13: `soft_delete_owned` sets `deleted_at` on the caller's own
live prompt (project-gated); `False` → `404`. First delete `204`; repeat and
any subsequent `GET`/`PATCH` → `404`.

### D5 — `updated_at` is trigger-owned
`tg_prompts_biu_touch_updated_at` sets `updated_at = now()` on every UPDATE;
the repo does not hand-set it (nothing to reconcile — there is no version
trigger to guard, cf. F2).

### D6 — List is side-effect-free, filtered, newest-first
`GET …/prompts` returns live prompts ordered by `(created_at DESC, id DESC)`,
with optional `?kind=<enum>` and `?scene_id=<uuid>` filters (both validated;
bad enum / non-UUID → `422`). No storyboard-style lazy creation (prompts have
no implicit parent to materialise). Empty → `200 []`.

> **Concurrency (D7), snapshot interplay (D8), endpoint shape, model linkage,
> and mutable-field set are decided in §7 (Q1–Q13).**

---

## Section 4 — Acceptance criteria (behavioural, provisional on §7)

**A1. Create happy.** `POST …/prompts` (owned project, `kind` +
`text_content`) → `201` + `PromptPublic`; `scene_id`/`model_id` echoed
(or `null`).
**A2. Create with scene link.** Valid `scene_id` in the same project → linked;
`scene_id` of another project / unknown / soft-deleted scene → `422` (Q3).
**A3. Create with model link.** Valid `model_id` (exists, not retired) →
linked; unknown / retired → `422` (Q4).
**A4. Create validation.** Missing `kind`/`text_content`, invalid `kind`
(not in enum), empty/oversize `text_content`, forbidden field
(`generated_by_agent`, `id`) → `422`.
**A5. Create on not-owned/missing project → 404** (project gate).
**A6. List happy + newest-first.** `200`, live prompts, `(created_at, id)`
DESC.
**A7. List filters.** `?kind=image` and `?scene_id=<uuid>` narrow correctly;
combined filters AND; bad enum / non-UUID → `422`.
**A8. List excludes soft-deleted; empty → `200 []`.**
**A9. List on not-owned/missing project → 404.**
**A10. Get single happy / 404** (unknown / other project / soft-deleted).
**A11. PATCH happy.** Mutable field changed → `200`; `updated_at` advances.
Field set + OCC behaviour per Q1/Q10.
**A12. PATCH partial (absent unchanged); explicit-null on nullable clears
(`model_id:null`); null on non-nullable (`text_content:null`) → `422`.**
**A13. PATCH not-owned/missing/soft-deleted (project or prompt) → 404.**
**A14. PATCH empty patch / forbidden field → 422** (+ stale token → `412` iff
Q1 = aggregate-OCC).
**A15. DELETE happy / idempotent-by-404.** First `204`; second `404`;
`GET`/`PATCH` after → `404`; not-owned/unknown → `404`; no auth → `401`;
non-UUID path → `422`.

**Engineering (E1–E6):** CI gate green; **no new migration** (stages 5/6/7
unchanged); no new `noqa`/`type: ignore`; unit coverage ≥ 80%; `import-linter`
kept (prompts mirror the scenes layering); schema validator + ERD unchanged
(no ORM change — table + indexes already exist).

---

## Section 5 — Test matrix (provisional)

### 5.1 Unit — use cases (fakes)
`CreatePrompt` happy / scene-link-valid / scene-link-foreign→422 /
model-link-valid / model-link-unknown→422 / project-not-owned→404;
`ListPrompts` ordered / kind-filter / scene-filter / empty / not-owned→404;
`GetPrompt` happy / not-visible→404; `UpdatePrompt` real-change /
same-value-no-op / not-visible→404 / (stale→412 iff Q1) / explicit-null-clears;
`DeletePrompt` happy / idempotent→404; scoping threaded on all.

### 5.2 Repository integration (real DB, SAVEPOINT rollback)
`add` (project- and scene-scoped) · `list_owned` order + soft-delete exclusion
+ kind/scene filters · `get_owned` cross-project isolation · `update_owned`
real change (`updated_at` advances; +OCC per Q1) · `update_owned` foreign/
soft-deleted → None · `soft_delete_owned` happy / wrong-owner / already-deleted
· **F6 link durability**: prompt `scene_id` survives scene *soft-delete* (SET
NULL does not fire) — the load-bearing schema-interaction test.

### 5.3 HTTP integration — `test_prompts.py`
Register→token→create-project(→create-scene)→call; cover A1–A15 end-to-end
(201/200/204/404/422/401, two-level 404, filters, tri-state PATCH,
idempotent-by-404; +412 iff Q1 = aggregate-OCC).

---

## Section 6 — Structured-log catalogue (α6.1 additions)

| Event | Level | Fields |
|---|---|---|
| `prompt.created` | INFO | `prompt_id`, `project_id`, `scene_id`, `kind`, `model_id`, `owner_user_id`, `tenant_id`, `ip`, `request_id` |
| `prompt.updated` | INFO | `prompt_id`, `project_id`, `changed_fields`, `ip`, `request_id` |
| `prompt.update_rejected` | WARN | `reason` (`not_visible` / `version_mismatch`†), `prompt_id`, `ip`, `request_id` |
| `prompt.deleted` | INFO | `prompt_id`, `project_id`, `owner_user_id`, `ip`, `request_id` |
| `prompt.delete_rejected` | WARN | `reason` (`not_visible`), `prompt_id`, `ip`, `request_id` |

* **No content in logs** — `text_content` / `extra` values are never logged
  (field **names** only). †`version_mismatch` only if Q1 = aggregate-OCC.

---

## Section 7 — Decisions & Open Questions (SIGN-OFF NEEDED)

### Q1 — Concurrency model, given `prompts` has **no `version` column** (F2). ★ load-bearing
| Option | Meaning | Trade-off |
|---|---|---|
| **A — No per-row OCC (last-writer-wins).** `PATCH` is a plain project-gated update; no fence, no `projects.version` bump. | Honours the baseline's explicit omission of `VersionMixin`: prompts are **not** concurrency-controlled rows. | Simplest, migration-free, matches the schema's own signal. Two racing edits: last wins (acceptable — prompts are low-contention authored text, usually created fresh not co-edited). |
| **B — Aggregate OCC.** `PATCH`/`DELETE`/`create` fence on + bump `projects.version` (the α5d.2 Aggregate OCC Rule token). Body carries `{ version }`. | Treats prompts as part of the versioned Project aggregate (invariant #6). | Migration-free (uses `projects.version`), consistent with scenes. **But** couples prompt edits to scene edits (either invalidates the other's token) and implies prompts belong in snapshots (contradicts Q8). Heavier API. |
| **C — Add `version` column.** Give prompts their own row-OCC via migration. | Full parity with scenes. | **Rejected** — breaks the no-migration discipline every α5 slice held; prompts are not high-contention enough to justify it. |

**Recommendation: A.** The baseline deliberately gave `prompts` no `version`
(unlike `scenes`), signalling prompts are generation **inputs**, not
concurrency-guarded editorial rows. Pair with Q8 (prompts out of snapshots) for
a coherent story: the versioned aggregate = {project root + scenes}; prompts /
media / timeline are production artefacts with their own lifecycles. If the
reviewer wants strict aggregate-OCC consistency, **B** is available
migration-free — but then Q8 must also flip (prompts enter snapshots), enlarging
the slice.

### Q2 — Endpoint shape: reconcile the `API_CONTRACT` `/prompts/{id}` stub.
The stub lists `/projects/{id}/prompts` **and** top-level `/prompts/{id}`.
**Recommendation: nest everything** — `/projects/{project_id}/prompts` and
`/projects/{project_id}/prompts/{prompt_id}` — consistent with scenes/versions
and the two-level gate (D2). Update the `API_CONTRACT` stub accordingly. A
top-level `/prompts/{id}` would need a global prompt→project resolve and diverge
from every other child resource.

### Q3 — Scene linkage: support project-level **and** scene-scoped prompts?
`scene_id` is nullable (F1). **Recommendation: yes, both.** `scene_id` is
**optional** on create; when present it must reference a **live scene in the
same project** (validated via the scene repo) → else `422`. Omitted → NULL
(project-level prompt). List supports `?scene_id=` filter (D6).

### Q4 — `model_id`: accept + validate, or defer?
**Recommendation: accept optional `model_id`, validated for existence + status
≠ `retired` against `ai_models`.** It is a natural prompt attribute ("which
model is this prompt written for"), cheap to validate, and the index exists.
Escape hatch: if the `ai_models` read-path adds friction, defer to α6.2
(always NULL in α6.1) — but recommend accepting it now.

### Q5 — `generated_by_agent`: client-supplied? **Recommendation: no.**
Server-owned provenance; stays NULL for human-authored prompts; AI authorship
is α8. Not in the create/update DTO.

### Q6 — `kind`: required + enum-validated. **Recommendation: yes** — required
on create, validated against the 8 `prompt_kind` values (F5). Mutable on PATCH
(Q10).

### Q7 — `text_content` bounds. **Recommendation:** required, strip, `1 ≤ len ≤
10000`. `extra="forbid"` on the DTO. (`extra` JSONB: accept optional object,
default `{}`; validate it is a dict.)

### Q8 — Prompts in version snapshots / restore / diff? ★ pairs with Q1
**Recommendation: NO for α6.1.** The `project_versions` snapshot stays {project
root + scenes} (ADR-0035). Prompts are generation inputs, not editorial content;
capturing them would enlarge the snapshot and couple generation state to
editorial history. Document explicitly in ADR-0036 + PROJECT_AGGREGATE §6 so
restore's silence on prompts is a **decision, not an omission**. Note F6: a
restore soft-deletes/revives scenes under the same `id`, so prompt→scene links
survive restore regardless.

### Q9 — List pagination. **Recommendation:** full ordered array + filters
(mirror α5c Q2); defer cursor. Revisit if a project accrues many prompts.

### Q10 — Mutable field set on PATCH.
**Recommendation:** mutable = `text_content`, `kind`, `model_id`, `extra`
(tri-state via `model_fields_set`). **Immutable** = `scene_id` (no re-parenting
in α6.1), `project_id`, `id`, `generated_by_agent`. Empty patch → `422`.

### Q11 — `PromptPublic` fields.
**Recommendation:** `{id, project_id, scene_id, kind, text_content, model_id,
extra, created_at, updated_at}` (+ `version` **iff** Q1 = B). Omit
`generated_by_agent` (server-internal for now) and `deleted_at`.

### Q12 — One cohesive slice? **Recommendation: yes** — full Prompt CRUD
(create/list/get/patch/delete) as one PR → `v0.4.11`. No "move" (prompts
unordered). Small, self-contained.

### Q13 — Companion docs: `PROMPT_AGGREGATE.md` + ADR-0036?
**DECIDED (accepted as drafted):** (a) a concise
`docs/domain/PROMPT_AGGREGATE.md` mirroring `SCENE_AGGREGATE.md` (identity,
boundary, no-OCC rationale, snapshot exclusion); (b) a short **ADR-0036 —
"Prompts are generation inputs outside the versioned content aggregate"**
recording Q1(A) + Q8(no) as the precedent that media (α6.2) and timeline (α6.3)
will also follow.

**ADR-0036 must state the governing principle verbatim:**

> *Project versions capture editorial state, not generation inputs. Prompts are
> mutable generation inputs that do not participate in aggregate optimistic
> concurrency, snapshots, restore, or diff. Generated media may retain the
> prompt used for provenance independently of the current prompt record.*

This single rule keeps α6.2 (Media Assets), α6.3 (Timeline), and α6.4
(Rendering) from accidentally blurring the editorial/generation boundary.

---

## Section 8 — File inventory (provisional)

### 8.1 New files
| Path | LOC est. | Purpose |
|---|---:|---|
| `backend/app/domain/prompts/__init__.py` | ~3 | package |
| `backend/app/domain/prompts/prompt.py` | ~70 | frozen `Prompt` entity + `replace` mutators |
| `backend/app/infrastructure/repositories/prompt_repository.py` | ~170 | `SqlAlchemyPromptRepository` (add/list/get/update/soft-delete) |
| `backend/app/application/use_cases/prompts/__init__.py` | ~6 | exports |
| `backend/app/application/use_cases/prompts/create_prompt.py` | ~80 | `CreatePrompt` (scene/model link validation) |
| `backend/app/application/use_cases/prompts/list_prompts.py` | ~50 | `ListPrompts` (filters) |
| `backend/app/application/use_cases/prompts/get_prompt.py` | ~40 | `GetPrompt` |
| `backend/app/application/use_cases/prompts/update_prompt.py` | ~80 | `UpdatePrompt` |
| `backend/app/application/use_cases/prompts/delete_prompt.py` | ~50 | `DeletePrompt` |
| `backend/app/api/v1/schemas/prompts.py` | ~110 | `PromptPublic` / `PromptCreateRequest` / `PromptUpdateRequest` |
| `backend/app/api/v1/routers/prompts.py` | ~130 | nested router (POST/GET/GET/PATCH/DELETE) |
| `backend/tests/unit/application/use_cases/prompts/test_*.py` | ~320 | unit matrix (§5.1) |
| `backend/tests/integration/infrastructure/repositories/test_prompt_repository.py` | ~200 | repo matrix (§5.2) |
| `backend/tests/integration/api/test_prompts.py` | ~360 | HTTP matrix (§5.3) |
| `docs/domain/PROMPT_AGGREGATE.md` | ~120 | companion (Q13) |
| `docs/decisions/ADR-0036-prompts-generation-inputs.md` | ~90 | Q1/Q8 precedent (Q13) |

### 8.2 Modified files
| Path | Change |
|---|---|
| `backend/app/main.py` | version → `0.4.11-phase3-alpha6.1-dev` |
| `backend/app/application/interfaces/repositories.py` | add `IPromptRepository` |
| `backend/app/core/container.py` | 5 use-case factories + `PromptRepository` on the UoW |
| `backend/app/api/v1/deps.py` | 5 `*PromptDep` aliases |
| `backend/app/api/v1/routers/__init__.py` *(or app include)* | mount prompts router |
| `backend/app/infrastructure/db/unit_of_work.py` (+ `_TestUnitOfWork`) | expose `.prompts` |
| `backend/tests/unit/application/use_cases/auth/_fakes.py` | `FakePromptRepository` (+ `FakeUnitOfWork.prompts`) |
| `API_CONTRACT.md` | prompts subsection (reconcile stub, Q2) |
| `CHANGELOG.md` | `[Unreleased]` α6.1 entry |
| `ROADMAP.md` | Phase 3 row α6.1 |
| `docs/domain/PROJECT_AGGREGATE.md` | §8 diagram: Prompts α6.1; §6 note (prompts outside snapshot, Q8) |

> **UoW note (load-bearing, α5c lesson).** The real `UnitOfWork`, the
> integration `_TestUnitOfWork`, and `FakeUnitOfWork` must all gain `.prompts`
> or every prompt use-case test fails at attribute access.

### 8.3 Deliberately NOT touched
No migration; no ORM change (table + indexes exist); `media.py` untouched
(α6.2); `helpers.py` / `errors.py` consumed as-is; version ledger code
untouched (Q8 — prompts out of snapshot).

---

## Section 9 — Reviewer sign-off

**Reviewer verdict — 2026-07-12: ✅ Approved.** **Q1 + Q8 = Option A** —
prompts are generation **inputs**: no per-row OCC, no `projects.version` bump,
and excluded from version snapshots/restore/diff. The versioned aggregate stays
{project root + scenes}; generated media (α6.2) may retain the prompt used for
provenance independently of the current prompt record. **Q2–Q13 accepted as
drafted.** ADR-0036 records the governing principle verbatim (§7 Q13). Branch
cut authorised: **`phase3/alpha6.1-prompts`** (single slice) — follow §10.

---

## Section 10 — Implementation order (once approved)

1. Cut `phase3/alpha6.1-prompts` off fresh `main`; bump `app/main.py` →
   `0.4.11-phase3-alpha6.1-dev`.
2. `Prompt` domain entity (`domain/prompts/prompt.py`).
3. `IPromptRepository` + wire `.prompts` on the UoW (+ `_TestUnitOfWork`, +
   `FakeUnitOfWork`).
4. `SqlAlchemyPromptRepository` (add/list/get/update/soft-delete) +
   `FakePromptRepository`.
5. Use cases + unit tests (§5.1); `pytest -m unit` + mypy green.
6. DTOs + container factories + deps + `routers/prompts.py`; mount it.
7. Repo integration (§5.2, incl. the F6 link-durability test) + HTTP (§5.3).
8. Docs: API_CONTRACT (reconcile stub), CHANGELOG, ROADMAP, PROJECT_AGGREGATE
   §6/§8, PROMPT_AGGREGATE.md, ADR-0036.
9. Local CI gate green (no migration → stages 5/6/7 unchanged).
10. Commit (chore(docs) + feat(prompts)), push, PR, merge, tag
    `v0.4.11-phase3-alpha6.1`; pivot to **α6.2 (media assets)**.

---

## Section 11 — Post-α6.1 roadmap (dependency order)

* **α6.2 — Media assets** (`/projects/{id}/assets`) — generated *from* prompts;
  `media_assets.prompt_id` already exists. Owns generation history.
* **α6.3 — Timeline / tracks / clips** — assets placed on the timeline
  (`Scene → Media → Clip → Timeline`).
* **α6.4 — Render jobs** — orchestrates existing data; owns none of it.
* **α8+ — AI provider integration** — populates `generated_by_agent`,
  `reason=generated` versions, end-to-end generation.
