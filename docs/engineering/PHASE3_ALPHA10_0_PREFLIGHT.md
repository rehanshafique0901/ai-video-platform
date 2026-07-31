# Phase 3 — α10.0 Pre-flight: Identity-Runtime Authoring

> **Status:** **Approved 2026-07-31**, and reconciled against the accepted ADR-0055 in a recorded
> pass (§14). Implementation begins at §12 step 1 once the governance baseline is committed.
>
> **Slice:** α10.0 — durable, creator-authored world state (characters, locations, props, project
> look, seed) bound into a generation at request time.
> **Target version:** `0.4.53-phase3-alpha10.0-dev` → `0.4.53-phase3-alpha10.0`
> **Baseline:** `v0.4.52-phase3-alpha9.9`
> **Grounding:** [`PHASE3_ALPHA10_0_GROUNDING.md`](./PHASE3_ALPHA10_0_GROUNDING.md)
> **Governed by:** [**ADR-0055**](../decisions/ADR-0055-identity-runtime-authoring.md) — **Accepted
> 2026-07-31**. Its five decisions and nineteen frozen decisions are normative; this document
> implements them and does not reopen them (§10, §14).

---

## 1. What this slice is, and what it is not

**It is:** a new owner-scoped bounded context that lets a creator author and keep a *world* —
named characters with stable appearance, a location, recurring props, a project look, and a stable
seed — and name that world when they request a generation, so that the six shots of a video are
about the same people in the same place.

**It is not:** a change to how shots are planned, prompted, resolved, dispatched, verified, or
rendered. Every one of those paths already consumes `IdentityProfile` and is left untouched. This
slice supplies the profile; it does not change what the pipeline does with one.

The single organising principle, inherited from α9.9's DISP-1 and applied here to authoring:

> **v1 accepts only what v1 can honour.** No field is authorable if no executable path consumes it.
> Offering a creator a control the deployment silently discards is the same defect class α9.9
> removed from provenance, one layer up.

---

## 2. Rulings — proposed here, signed off 2026-07-31

*The ten rulings below were this document's proposals. All ten were approved on 2026-07-31 and are
now governed by the accepted ADR-0055; §14 maps each to its governing decision. They are preserved
as written — implementation follows them and does not reopen them.*

### PF1 — a new bounded context with a relational model, not a JSONB body

`app/domain/identity_runtime/` with an `IdentityProfile` root and `Character` / `Location` / `Prop`
children, persisted as one parent table plus three child tables (§3). The alternative — a single
`identity_profiles.body` JSONB on the `templates` precedent — is rejected because this is *mutable*
creator state, not an immutable record: it needs partial updates to one character, per-row
uniqueness of a character's stable `id` inside its profile, and DB-owned invariants. JSONB in this
repository is reserved for frozen snapshots (`generations.request`, `publish_jobs.content_package`),
which is precisely what PF2 makes the binding, not the source.

### PF2 — a generation binds identity by **snapshot**, never by reference

At ingress, the named profile is read once, validated, and **serialised in full into the
ingress-owned `generations.request` JSONB**. `generations.identity_id` records *which* profile the
snapshot came from, and the snapshot carries the profile's `version` — both as provenance values,
with **no foreign key** (ADR-0046 X5: "values, not FKs").

Consequences, all intended: editing a character tomorrow cannot change what a generation executed
yesterday, or what it would replay as; a queued generation claimed twenty minutes later runs the
world the creator asked for, not the world as since edited; and PF10's hard delete stays safe.
This is DISP-2's reasoning transplanted — capture the binding at the moment it is used, never
re-derive identity later from a mutable source.

### PF3 — `identity_id` is reclassified as ingress-owned and removed from the runtime upsert

`generation_ledger_repository.py:61` currently lists `identity_id = EXCLUDED.identity_id` in the
execution store's `ON CONFLICT DO UPDATE`, and `begin()` always supplies `None`. An
ingress-written value would be erased on the first status write. Under **GEN-1** the fix is a
one-line deletion from the enumerated column list plus removal of the `None` parameter, which
restores the invariant's own guarantee ("the `ON CONFLICT` clause enumerates only runtime-owned
columns, so the prohibitions hold by construction"). The execution runtime stays ownership-blind
and identity-blind: `GenerateVideoRequest` gains no new field, and no execution-plane module learns
what a profile is.

