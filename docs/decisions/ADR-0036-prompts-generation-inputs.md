# ADR-0036 — Prompts Are Generation Inputs Outside the Versioned Content Aggregate

**Status:** Proposed (documents the pattern shipped in Phase 3 α6.1 — Prompt
CRUD). Flips to Accepted on merge of this ADR PR.
**Refines / documents:** `docs/domain/PROMPT_AGGREGATE.md`, `docs/domain/PROJECT_AGGREGATE.md`
§3/§6/§8 (the aggregate boundary + the two versioning mechanisms + the Aggregate
OCC Rule), `API_CONTRACT.md` §3.2.2, and the α6.1 pre-flight
(`docs/engineering/PHASE3_ALPHA6_1_PREFLIGHT.md`, §7 Q1/Q8/Q13). Builds on
**ADR-0035** (project version snapshots), **ADR-0034** (authenticated endpoint
pattern), and the α5c Scene aggregate.
**Wave:** Phase 3, generation-pipeline slice α6.1 (Prompt aggregate). Sets the
precedent for α6.2 (Media Assets), α6.3 (Timeline), α6.4 (Rendering).

---

## Context

α6.1 introduces the **Prompt aggregate** — the first *generation-input* content
— as an owner-scoped CRUD surface nested under a project
(`/projects/{id}/prompts`), backed by the existing baseline `prompts` table.
The slice ships **zero migrations** (table + all three indexes exist in baseline
`0001`).

One load-bearing architectural question had to be resolved: the `prompts` table
has **no `version` column** (baseline gave `Prompt` `UUIDPrimaryKeyMixin +
TimestampMixin + SoftDeleteMixin`, deliberately **omitting** `VersionMixin` that
`scenes` has). It is absent from `_VERSION_BUMP_TABLES` and carries only a
`touch_updated_at` trigger. So there is no per-row optimistic-concurrency token
on a prompt, and α6.1 had to define the concurrency model **without inventing a
migration**.

Without an ADR, a future contributor sees a child table with no `version`, an
API with no `412`, and a version-restore path that silently ignores prompts —
and cannot tell whether that is a **decision** or an **oversight** to be
"fixed." This ADR promotes it from implemented convention to recorded decision.

The physical facts α6.1 must honour (from the pre-flight §2):

- `prompts`: `project_id` (`ON DELETE CASCADE`), nullable `scene_id`
  (`ON DELETE SET NULL`), `kind` (`prompt_kind` enum), `text_content`, nullable
  `model_id` (`ON DELETE SET NULL`), server-owned `generated_by_agent`, `extra`
  JSONB, timestamps, `deleted_at`. **No `version`.**
- Ownership is **derived** through `project_id → projects.(tenant_id,
  owner_user_id)` — there is no `tenant_id`/`owner_user_id` on the row.
- `SET NULL` on the scene/model FKs fires **only on a hard parent `DELETE`** —
  and the API only ever *soft-deletes* scenes.

---

## Decision

### D1 — Prompts are generation inputs, not versioned editorial content

The governing principle, stated verbatim (the α6.1 sign-off requirement, Q13):

> **Project versions capture editorial state, not generation inputs. Prompts
> are mutable generation inputs that do not participate in aggregate optimistic
> concurrency, snapshots, restore, or diff. Generated media may retain the
> prompt used for provenance independently of the current prompt record.**

The versioned Project aggregate is **{project root + default storyboard +
ordered scenes}** (ADR-0035). Prompts — and, by the precedent this ADR sets,
media assets (α6.2) and timeline (α6.3) — sit **outside** that boundary. Scenes
are *editorial state*; prompts are *generation-pipeline artefacts* with their
own lifecycle. A prompt is closer to an inference request / generation parameter
than to the project's authored content.

### D2 — No per-row OCC; PATCH is last-writer-wins (α6.1 Q1 = Option A)

Given D1 and the baseline's deliberate omission of `VersionMixin`, prompts take
**no optimistic-concurrency control**:

