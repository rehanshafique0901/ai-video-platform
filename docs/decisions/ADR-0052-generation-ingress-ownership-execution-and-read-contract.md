# ADR-0052 — Generation Ingress: Ownership, Execution Model & Read Contract

**Status:** **Accepted** (Phase 3, α9.7 — Generation Ingress, 2026-07-29). Governance that
**precedes** implementation (like ADR-0044/0045/0046/0047/0048/0049/0050/0051): it fixes how a
creator's generation request enters, executes in, and is read back from the platform **before any
ingress code exists**. Drafted at the α9.7 grounding stop
([`PHASE3_ALPHA9_7_GROUNDING.md`](../engineering/PHASE3_ALPHA9_7_GROUNDING.md)), **amended** to make
the legacy-row migration philosophy explicit (see D1), and **Accepted** with that amendment. The α9.7
pre-flight follows; **no implementation accompanies this ADR.**

**Amendment (2026-07-29, pre-acceptance).** The D1 migration recommendation was unchanged in
mechanics but strengthened in rationale: legacy ownerless generations are now explicitly classified as
**implementation artefacts rather than domain objects**, their invisibility as **preservation of
ownership correctness rather than data loss**, and **ownership inference of any kind is absolutely
prohibited**. Administrative tooling may explicitly assign or purge such rows, but never through the
production API, and **owner-scoped reads remain the sole supported read model**. This amendment
introduced **no new architectural question**.

**Accepted decisions (summary).** **D1-A** — generations become **owner-scoped immediately**
(`tenant_id` + `owner_user_id` on `generations`); **this requires a database migration and the ADR
does not attempt to avoid one**. **D2-B** — **queued background execution** on the established
create→queue→worker→poll pattern, which the `generations` table was already shaped for. **D3-A** —
**HTTP polling only** for v1, over a curated projection. **D4-A** — **client-supplied idempotency
key** with a DB unique backstop, reusing the α7.1 render-job precedent; **never** server-derived
content hashing. **D5-C** — a **dedicated `/api/v1/generations` resource**.

**Builds on:**
- **ADR-0042** (orchestration/platform freeze — ingress plugs in **additively**; the frozen runner,
  relay, and outbox are not edited).
- **ADR-0045** (Decision/Execution plane split — ingress and ownership are Execution-plane concerns;
  no policy moves into the API).
- **ADR-0046** (Execution Runtime boundaries X1–X8, and **Q1**, which recorded that generation has
  no tenant/owner/project context **yet** — this ADR resolves that deferral).
- **ADR-0048** (DB-owned idempotency: prefer a database constraint over application dedup).
- **ADR-0051** (read-model hygiene: internal bookkeeping must never leak into a public contract).

---

## Context

Every product capability the platform offers is reachable over HTTP — projects, scenes, prompts,
media, timeline, render, export, publish, library, notifications, analytics, dashboard — **except the
one the product is named for.** `GenerateVideo`
(`app/application/use_cases/generation/generate_video.py:86`) is complete and container-wired
(`container.py:1520`), but has no router and no `deps.py` entry. It is reachable only from
`scripts/generate_demo.py` and tests.

### The decisive facts

Four facts, all verified during grounding, constrain every option below.

**F1 — `generations` is already shaped like a job table.** Migration `0012` defines
`generation_status` as `queued → planning → resolving → generating → verifying → repairing →
rendering → exporting → completed | failed | cancelled` (`0012_execution_runtime.py:50-62`), defaults
the column to **`queued`**, and creates **`ix_generations_status`** (`:144`) — precisely the claim-scan
index a poll-ingress worker needs. The table already anticipates queued execution and cancellation.
**No worker exists to drive it** (no `GenerationWorker` anywhere in `backend/`).

**F2 — Generation state carries no ownership.** `generations`, `generation_shots`, and
`generation_assets` have **no `tenant_id`, `owner_user_id`, or `project_id`**
(`0012_execution_runtime.py:99-207`). This was deliberate (**ADR-0046 Q1**) and α8.8 recorded the
deferral verbatim: "**AP9 — project-asserted, generation-unowned** … that is deferred to the future
generation-trigger slice" (`promote_generation_assets.py:24`). **This slice is that slice.**