### PF4 — `SPEC_VERSION = 2`, with an explicit decode path for v1 rows

`decode_spec` rejects any version but the current one, so a bump without a compatibility path would
make every generation α9.7 queued undecodable. v2 adds one optional nested `identity` object; a v1
payload decodes to a spec whose identity is absent, reconstructing today's `seed` + `global_style`
profile exactly. Rows are never rewritten in place.

### PF5 — reference images are **excluded** from v1 authoring

`ReferenceImage` stays a domain value object with no table, no route, and no request field. The
only registered adapter accepts `reference_image_refs` and discards them
(`pollinations_image_generator.py:45–47`), so authoring them would sell image conditioning this
deployment cannot perform. They arrive with the first adapter whose executable capability includes
reference conditioning — at which point α9.9's executable-set machinery is exactly the mechanism
that decides whether the control may be offered. This also keeps the byte-upload surface (which
does not exist anywhere in the generation plane) out of the slice.

### PF6 — bounded cardinality: at most 4 characters, at most 1 location, at most 6 props

Not arbitrary product limits — each is the number the current Decision plane can honour. The
planner casts **every** character into **every** shot (`planner.py:120`) and uses only the
**first** location (`:123`); the prompt builder emits **every** prop (`prompt_builder.py:95`). A
fifth character makes every prompt worse, and a second location is inert. Caps are enforced at
authoring time with a clear validation error. Richer worlds require per-shot casting, which is a
Decision-plane slice of its own (§9).

### PF7 — the planner and prompt builder are not modified

Not one line of `planner.py`, `shot_intent.py`, `storyboard.py`, or `prompt_builder.py` changes.
This keeps the slice clear of ADR-0045 F3/F7 and CS-1/CS-2 behaviour, and means the prompt
composition path arrives already tested. It is also what makes PF6 a *constraint* rather than a
preference: the caps exist because the untouched planner has these rules.

### PF8 — OCC on the profile root; children are written through the root

The profile carries `version`; every child mutation bumps it. Per-child OCC is rejected because
PF2 snapshots the whole world at once — a snapshot must never straddle two states — and because
"the world changed under me" is the only conflict a creator can act on.

### PF9 — owner-scoped, not project-scoped

A profile belongs to `(tenant_id, owner_user_id)`, with a required unique `name` per owner. No
`project_id`: generations themselves are not project-scoped (`0016` adds tenant/owner only), and
coupling identity to projects now would invent a relationship neither side has.

### PF10 — hard delete is permitted

Because PF2 makes past generations independent of the live profile, deletion needs no soft-delete
tombstone and no reference counting. A deleted profile's `identity_id` remains in old rows as a
provenance value that no longer resolves — which is the honest record: that world existed, and no
longer does.

---

## 3. Data model (migration `0017`)

| Table | Key columns | Notes |
|---|---|---|
| `identity_profiles` | `id` uuid PK, `tenant_id`, `owner_user_id`, `name`, `seed` bigint, `global_style`, `camera_style`, `lighting`, `color_palette`, `negative_prompt`, `version` int, `created_at`, `updated_at` | `uq_identity_profiles_owner_name` unique `(owner_user_id, name)`; `ix_identity_profiles_owner_created` keyset index on `(owner_user_id, created_at DESC, id DESC)` |
| `identity_characters` | `id` uuid PK, `profile_id` FK → `identity_profiles` ON DELETE CASCADE, `character_key` text, `name`, `age`, `appearance` text[], `clothing`, `accessories` text[], `position` int | `uq_identity_characters_profile_key` unique `(profile_id, character_key)` — `character_key` is the stable id the planner and shot records carry |
| `identity_locations` | as above, `location_key`, `name`, `descriptors` text[] | `uq_identity_locations_profile_key` |
| `identity_props` | as above, `prop_key`, `name`, `descriptors` text[] | `uq_identity_props_profile_key` |

`music_style`, `subtitle_style`, `voice`, `personality`, `expressions`, `poses` and every reference
field are **omitted** — no v1 path consumes them (PF5, and `identity.py:76–80` already excludes
expressions/poses from the stable fragment). `downgrade()` drops children then parent.

**Closed to, by ADR-0055 frozen decision 19:** none of these tables may gain a column for execution
history, planner decisions, rendering or adapter preference, adapter health, success statistics, or
verification outcomes. A profile records what the creator declares exists, never what happened.

## 4. API surface — `/api/v1/identities`