- `PATCH …/prompts/{id}` is a plain project-gated update. **No `version` on the
  wire, no `412`.** Two racing edits: the last writer wins.
- A prompt mutation does **NOT** bump `projects.version`. The **Aggregate OCC
  Rule** (ADR-0035 D9) — which requires any aggregate *child* mutation to bump
  the root token — **does not extend to prompts**, precisely because prompts are
  not in the aggregate.

This is defensible because prompts are low-contention authored text (usually
created fresh, rarely co-edited), and it honours the schema's own signal.
Rejected alternatives (aggregate-OCC via `projects.version`; a new `version`
column) are recorded below.

The use case still detects a **same-value no-op** (no write / no `updated_at`
bump) and implements **tri-state PATCH** (`exclude_unset`): absent = unchanged;
explicit `model_id: null` clears the link; `text_content`/`kind` are
non-nullable so an explicit `null` is a `422`.

### D3 — Prompts are excluded from version snapshots / restore / diff (Q8)

The `project_versions` snapshot stays {project root + scenes}. Prompts are
**not** captured, **not** restored, **not** diffed. This is a **decision, not an
omission**: capturing prompts would enlarge the snapshot and couple
generation-pipeline state to editorial history — so restoring a project to an
old version would resurrect stale, experimental prompt wording, which is not the
mental model users hold ("restore structure / ordering / timing / narrative",
not every prompt iteration). Provenance, when needed, is retained **downstream**
(a media asset can hold its originating `prompt_id` / prompt snapshot — α6.2),
independent of the current prompt record.

### D4 — Two-level visibility gate; link failures are 422, not 404

Every endpoint is authenticated (`CurrentUserDep`, ADR-0034) and runs the
**project ownership gate first** (`projects.get_owned` → uniform `404` if
missing / soft-deleted / not the caller's), then the **prompt gate** (prompt
must be live under that project → else the same `404`; anti-enumeration).

Link validation is distinct from the visibility gate and yields **`422
VALIDATION_FAILED`, not `404`**: a non-null `scene_id` must reference a **live
scene in the same project** (via the scene repo); a non-null `model_id` must be
an existing `ai_models` row with status ≠ `retired` (via
`IPromptRepository.model_is_linkable` — the FK alone is `ON DELETE SET NULL` and
would otherwise accept a since-retired model). The route-target project is fine;
the *body* is what is invalid.

### D5 — `scene_id` is immutable; server owns identity + provenance

`scene_id` is set at create and **immutable** thereafter (no re-parenting in
α6.1). `id`, `project_id`, and `generated_by_agent` are server-owned — the
create/update DTOs use `extra="forbid"`, so any non-declared key (including
`generated_by_agent`, `id`, or a PATCH `scene_id`) is a `422`.
`generated_by_agent` stays server-`NULL` (AI authorship is α8).

### D6 — Scene-link durability across soft-delete and restore (F6)

Because `prompts.scene_id`'s `ON DELETE SET NULL` fires **only on a hard scene
`DELETE`**, and the API only ever *soft-deletes* scenes (and a version restore
soft-deletes-then-revives scenes under the same `id`), a prompt's `scene_id`
link **survives** both a scene soft-delete and a project restore. This is
asserted by a load-bearing repository integration test, so the durability is a
guaranteed property, not an accident.

---

## Alternatives Considered

1. **Aggregate OCC — fence prompt PATCH/DELETE/create on `projects.version`
   (α6.1 Q1 Option B).** *Rejected.* Migration-free, and consistent with scenes,
   but it couples prompt edits to scene edits (each invalidates the other's
   token) and logically implies prompts belong in snapshots — contradicting D3
   and enlarging the slice into snapshot/restore/diff participation. The baseline
   deliberately gave prompts no `version`; Option B fights that signal.

2. **Add a `version` column to `prompts` (α6.1 Q1 Option C).** *Rejected.* Full
   parity with scenes, but it breaks the no-migration discipline every α5 slice
   held, and prompts are not high-contention enough to justify row-OCC.