**F3 — There is a latent authorization gap that ingress would make live.**
`PromoteGenerationAssets.execute()` authorizes the **project link** for the caller
(`:104-110`) and then calls `load_final_video(generation_id)` (`:118`) with **no ownership predicate
on the generation**. Today this is unreachable — no endpoint mints or lists a `generation_id`, so no
caller can learn one. **The moment generation ingress exists, generation ids become client-visible**,
and a caller who knows another tenant's id could promote that video into their own project. D1 must
close this; it is not a hypothetical.

**F4 — Execution is long and expensive.** `execute()` blocks until terminal: it loops over shots
(~6 for the default 18s/3s scenario) with up to `DEFAULT_MAX_ATTEMPTS = 3` each, calling
`IImageGenerator.generate()` inline (Pollinations timeout default **120 s**), then runs ffmpeg inline
(`render_timeout_seconds` default **900 s**). Worst case is comfortably **tens of minutes**, and every
attempt spends **real provider money**. Cost asymmetry drives D2 and D4 the way PUB-11's
duplicate-post asymmetry drove the publishing ADRs.

### Decision points

| # | Question |
|---|---|
| **D1** | Who owns a generation, and how is that expressed in the schema? |
| **D2** | Does a generation run inside the request, or as queued background work? |
| **D3** | How does a client observe progress? |
| **D4** | How are duplicate/ambiguous create requests prevented? |
| **D5** | What resource does generation expose, in the platform's existing vocabulary? |

---

## D1 — Ownership model

### Options

| | Option | Shape |
|---|---|---|
| **A** | **Owner-scoped immediately** *(recommended)* | Add `tenant_id` + `owner_user_id` (+ optional `project_id`) to `generations`; every read carries an owner predicate |
| **B** | Ownerless; bind ownership only after promotion | Status quo (AP9). Generations stay anonymous; only the promoted `media_asset` is owned |
| **C** | Bind to projects only | Add `project_id` to `generations`; ownership is derived transitively by joining `projects` |
| **D** | Owned wrapper table | New `generation_jobs` table (owner columns + lifecycle) referencing the unowned `generations` row, mirroring `publish_jobs` |

### Evaluation

| Criterion | A — owner columns | B — ownerless | C — project only | D — wrapper table |
|---|---|---|---|---|
| **Consistency with owner-scoped architecture** | **Exact match** — every other read derives `tenant_id` + `owner_user_id` from `CurrentUserDep` and filters directly | **Incompatible** — no predicate exists to scope a read | Partial — ownership is real but **indirect**, requiring a join on every read | Match, but ownership lives on a *different row* than the state being read |
| **Interaction with α8.8 promotion** | **Closes F3**: promotion can add a generation-ownership check alongside the existing project check | **Leaves F3 open** and makes it exploitable | Closes F3 via join; a project transfer would silently move generations with it | Closes F3, but promotion must join the wrapper to reach the generation |
| **Security** | **Strongest** — direct predicate, defence in depth, no trust in join correctness | **Weakest** — any caller knowing a UUID can promote it (F3) | Adequate; correctness depends on every read remembering the join | Adequate; correctness depends on wrapper/generation staying in sync |
| **Query model** | Single-table predicate; one composite index serves keyset listing | No owner query possible — "list my generations" is **unbuildable** | Join `generations × projects` on every list/read | Join, plus a second row to keep consistent |
| **Future teams / RBAC** | **Best** — matches the anticipated relaxation (drop `owner_user_id`, keep `tenant_id`) | n/a | Works, but team visibility becomes a property of the project only | Works; the wrapper becomes the RBAC surface |
| **Migration** | **Required** — add 2–3 columns + index | **None** | **Required** — add 1 column + index | **Required** — new table, FK, indexes |
| **Determinism** | Unaffected | Unaffected | Unaffected | Unaffected |
| **Operational simplicity** | **Highest** — one row, one lifecycle, one status machine | Simplest today, unusable tomorrow | Moderate | **Lowest** — dual-write, dual lifecycle, and it **duplicates the `generation_status` machine that F1 shows already exists** |

### Why not D, despite the `publish_jobs` precedent

A wrapper table is the natural instinct — `publish_jobs` is exactly that for the publish runtime, and
it preserves ADR-0046's plane separation. It is rejected because **F1 removes the reason it exists**.
`publish_jobs` was created because there was no durable publish row; `generations` already has the
full `queued → … → cancelled` state machine, the status index, and the lifecycle columns. A wrapper
would stand up a *second* status machine over the first, and every transition would need mirroring —
a dual-write correctness burden with no compensating benefit.