Owner-scoped, envelope + keyset pagination + OCC exactly as the library slice (α9.2) does it.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/identities` | create profile (children optional, inline) → 201 |
| `GET` | `/identities` | keyset list, `cursor` + `limit` |
| `GET` | `/identities/{id}` | full profile with children |
| `PATCH` | `/identities/{id}` | root fields; requires `version` → 412 on conflict |
| `DELETE` | `/identities/{id}` | 204 (PF10) |
| `POST`/`PATCH`/`DELETE` | `/identities/{id}/characters[/{key}]` | children; each bumps profile `version` (PF8) |
| …same for | `/locations`, `/props` | |

Schemas use `extra="forbid"`; a foreign or missing id is a uniform 404 (anti-enumeration).

**Closed to, by ADR-0055 frozen decision 18:** no endpoint ranks, scores, annotates, or recommends
identity options, and none reports what "works well". The API offers controls; preference is the
Decision plane's and arrives, if ever, through its own contract.

## 5. Binding at ingress

`GenerationCreateRequest` gains one optional field, `identity_id`. When present, `CreateGeneration`
loads the owner's profile (404 if absent), validates caps, builds the nested identity payload, and
persists it inside the v2 spec; `resolve_seed` prefers an explicit request seed, then the profile
seed, then a drawn one. When absent, behaviour is byte-for-byte today's. `to_runtime_request`
rebuilds the full `IdentityProfile` from the snapshot — the only function in the codec that changes
shape.

Three constraints carried from the accepted ADR:

- **The snapshot is the only channel** (frozen decisions 4–5). The profile seed and every other
  identity value reach the runtime through the decoded payload and nowhere else; no execution-side
  code performs a second lookup. That is what keeps the two writers of `generations.seed` unable to
  disagree (ADR-0055 D4, *checked and clear*).
- **Identity is excluded from the idempotency key** (frozen decision 14). A replayed
  `idempotency_key` returns the original generation with its original snapshot even if the caller
  names a different profile the second time; content is never hashed (ADR-0052 D4-A).
- **An undecodable payload fails loudly** (frozen decision 7) and terminalises the generation. No
  world is reconstructed by guesswork and no partial identity is substituted.

## 6. Architectural dependencies

**Code, upstream:** α9.7 generation ingress (the resource, the codec, `generations.request`);
α9.8 worker runtime host (executes what ingress queues); the α8.5x/α8.7 Decision plane
(`identity.py`, `planner.py`, `prompt_builder.py`) — consumed unchanged; α9.2 library and
`media_assets` — *not* used in v1, but the future home of reference images (PF5).

**Governance:** ADR-0045 **F3/F7** (planner names no provider, no provider-specific logic — this
slice adds no logic to the planner at all); ADR-0046 **X2/X3/X5** (Execution consumes an immutable
plan; provenance as values); ADR-0052 **D1/D4** and **GEN-1/GEN-3** (ownership model, idempotency,
the write boundary PF3 corrects); ADR-0054 **DISP-1/2/3** (§7); `CINEMATIC_STORYBOARD_CONTRACT`
**CS-1/CS-2** (identity constant across shots — this slice makes CS-2 authorable for the first
time); ADR-0042 frozen paths — **none touched** (the nineteen guarded paths are all orchestration).

**Infrastructure:** migration `0017`; `IUnitOfWork` gains `identities`; container factories and
`deps.py` aliases; CI **Stage 27**; import-linter needs **no new contract** (the context fits the
existing layer rules).

**External:** **none.** No provider, key, account, or review.

## 7. Invariants inherited from α9.9, and how α10.0 honours them

| Invariant | Obligation on this slice |
|---|---|
| **DISP-1** — resolution is well-formed only against a declared executable set | Identity must never become a resolver input. It is consumed *upstream* of resolution, by the planner and prompt builder; `resolve()` keeps its four inputs, and `ExecutableAdapters` keeps coming from the registry's keys alone. The invariant's **spirit** — never offer what the deployment cannot construct — is what PF5 enforces at the authoring surface |
| **DISP-2** — a record naming an adapter names the one that produced the bytes | Untouched, and mirrored: identity provenance is asserted by the **snapshot captured at request time**, never by re-reading a mutable profile at execution time. `identity_id` + the snapshot's `version` are **request facts** (ingress-owned), and are neither decision facts nor execution facts — a third column of the ADR-0054 field-classification table, not a blurring of its two |
| **DISP-3** — Execution never follows an ordering the Decision plane did not compute | Untouched. This slice introduces no ordering, no preference, no candidate handling, and no fallback of any kind |
| **Frozen decision 8** — identity asserted by the binding, never by self-report | The direct analogue is PF2: the generation's world is asserted by what ingress resolved and stored, never by whatever the profile happens to say later |
| **GEN-1** — ingress owns identity, the runtime owns execution state | PF3 corrects the one place the code does not yet match the invariant |
| **GEN-2** — one execution is one spend opportunity | Unaffected: no retry, requeue, or additional provider call is introduced |
| **GEN-3** — ownership is never inferred | Profiles are owner-scoped from creation; no backfill, no attribution of pre-α10.0 generations |
| **HOST-1/HOST-2** | Unaffected: no new worker, no change to registration or supervision |

**New invariants, ratified in ADR-0055 (2026-07-31) and normative for this slice:**

- **IDENT-1** — *a generation's world is a value captured at acceptance, never a live reference.*
  Mutating or deleting a profile never changes the meaning, provenance, or replay of an existing
  generation.
- **IDENT-2** — *only honourable state is authorable.* No identity field is accepted through the
  API unless a path in the deployment consumes it; capability, not taste, sets the boundary — and
  capability answers *can*, never *should*, so it never expresses quality, preference, routing,
  ranking, cost, historical success, or a planner decision.
- **IDENT-3** — *authoring supplies world state, never policy.* Casting, ordering, routing and
  shot decomposition remain the Decision plane's; identity never becomes a lever over them.
- **IDENT-4** — *identity is knowledge, never measurement.* A profile never accumulates execution
  outcomes, quality feedback, usage statistics, or per-adapter tuning, and no Decision-plane
  behaviour may become a function of such data (§3 and §4 carry the resulting prohibitions).

## 8. Scope decisions

| Item | In α10.0? | Reason |
|---|---|---|
| Profile + characters + locations + props, CRUD | **In** | The slice |
| Snapshot binding at ingress, spec v2 | **In** | PF2/PF4 — the point of the slice |
| `identity_id` ownership correction | **In** | PF3; a GEN-1 correctness defect this slice would otherwise trip over |
| Reference images / image conditioning | **Deferred** | PF5 — no executable adapter consumes them |
| Per-shot casting (which character appears in which shot) | **Deferred** | Decision-plane change; PF7 keeps the planner untouched |
| Multiple locations | **Deferred** | The planner anchors to one (PF6) |
| Voice, personality, expressions, poses, music/subtitle style | **Deferred** | No consuming path — audio and lip-sync slices own them |
| Project ↔ identity association | **Deferred** | PF9 |
| Identity templates / a starter gallery | **Deferred** | The `templates` table has no code at all; its own slice |
| Character embeddings / similarity | **Deferred** | Guarded — `test_metadata.py` permits embeddings on two columns only, and a third needs an ADR |

## 9. Risks and residuals

1. **Prompt-length growth.** Four characters plus six props plus the look block make a long prompt;
   Pollinations is tolerant, but a future adapter may not be. Bounded by PF6, not solved by it.
2. **Caps will feel arbitrary to a creator** who wants a fifth character. They are honest about the
   planner's current behaviour, and the ceiling lifts exactly when per-shot casting ships.
3. **Snapshot duplication.** Every generation stores its own copy of the world. That is the
   intended cost of IDENT-1, and matches how `publish_jobs` treats its content package.
4. **A dangling `identity_id`** after deletion resolves to nothing. Intended (PF10), and the reason
   it is a value and not an FK.
5. **The v1/v2 decode path is the slice's sharpest edge.** A mistake there mis-reads a queued
   creator request. It is the first thing the test plan pins.

## 10. Architectural decision check — **ADR-0055, Accepted 2026-07-31**

*Historical record.* When this section was written the ADR did not exist. Unlike α9.9, the slice
could not proceed under an existing one: it creates a bounded context, changes a persisted request
payload's version, and reclassifies the ownership of a `generations` column. The decision points
raised here are now **settled**; the table records what this document proposed and where the
accepted ADR ruled.

| # | Question raised here | Pre-flight's recommendation | Settled in ADR-0055 as |
|---|---|---|---|
| **D1** | Is authored identity a value snapshotted into the request, or a live reference resolved at execution? | Snapshot (PF2) → IDENT-1 | **D2** + IDENT-1; frozen decisions 4–6 |
| **D2** | Which plane owns `generations.identity_id`? | Ingress; remove from the runtime upsert (PF3) | **D4** (identity as a *request fact*); frozen decisions 12–13 |
| **D3** | What bounds authorable identity — product judgement or executable capability? | Capability (PF5/PF6) → IDENT-2 | **D5** + IDENT-2; frozen decisions 15–16, 18 |
| **D4** | Does authoring gain any influence over casting or routing? | No (PF7) → IDENT-3 | **IDENT-3**; frozen decision 17 |

Every recommendation was adopted unchanged. The ADR additionally settled two questions this section
had not raised as decision points, both already implicit in PF1 and PF4: the bounded context's home
and plane (**D1**, frozen decisions 1–3) and payload versioning with its compatibility and
migration guarantees (**D3**, frozen decisions 8–11).

## 11. Test plan and gate impact

**Unit** — domain: profile invariants, cap enforcement, deterministic ordering of children,
`prompt_fragment` composition unchanged. Codec: v1 payload still decodes; v2 round-trips; unknown
keys still rejected; a v2 payload with no identity equals today's behaviour exactly. Use cases:
404-before-412, OCC bump on child writes, name uniqueness.

**Integration (new Stage 27, `tests/integration/api/test_identity_runtime.py`)** — author a world →
create a generation naming it → assert the persisted `request` carries the full snapshot and
`identity_id` is set → run the pipeline → assert `identity_id` **survives** `begin()` (the PF3
regression test) and that shot prompts contain the character and location fragments; then **edit
the profile and re-read the generation**, asserting nothing about the completed run changed; then
**delete the profile** and assert the past generation still reads and still reports its provenance.

**Must stay green untouched:** Stage 13 (generation end-to-end, incl. the α9.9 dispatch and
wrong-binding tests), Stage 25 (generation ingress), Stage 26 (worker host), and the frozen-platform
check.

## 12. Implementation order

Each step is independently reviewable and leaves the gate green.

1. **Domain** — `app/domain/identity_runtime/` profile + children, caps, validation. Unit tests.
2. **Migration `0017` + ORM declarations** — four tables, indexes, symmetric `downgrade()`;
   `models/identity_runtime.py`; `validate_schema.py` / ERD round-trip updated. The ORM
   declarations belong here and not in step 3 because the schema validator fails on a table with
   no model, so a migration alone cannot leave the gate green (recorded in §14).
3. **Repository** — `IIdentityRepository` on `IUnitOfWork`,
   `infrastructure/repositories/identity_repository.py` (`uuid4()`, `IntegrityError` →
   `ConflictError`, keyset list).
4. **Use cases** — create / get / list / update / delete profile, plus child add / update / remove.
5. **API** — `schemas/identity.py`, `routers/identity.py`, `deps.py` aliases, container factories,
   `main.py` registration.
6. **Codec v2** — `SPEC_VERSION = 2`, nested identity, v1 compatibility, `to_runtime_request`
   rebuild. Unit tests first; this is the sharpest edge (§9.5).
7. **Ingress binding** — `identity_id` on the create request, profile load + snapshot in
   `CreateGeneration`, seed precedence.
8. **PF3 correction** — drop `identity_id` from the runtime upsert enumeration; write it from
   ingress; regression test that `begin()` cannot null it.
9. **Stage 27** — integration test + `ci_gate.py` registration + docstring stage list.
10. **Full canonical gate** on an ephemeral database.
11. **Docs** — ADR-0055 (ratified before step 1 lands), this pre-flight's implementation record,
    `PLATFORM_STATUS` (capability row, invariant catalog entries IDENT-1..3, roadmap row retired),
    `SYSTEM_MAP` (an Identity Runtime row), `EXECUTION_RUNTIME_CONTRACT` §3 ownership table,
    `API_CONTRACT`, `CHANGELOG`.
12. **Release** — version bump to `-dev`, review, finalise, merge, tag `v0.4.53-phase3-alpha10.0`.

Branch: `feat/alpha10.0-identity-runtime-authoring`.

---

## 13. What approval of this document meant

*Historical record — approval was given on 2026-07-31.* Approving this pre-flight approved ten
rulings, a four-table migration, a new public resource, a persisted-payload version bump, one
ownership correction inside the α9.7 write boundary, and four invariants — and deferred reference
images, per-shot casting, and multi-location worlds with the reasons stated in §8. Approval alone
did not authorise implementation: **ADR-0055 was ratified first** (Accepted 2026-07-31), and the
reconciliation recorded in §14 followed it. Implementation begins at §12 step 1 once the governance
baseline commit lands.

---

## 14. Reconciliation record — PF1–PF10 against the accepted ADR-0055

Performed at acceptance on 2026-07-31, as the ordering note in ADR-0055 requires. Because the
grounding and this pre-flight were written before the ADR, its rulings were provisional until the
decisions existed; the pass asked one question of each: *does it contradict a decision, an
invariant, or a frozen decision of the accepted ADR?* **None did.** No ruling was withdrawn, none
was added, and the slice's scope (§8) is unchanged.

| Ruling | Governing decision | Verdict |
|---|---|---|
| **PF1** — new bounded context, relational model | D1; frozen 1–3 | Consistent. D1 adds the Knowledge-plane classification and the negative-ownership list, folded into §3 |
| **PF2** — bind by snapshot, never by reference | D2, IDENT-1; frozen 4–6 | Consistent |
| **PF3** — `identity_id` ingress-owned, removed from the runtime upsert | D4; frozen 12–13 | Consistent; reclassified as a *request fact* rather than merely an ownership move |
| **PF4** — `SPEC_VERSION = 2` with v1 compatibility | D3; frozen 8–11 | Consistent. D3 adds the no-rewrite and no-backfill guarantees, folded into §5 |
| **PF5** — reference images excluded from v1 | D5; frozen 16 | Consistent |
| **PF6** — bounded cardinality | D5; frozen 15 | Consistent. The ADR fixes the *rule*; the numbers stay here (ADR open question 1) |
| **PF7** — planner and prompt builder untouched | IDENT-3; frozen 17 | Consistent |
| **PF8** — OCC on the profile root | frozen 3 | Consistent |
| **PF9** — owner-scoped, not project-scoped | D1, rejected alternative 8 | Consistent |
| **PF10** — hard delete permitted | D2; frozen 6 | Consistent |

**Four frozen decisions had no counterpart among the rulings and were folded into the body rather
than left implicit:** 7 (an undecodable payload fails loudly) and 14 (identity is excluded from the
idempotency key) into §5; 18 (*can*, never *should*) into §4; 19 (negative ownership) into §3.
IDENT-4 was added to §7 alongside them, taking the slice from three new invariants to four.

**Path correction, made after the baseline commit and before step 1.** PF1 and §12 named
`app/domain/identity/` and `models/identity.py`. Both are already taken by the **authentication**
context (`User`, `Tenant`, `Session`, and their ORM module, α2a). Writing the profile aggregate
there would have merged two bounded contexts and contradicted the decision it implements, so the
domain package is `app/domain/identity_runtime/` and the ORM module `models/identity_runtime.py`.
The rename stops at the collision: the resource stays `/api/v1/identities`, the API modules stay
`schemas/identity.py` and `routers/identity.py`, the repository stays `identity_repository.py` with
`IIdentityRepository` on `IUnitOfWork` (neither exists today — auth uses `user`, `tenant` and
`session` repositories), and the auth context is not touched. ADR-0055 D1 carries the same
correction. No ruling, table, endpoint, invariant, or step changed.

**Step-boundary correction, made during step 2.** §12 put the migration in step 2 and the ORM
declarations in step 3, which the preamble's own promise — every step leaves the gate green —
cannot satisfy: `scripts/validate_schema.py` fails on any base table with no SQLAlchemy model,
and the allowlist for that is reserved for tables that are ORM-less *by design* (the provider
catalogue, the execution-runtime tables). The α10.0 tables are ORM-mapped, so
`models/identity_runtime.py` moved into step 2. Nothing else moved: the repository, the
`IIdentityRepository` port, the `IUnitOfWork` slot, container and API wiring all remain step 3 and
beyond. No table, column, index, endpoint, ruling or invariant changed.

**Left open deliberately.** ADR open questions 2–4 — a child's stable-key provenance, child-write
granularity, and whether a profile may be created inline with a generation — are implementation
choices inside §12 and are not pre-empted here. Open questions 5–6 — which slice lifts the location
cap, and when v1 payload retirement becomes worthwhile — belong to later slices and are not opened
by this one.