3. **Capture prompts in the version snapshot (α6.1 Q8 = yes).** *Rejected.* Would
   bloat the snapshot and make "restore" rewind experimental prompt wording,
   coupling generation state to editorial history. Provenance is better kept
   downstream on generated media (D3).

4. **Top-level `/prompts/{id}` addressing (the old API_CONTRACT stub).**
   *Rejected in favour of nesting* (α6.1 Q2): everything nests under
   `/projects/{project_id}/prompts`, consistent with scenes/versions and the
   two-level gate. A top-level route would need a global prompt→project resolve
   and diverge from every other child resource. The stub was reconciled.

5. **Allow `scene_id` re-parenting on PATCH.** *Rejected for α6.1* (Q10): moving
   a prompt between scenes is not yet a use case; `scene_id` is create-only.

---

## Consequences

- **Positive — clean editorial/generation boundary.** "Versioned editorial
  state" (project + scenes) vs "generation inputs" (prompts → media → timeline)
  is now explicit. A contributor will not "helpfully" wire prompts into
  `projects.version` or the snapshot builder.
- **Positive — small, migration-free slice.** α6.1 is CRUD + ownership +
  validation + filtering, not CRUD + OCC + snapshot/restore/diff participation.
- **Positive — future history stays open.** If prompts ever need audit/history,
  a `prompt_runs` / `prompt_revisions` table can be added later **without**
  changing the meaning of project snapshots — far easier than removing prompts
  from version history after the fact.
- **Contract — no `version` on the prompt wire.** Clients must not expect a
  `version` field or a `412` on prompt PATCH; a prompt PATCH is
  last-writer-wins. `PromptPublic` = `{id, project_id, scene_id, kind,
  text_content, model_id, extra, created_at, updated_at}`.
- **Contract — restore does not touch prompts.** A project restore neither
  captures nor rewrites prompts; prompt→scene links survive it (D6).
- **Precedent — later generation aggregates inherit this.** Media assets (α6.2)
  and timeline (α6.3) follow D1/D3: outside the editorial snapshot, with their
  own lifecycle; media may retain prompt provenance independently.

---

## Pattern Reference (Examples)

- **Domain:** `app/domain/prompts/prompt.py` (frozen `Prompt`, no `version`).
- **Repository:** `app/infrastructure/repositories/prompt_repository.py`
  (`SqlAlchemyPromptRepository`: `add`, `list_owned` + filters, `get_owned`,
  `update_owned` — no OCC fence, `soft_delete_owned`, `model_is_linkable`).
- **Use cases:** `app/application/use_cases/prompts/*` — `CreatePrompt`
  (two-level gate + scene/model link validation → `422`), `ListPrompts`,
  `GetPrompt`, `UpdatePrompt` (same-value no-op, tri-state), `DeletePrompt`
  (idempotent-by-404). None call `IProjectRepository.touch_version`.
- **DTOs / router:** `app/api/v1/schemas/prompts.py`,
  `app/api/v1/routers/prompts.py` (nested under `/projects/{project_id}/prompts`;
  no `version` on the wire, no `412`).
- **F6 durability:** `tests/integration/.../test_prompt_repository.py`
  (prompt `scene_id` survives scene soft-delete).

New generation-pipeline aggregates copy these shapes rather than reinventing
them.

---

## Future Extensions

- **α6.2 — Media assets** (`/projects/{id}/assets`) — generated *from* prompts;
  `media_assets.prompt_id` retains prompt provenance independent of the current
  prompt record (D1/D3). Owns generation history.
- **α6.3 — Timeline / tracks / clips** — assets placed on the timeline; same
  outside-the-editorial-snapshot stance.
- **α8+ — AI-authored prompts** — populate `generated_by_agent`; add
  `prompt_runs`/`prompt_revisions` if prompt history is ever required, without
  changing project-snapshot semantics.