### Relationship to ADR-0046 Q1

Adding ownership **does not reverse Q1**. Q1 ruled on *asset persistence* (a distinct
`generation_assets` table rather than reusing `media_assets`); its remark that generation "has no
tenant/owner/project/publishing context **yet**" was stated context, not a prohibition, and AP9
explicitly scheduled the binding for this slice. D1-A is the **extension Q1 anticipated**.

### Recommendation — D1-A, and it requires a migration

**Add `tenant_id` and `owner_user_id` to `generations`, plus a composite index supporting the
owner-scoped keyset list.** An optional nullable `project_id` may be added in the same migration to
associate a generation with a project; whether v1 populates it is a pre-flight question, not an
architectural one.

**A database migration is required. This ADR does not attempt to avoid it.** Options B (no migration)
and C (smaller migration) were evaluated on merit and are inferior: B cannot express the product
requirement at all, and C buys a marginally smaller migration at the cost of indirection on every read
plus surprising transfer semantics.

**Migration shape (recommended, pre-flight to finalise).** Existing `generations` rows have no owner
and cannot be backfilled truthfully. The columns are therefore added **nullable**, with all
owner-scoped reads filtering on the predicate — so legacy rows are simply invisible rather than
misattributed. Tightening to `NOT NULL` is a later, deliberate migration once no legacy rows remain.

#### Migration philosophy — legacy ownerless generations

This is a statement of principle, not merely a description of mechanics. It binds the pre-flight and
any future slice that touches these rows.

1. **Historical ownerless generations are legacy implementation artefacts, not user-visible domain
   objects.** They were produced by `scripts/generate_demo.py` and the test suite while the execution
   runtime had no identity context at all (ADR-0046 Q1). They never represented a creator's request,
   because no creator could make one — there was no ingress. They are residue of a runtime being
   built, and the platform owes them no product semantics.
2. **Their invisibility is an intentional preservation of ownership correctness, not data loss.** The
   rows remain in the database, fully intact and queryable by an operator. What the migration declines
   to do is *assert* an owner that does not exist. Excluding a row from an owner-scoped read is the
   only truthful answer when the owner is unknown; surfacing it under some caller's identity would be
   a fabrication with security consequences.
3. **No inference, heuristic attribution, or backfill may ever attempt to "guess" ownership.** Not by
   nearest-timestamp user, not by sole-tenant assumption, not by the project a generation was later
   promoted into, not by any correlation with `media_assets`, `prompts`, or the outbox. A guessed
   owner is indistinguishable at the database level from a real one, so a single heuristic backfill
   would permanently destroy the ability to tell asserted ownership from inferred ownership. This
   prohibition is absolute and outranks any convenience argument.
4. **Future administrative tooling may explicitly migrate or inspect legacy rows, and that is outside
   the production API contract.** An operator with out-of-band knowledge may deliberately assign
   ownership to specific rows, or purge them. Such tooling is an administrative action with a human
   accountable for the assertion — categorically different from the system inventing an owner. It must
   never be reachable through `/api/v1`.
5. **Owner-scoped reads remain the sole supported read model.** There is no "legacy view", no
   `include_unowned` flag, no admin bypass parameter, and no unscoped list endpoint in the production
   API. Any capability that would require reading unowned generations is, by definition, administrative
   tooling under point 4.

Inventing an owner for an unowned row would be worse than excluding it: it converts a known gap into
an invisible falsehood.

---

## D2 — Execution model

### Options

| | Option | Shape |
|---|---|---|
| **A** | Synchronous request execution | `POST` blocks until the video exists, returns the result |
| **B** | **Queued background execution** *(recommended)* | `POST` inserts `status='queued'` and returns immediately; a poll-ingress worker claims and runs it |
| **C** | Hybrid | Run inline up to a deadline, then fall back to queued |

### Evaluation

| Criterion | A — synchronous | B — queued worker | C — hybrid |
|---|---|---|---|
| **Consistency with Export / Publish / Render** | **None** — every existing long job is create→queue→worker→poll | **Exact** — reuses the proven shape (`RenderWorker.run_once()`, `get_publish_worker()`) | Partial, and introduces a shape the platform has nowhere else |
| **Lease model** | No lease; concurrency bounded by HTTP connections | `distributed_locks` lease `generation:<id>`, as publish uses (`process_publish_job.py:101`) | Two concurrency models to reason about |
| **Cancellation** | **Impossible** — no handle until it finishes | `status='cancelled'` (**already in the enum**, F1); worker checks cooperatively between shots | Only in the queued half |
| **Retries** | Client must re-POST, **re-spending money** with no dedup | Bounded, server-controlled attempts with the spend policy below | Ambiguous which half owns the retry |
| **Timeout behaviour** | **Fails at the gateway** long before the work does (F4: tens of minutes) | Bounded by lease TTL, independent of any HTTP timeout | Deadline choice is arbitrary and client-invisible |
| **Operational cost** | Ties a request worker up for tens of minutes | One more `run_once` cadence, as Export/Publish already require | Highest — both paths must be built and tested |
| **API responsiveness** | Unusable | `202`/`201` immediately | Non-deterministic: identical requests return different shapes |
| **Replay semantics** | None | Durable queued row is the replay anchor (see D4) | Split-brain between the two paths |

### Lease and spend policy (the part unique to generation)

Generation differs from publish in one decisive way: **a retry costs real money.** Two rulings follow.

1. **The lease must outlive the worst-case run or be renewed.** The publish lease is 900 s; F4's worst
   case exceeds that. The lease TTL must either exceed the worst case or be **heartbeat-renewed** by
   the worker. A lease that expires mid-run would let a second worker start the same generation and
   **double the spend** — the cost analogue of PUB-11's duplicate post. The exact mechanism is a
   pre-flight detail; that the window must be closed is fixed here.
2. **Job-level retries are bounded and conservative — recommended `max_attempts = 1` for v1.**
   Intra-run recovery already exists at the shot level (`DEFAULT_MAX_ATTEMPTS = 3` per shot). A
   *job-level* retry re-runs shots that may already have been paid for. A crashed run should surface
   as `failed` for the creator to re-request explicitly, rather than silently re-spending.

### Crash / failure matrix

| Failure window | A — synchronous | B — queued worker (recommended) |
|---|---|---|
| Crash **before** any provider call | Client sees a dropped connection; no record exists | Row stays `queued`; lease expires; **re-claimed and run once** ✅ |
| Crash **mid-run**, after provider spend | **Work and money lost, no record** | Row is mid-state with a stale lease; with `max_attempts=1` it settles **`failed`** — spend is recorded and visible, never silently repeated |
| Crash **after** render, before persist | Video exists in storage, **orphaned and unreachable** | Same orphan risk, but the row records the last reached state, so it is **diagnosable** |
| Gateway timeout | **Guaranteed** for any realistic run (F4) | Not applicable — the request returned in milliseconds |
| Two workers race one row | Possible (two clients, two requests) | **Lease-guarded**; renewal closes the long-run window |
| Client retries an ambiguous create | **Second full generation, double spend** | Deduplicated by D4's idempotency key ✅ |

### Recommendation — D2-B

**Queued background execution**, reusing the established create→queue→worker→poll pattern with a
`generation:<id>` lease, cooperative cancellation via the existing `cancelled` status, and
conservative job-level retries. F1 shows the schema was designed for exactly this; only the worker is
missing.

---

## D3 — Read contract

### Options

| Criterion | A — polling *(recommended)* | B — long polling | C — SSE | D — WebSocket |
|---|---|---|---|---|
| **New infrastructure** | **None** | None, but ties up a worker per waiting client | Streaming response lifecycle | **Connection manager, auth handshake, fan-out** |
| **Consistency with platform** | **Exact** — render/export/publish all poll | Nowhere in the platform | Nowhere | Nowhere (grounding §2.17: realtime is absent) |
| **Determinism in CI** | **Fully deterministic** — a `GET` returns a value | Timing-dependent | Timing-dependent, needs streaming test harness | Timing-dependent, needs a WS client |
| **Client complexity** | Trivial | Moderate | Moderate | High |
| **Latency of updates** | Bounded by poll interval (seconds) | Near-real-time | Near-real-time | Near-real-time |
| **Cost under load** | Cheap, cacheable, stateless | Holds connections open | Holds connections open | Holds connections open |
| **Failure mode** | A missed poll is harmless | Reconnect logic | Reconnect + replay logic | Reconnect + replay + backpressure |

For a job that runs for **minutes** (F4), sub-second update latency has no product value. Polling's
only weakness is precisely the one that does not matter here.

### What the contract exposes

Applying **ADR-0051's read-model lesson** (internal bookkeeping must never reach a public contract —
the `_email` namespace precedent), the v1 contract is a **curated projection**, not a dump of runtime
internals. `provenance` JSONB, the resolution ledger, adapter/provider identities, and raw per-attempt
verifier output are **internal** and stay out of the public shape unless a later slice deliberately
promotes them.

The smallest deterministic v1 contract:

| Endpoint | Returns |
|---|---|
| `GET /api/v1/generations/{id}` | Owner-scoped: `status`, coarse progress (accepted shots / total shots), `created_at`/`started_at`/`finished_at`, terminal `failure_reason`, and — when `completed` — the reference needed to promote (`generation_id`, final video asset) |
| `GET /api/v1/generations` | Owner-scoped keyset list (the platform's existing pagination idiom), newest first, optional `status` filter |

Per-shot detail, live logs, and streaming are **explicitly deferred**. Nothing in D3-A precludes
adding SSE later as a **pure addition** over the same projection.

### Recommendation — D3-A

**HTTP polling only for v1**, over the curated projection above.

---

## D4 — Idempotency

### The question

Does the platform require a **client-supplied idempotency key**, or should the server **derive**
replay deterministically from request content?

### Why server-derived content hashing is wrong here

Hashing `(prompt, params, owner)` to dedupe would make the second identical request return the first
generation. **This breaks a legitimate creator action**: asking for another take of the same prompt is
*the* iteration loop of a generative product — non-determinism is the feature. Content hashing would
silently deny it. Rejected on product-correctness grounds, not cost.

Note the contrast with publishing: PUB-7 could rely on the **natural key**
`(source_media_asset_id, social_account_id)` because publishing the same artefact to the same account
twice is genuinely meaningless. **Generation has no such natural key** — which is exactly why the
intent must be carried explicitly.

### Options

| Criterion | A — client key *(recommended)* | B — server-derived hash | C — none |
|---|---|---|---|
| **Precedent in-platform** | **Exists**: `RenderJobCreateRequest.idempotency_key` with the DB backstop `uq_render_jobs_project_id_idempotency_key` and a 201/200 split (`create_render_job.py:22-25`) | None | None |
| **Allows deliberate repeat generation** | **Yes** (new key = new intent) | **No** — silently returns the old run | Yes |
| **Protects ambiguous retries** | **Yes** | Yes | **No** — double spend |
| **Consistency with ADR-0048** | **Yes** — DB constraint owns the race, not application dedup | Constraint on a hash | n/a |
| **Client burden** | One header/field, already idiomatic here | None | None |
| **Failure cost** | Low | **Denies a core product action** | Real money per duplicate |

### Crash / ambiguity matrix (D4-A)

| Scenario | Outcome |
|---|---|
| Client never receives the `201` and retries with the **same key** | Same generation returned (`200`), **no second run, no second spend** ✅ |
| Client retries with a **new key** | A genuinely new generation — correct: the creator asked twice |
| Two concurrent creates, same key | **Unique constraint** decides one winner; the loser reads back the winner (ADR-0048 posture) |
| Crash after row insert, before worker claim | Row is `queued` and durable; worker claims it later ✅ |
| Crash mid-run | Per D2: settles terminal `failed`; a re-request needs a **new key**, making the re-spend explicit and creator-authorised |
| No key supplied | Create still succeeds (key optional, as with render jobs) — the creator accepts duplicate risk |

### Recommendation — D4-A

**Client-supplied idempotency key, optional, with a DB unique constraint as the race-safe backstop and
a 201/200 create split** — the exact α7.1 render-job shape. Server-derived content hashing is
rejected.

---

## D5 — API boundary

### Options

| Criterion | A — `/generation-jobs` | B — extend `/workflow-runs` | C — `/generations` *(recommended)* |
|---|---|---|---|
| **Matches domain vocabulary** | Introduces "generation job" — a term **no table, entity, or DTO uses** | Conflates two unrelated runtimes | **Exact**: the table is `generations`, the DTO is `GenerateVideoResult.generation_id`, and `POST /media/promotions` already speaks `generation_id` over HTTP |
| **Matches platform vocabulary** | `-jobs` is used where a *job row wraps* other work (`render-jobs`, `publish-jobs`) | `workflow-runs` is the α7.6 orchestration runtime | The generation row **is** the job (F1) — no wrapper to name |
| **Risk of confusion** | Two names for one row | **High** — `generate-video@1.0.0` already exists there as a *different* pipeline (`registry.py:317`, pausing on the provider seam) | Low |
| **Coupling** | Low | **Couples the frozen α7.6 runner to the α8.6 runtime** | Low |

Option B deserves explicit rejection because it looks superficially attractive: `/workflow-runs`
already accepts `generate-video`. But that step dispatches to `VideoProvider.submit()`
(`dispatcher.py:113`) and pauses on the async-provider seam — it is the "Path A" pipeline, entirely
distinct from the α8.6 execution runtime. Overloading it would fuse two runtimes the architecture
deliberately keeps apart and would edit orchestration frozen under ADR-0042.

### Shape

**Flat and owner-scoped**, like `/publish-jobs`, `/media`, and `/library` — *not* project-nested like
`/render-jobs`, because under D1-A a generation is owned by a **user**, and its association with a
project is optional. A `project_id` filter/field can express the association without making it a path
segment.

### Recommendation — D5-C

A dedicated **`/api/v1/generations`** resource: `POST` to request, `GET /{id}` to read, `GET` to list,
and `POST /{id}/cancel` mirroring the existing cancel idiom on render/workflow runs.

---

## Migration summary (explicit, per D1 option)

| Ownership option | Migration required? | Contents |
|---|---|---|
| **A — owner columns** *(recommended)* | **YES** | Add `tenant_id`, `owner_user_id` (nullable), optional `project_id`; composite index for the owner-scoped keyset list |
| B — ownerless | No | — (but the slice cannot be built) |
| C — project only | **YES** | Add `project_id` + index |
| D — wrapper table | **YES** (largest) | New `generation_jobs` table, FKs, status machine, indexes |

**The recommended option requires a migration.** That is stated plainly rather than engineered
around. It would be the first migration since α9.0 (`0015`), and it is the correct outcome: the
alternative — leaving generation state unowned — cannot express the product requirement and leaves
the F3 authorization gap open.

D2/D3/D4/D5 add no further migration beyond D4's unique constraint on the idempotency key, which
rides along in the same migration.

---

## Compatibility with existing ADRs

| ADR | Compatibility |
|---|---|
| **ADR-0046** (Execution Runtime X1–X8, Q1/Q2) | **Extends, does not reverse.** Q1 ruled on asset persistence; its "no owner context *yet*" was context, and α8.8 AP9 scheduled the binding for this slice. Q2's raw-SQL, ORM-free persistence style is preserved — the new columns and repository reads stay raw SQL with the validator allowlist. The X-boundaries are untouched: ingress sits *upstream* of the runtime, exactly as the planner does. |
| **ADR-0048** (DB-owned idempotency) | **Directly applied.** D4 puts a **unique constraint** behind the idempotency key rather than application-level dedup — the same posture as `uq_analytics_events_source_event_id`. |
| **ADR-0049** (AI boundary, one-way dependency) | **Unaffected.** Ingress adds no publishing→AI or AI→publishing edge. The AI plane remains ignorant of the API layer; the router depends on an application use case, never on an adapter. |
| **ADR-0050** (thumbnail source/delivery) | **Unaffected.** Generation produces a video; thumbnails remain creator-supplied at publish time. No lineage traversal is introduced, consistent with ADR-0050's explicit exclusion. |
| **ADR-0051** (read-model hygiene, at-least-once effects) | **Directly applied twice.** D3 exposes a **curated projection**, keeping `provenance` and resolver internals out of the public contract (the `_email` lesson). D2's at-least-once claim model mirrors D1-C's lease posture, with the cost asymmetry inverted: for email, delivery outranked duplicate-avoidance; **for generation, duplicate-avoidance outranks eager retry**, because a duplicate costs money rather than mild annoyance. |
| **ADR-0042 / ADR-0045** | **Additive.** No edit to the frozen runner, relay, or outbox; no policy migrates into the API; the Decision plane stays pure. |

---

## Rejected alternatives

1. **Leave generations ownerless (D1-B).** Cannot express "list my generations", and leaves F3
   exploitable the moment ids become client-visible. Rejected on security and product grounds.
2. **Project-only ownership (D1-C).** Buys a smaller migration at the cost of a join on every read and
   surprising semantics if a project is ever transferred. Rejected: optimising for a smaller migration
   is the wrong objective.
3. **An owned `generation_jobs` wrapper (D1-D).** Would duplicate the `generation_status` machine that
   already exists (F1). Rejected as redundant state with dual-write risk.
4. **Synchronous execution (D2-A).** Guarantees gateway timeouts for realistic runs, offers no
   cancellation and no dedup, and contradicts three existing job runtimes. Rejected.
5. **Hybrid execution (D2-C).** Makes the API's response shape depend on unpredictable timing.
   Rejected.
6. **SSE/WebSocket for v1 (D3-C/D).** Real infrastructure cost and non-deterministic tests to shave
   seconds off a multi-minute job. Rejected for v1; addable later as a pure extension.
7. **Server-derived content-hash idempotency (D4-B).** Would silently refuse a creator's legitimate
   second take. Rejected — it breaks the product's core iteration loop.
8. **Extending `/workflow-runs` (D5-B).** Would fuse the α7.6 orchestration runtime with the α8.6
   execution runtime and touch frozen orchestration. Rejected.

---

## Consequences

**Positive.**
- The product's core promise becomes reachable: a creator can request a video and watch it complete.
- The **F3 authorization gap closes** before generation ids ever become client-visible.
- "List my generations" becomes expressible, unblocking dashboard and library integration later.
- Generation joins the platform's single async-job idiom, so operators reason about one pattern.
- The idempotency key makes provider spend **explicitly creator-authorised**.

**Negative / accepted.**
- **A migration is required** — the first since `0015`.
- Legacy pre-α9.7 generation rows remain unowned and therefore **invisible** to the new reads. This is
  deliberate data honesty (see D1 migration shape), not data loss.
- A **new worker cadence** joins render/export/publish/notification-email — more operational surface.
- With `max_attempts = 1`, a crashed mid-run generation settles `failed` and the creator must
  re-request. Accepted: an automatic retry would re-spend money without consent.
- Long leases (or heartbeat renewal) are a genuinely new requirement; no existing worker runs this
  long, so the mechanism needs real test coverage.

---

## Load-bearing invariants

Fixed here; a pre-flight may not weaken them.

1. **Every generation read is owner-scoped.** No endpoint returns generation state without a
   `tenant_id` + `owner_user_id` predicate derived from `CurrentUserDep`.
2. **Promotion must authorize the generation, not only the project.** The F3 gap is closed as part of
   this slice: promotion requires **both** generation ownership **and** project membership.
3. **No HTTP request blocks on generation execution.**
4. **A generation is never automatically re-run in a way that re-spends provider budget** without an
   explicit new creator request (or an explicitly bounded, documented retry policy).
5. **The lease outlives the worst-case run, or is renewed.** A second worker must never be able to
   start a generation that is still running.
6. **The public read contract is a curated projection.** Runtime internals (`provenance`, resolution
   ledger, adapter identities, raw verifier output) stay internal (ADR-0051).
7. **Identical prompts remain independently generatable.** Idempotency is keyed on explicit client
   intent, never on request content.
8. **Ownership is never inferred.** No heuristic, correlation, or backfill may assign an owner to a
   legacy generation; owner-scoped reads are the sole supported read model (D1 migration philosophy).

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-29 | **Proposed.** Drafted at the α9.7 grounding stop to resolve the ownership deferral recorded by ADR-0046 Q1 and α8.8 AP9. Recommends **D1-A** (owner-scoped generations, **migration required**), **D2-B** (queued worker), **D3-A** (polling), **D4-A** (client idempotency key), **D5-C** (`/generations`). No pre-flight, no implementation. |
| 2026-07-29 | **Amended, then Accepted.** D1's legacy-row treatment gained an explicit **migration philosophy**: legacy ownerless generations are implementation artefacts, not domain objects; their invisibility preserves ownership correctness and is not data loss; **ownership inference is absolutely prohibited**; administrative tooling may explicitly migrate or inspect them but never through `/api/v1`; owner-scoped reads are the sole supported read model. Added as **load-bearing invariant 8**. Mechanics unchanged; no new architectural question. D1–D5 accepted as drafted. |
