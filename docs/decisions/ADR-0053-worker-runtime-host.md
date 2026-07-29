# ADR-0053 — Worker Runtime Host: Process Topology, Scheduling & Shutdown Semantics

**Status:** **Accepted** (Phase 3, α9.8 — Worker Runtime, 2026-07-29). Governance that **precedes**
implementation (like ADR-0044/0045/0046/0047/0048/0049/0050/0051/0052): it fixes *how the platform
executes background work* before any host process exists. Drafted at the post-α9.7 grounding stop,
**amended twice** before acceptance (see Changelog). The α9.8 pre-flight follows; **no implementation
accompanies this ADR.**

**Amendments (2026-07-29, pre-acceptance).** Neither changed a recommendation. **(1)** D4 was restated
as a *requirement* rather than a repository observation — replica safety is a per-worker obligation
proven by that worker's own **safe claim mechanism**, never something the host supplies — and a worker
contract was added as load-bearing invariant 2, fixing the worker/host division of responsibility.
**(2)** Two properties that are easy to conflate were given explicit definitions: **replica-safe
processing** versus **exactly-once external effects**, and scan behaviour was classified as *correct
but potentially inefficient*, so that future optimisation is never mistaken for correctness work.

**Scope: platform-wide, not generation-only.** α9.7 exposed the problem, but the problem is not
generation's. Seven independent poll primitives are affected, and solving only the newest one would
force the same five questions to be re-answered the next time a worker is added. This ADR settles the
runtime model for all of them at once. It may still distinguish *workload classes*, and D1 is
deliberately shaped so that splitting them later is a deployment change rather than a redesign.

**Builds on:**
- **ADR-0042** (orchestration/platform freeze — the host *invokes* existing primitives; it edits no
  frozen path, and `RelayService` in particular is called, never modified).
- **ADR-0046** (Execution Runtime boundaries — the host is an execution-plane concern; no policy,
  planning, or resolution logic may migrate into it).
- **ADR-0051** (the lease + at-least-once posture that six of the seven workers already implement).
- **ADR-0052** (**GEN-2**: one generation execution is one external spend opportunity, with no
  automatic retry — the invariant that makes D3 genuinely hard, and the reason this is an ADR).

---

## Context

Every asynchronous capability the platform has shipped since α7.3 is **dormant in a running
deployment.** Not degraded, not slow — inert.

`POST /api/v1/generations` returns `201`, and the generation sits `queued` forever. A `PublishJob`
is never uploaded. An `ExportJob` never encodes. Enrichment never derives a thumbnail. Email is
never sent. And because the **outbox relay** is among the unrun primitives, in-app notifications
(α8.5b.3, α8.9a) and analytics (α9.0) are never projected either — the events accumulate in
`event_outbox`, unread.

This is not a regression. The worker primitives were built correctly and tested thoroughly; what was
never built is the thing that *calls* them. α9.7 made the gap acute rather than academic by shipping
a creator-facing front door to a queue that nothing drains.

### The decisive facts

Six facts, all verified during grounding, constrain every option below. Two of them remove
constraints I expected to find, and one of them is the reason this document exists.

**F1 — There is no execution host of any kind, and never has been.** Outside tests, every reference
to `run_once` / `relay_once` in the entire backend is a docstring or a config-field description.
There is no daemon, scheduler, Celery, task queue, FastAPI lifespan task, Dockerfile,
`docker-compose` service, Procfile, systemd unit, or Makefile target. `scripts/run_dev.py` starts
uvicorn and nothing else. The lifespan hook (`main.py:74-91`) pings the database and disposes the
engine — it starts no work. The worker docstrings say so plainly: *"No trigger, endpoint, or cron is
added … the worker is invoked externally"* (`publish_worker.py:10-11`).

**F2 — Seven primitives are affected, and one of them is load-bearing for two others.**
`RelayService`, `RenderWorker`, `ExportWorker`, `MediaEnrichmentWorker`, `PublishWorker`,
`NotificationEmailWorker`, `GenerationWorker`. The relay is not merely one of seven: it is the
delivery mechanism for the notification and analytics projections, so its absence silently disables
two shipped product surfaces that have nothing to do with background jobs.

**F3 — All seven are already `await`-friendly. Co-hosting is technically viable.** This is the fact
that removes the constraint I expected to be decisive. ffmpeg is invoked with
`asyncio.create_subprocess_exec`, never `subprocess.run` (`ffmpeg_renderer.py:224-234`,
`ffmpeg_exporter.py:178-188`, `ffmpeg_slideshow_renderer.py:152-157`, `_ffmpeg_exec.py:20-28`); all
provider and destination HTTP uses `httpx.AsyncClient`; Pillow feature extraction is offloaded via
`asyncio.to_thread` (`pillow_feature_extractor.py:145-146`); file I/O uses `asyncio.to_thread`
(`process_render_job.py:231`). **No worker blocks the event loop for the duration of its heavy
work.** A single asyncio process can therefore host all seven without one starving another —
*provided* D2 does not serialise them.

**F4 — Concurrency safety already exists, per worker, and it is not uniform.** Six workers take a
distributed lease before touching an item (`render_job:{id}`, `export_job:{id}`,
`media_enrichment:{id}`, `publish_job:{id}` + `project_publish:{project_id}`,
`notification_email:{id}`, `generation:{id}`), most paired with a status CAS. The relay uses a
different mechanism entirely — `FOR UPDATE SKIP LOCKED` on the outbox fetch
(`event_outbox_repository.py:71-78`), so concurrent relays claim disjoint row sets. **Replication is
therefore safe by construction**, but for two different reasons that must both be understood before
the host declares itself replicable.

**F5 — Failure isolation is inconsistent, and four of seven workers will abort a batch.** Relay
(`relay_service.py:87-116`), notification email (`notification_email_worker.py:63-73`), and
generation (`generation_worker.py:147-156`) wrap each item so one failure cannot stop the pass.
Render, export, enrichment, and publish **do not**: `RenderWorker.run_once` is a bare loop
(`render_worker.py:55-58`), so an exception the inner use case does not classify propagates out and
silently discards the remaining jobs in the batch. Whatever calls `run_once()` must not assume it
returns normally.

**F6 — Under GEN-2, abandoning in-flight work destroys paid output.** `GenerationWorker` holds its
lease for minutes, renewed by a heartbeat that runs in the *same* coroutine as the pipeline
(`generation_worker.py:161-183`), not a separate task. If the process dies, the `finally: release`
may not run; the lease expires after `generation_lease_seconds` (300s); the reaper then terminalises
the row to `failed` after `generation_reap_grace_seconds` (120s) — and per **ADR-0052 GEN-2 it is
never requeued**. That behaviour is correct: an automatic retry would re-spend a creator's money
without consent. But it means **a routine deploy destroys every generation in flight**, and the
creator must pay again. GEN-2 is not wrong; it simply makes the host's shutdown contract a decision
about money rather than about tidiness.

### Why this is an ADR and not a pre-flight

A pre-flight implements decisions already made. Here, the decisions are not made, and two of them
have no objectively correct answer:

- **Shutdown (D3)** trades deployment velocity against destroyed creator spend, and *both* directions
  are defensible. Immediate termination keeps deploys fast and predictable but wastes real money on
  every release. Draining to completion protects the creator but makes deploy duration a function of
  the longest-running generation.
- **Topology (D1)** binds how the platform is deployed, scaled, and paid for, and is expensive to
  reverse once operational runbooks and container images assume it.

Both also interact with an invariant frozen *one release ago*. That is precisely the case the ADR
process exists for.

### Decision points

| # | Question |
|---|---|
| **D1** | Where do workers run — in the API process, or a dedicated one? |
| **D2** | What drives cadence, and how are the seven scheduled relative to each other? |
| **D3** | What happens to in-flight work on shutdown? |
| **D4** | May the host be replicated, and which workers are safe when it is? |
| **D5** | How does the host survive a failing worker, and how do we know it is alive? |

---

## D1 — Process topology

### Options

| | Option | Shape |
|---|---|---|
| **A** | In the API process | Start workers as background tasks in the FastAPI lifespan |
| **B** | One dedicated worker process class *(recommended)* | A second entrypoint running all seven, with a config selector for which subset this instance runs |
| **C** | One process class per workload class | Split into e.g. latency-sensitive (relay, email), media-heavy (render, export, enrichment, generation), network-bound (publish) |
| **D** | One process per worker | Seven independently deployed process classes |

### Evaluation

| Criterion | A — in API | B — one worker class | C — per workload class | D — per worker |
|---|---|---|---|---|
| **API latency isolation** | **Poor** — heavy work shares the request event loop; F3 keeps it await-friendly, but ffmpeg subprocess memory and CPU still land on the API host | **Full** | **Full** | **Full** |
| **Independent scaling** | **None** — worker capacity is a function of API replica count | Coarse — one knob, but the selector makes finer splits a deploy-time change | **Good** | **Best** |
| **Deploy blast radius (F6)** | **Worst** — every API deploy, autoscale event, and rolling restart kills in-flight generations | Moderate — worker deploys can be scheduled separately from API deploys | Good — media-heavy class can deploy on its own cadence | **Best** |
| **Polling load on the DB** | **Worst** — every API replica polls; scan load scales with request traffic, which is unrelated to queue depth | Controlled | Controlled | Controlled |
| **Operational surface** | **Smallest** — one thing to run | Small — two | Moderate — three or four | **Largest** — seven images, seven runbooks |
| **Resource contention** | Unacceptable — an ffmpeg batch competes with request serving for host memory | Present but contained; F3 means it is resource contention, not event-loop starvation | **Best isolation** | Best isolation |
| **Reversibility** | Hard — unwinding means introducing a new process class later anyway | **Easy** — a selector turns B into C without code change | Moderate | Moderate |
| **Local dev / CI** | Simplest | Simple — one extra command | More setup | Most setup |

### Recommendation — **B, with a worker selector**

A dedicated worker process class, whose entrypoint takes a configurable **set** of workers to run.
Deployed with the selector unset, one process runs all seven — the simplest possible operational
story for a platform with no production traffic. Deployed twice with disjoint selectors, it *is*
option C, with no code change.

This matters because **C is currently unjustifiable on evidence.** Splitting by workload class is a
performance optimisation, and there are no measurements to optimise against — no production
deployment exists. But the *reason* to split is easy to foresee (F3 keeps the event loop healthy, yet
four concurrent ffmpeg processes are still four ffmpeg processes on one host), so the design should
make the split free. A selector costs one setting and buys the entire option.

Option A is rejected on F6 alone: coupling generation survival to API deploy cadence guarantees that
routine releases destroy paid creator work, and the platform would inherit that property permanently.
The DB polling argument is nearly as strong — API replicas scale with request traffic, which has no
relationship to queue depth, so A scales polling load against exactly the wrong signal.

Option D is premature at seven small workers that share one container image and one dependency set.

---

## D2 — Scheduling model

### Options

| | Option | Shape |
|---|---|---|
| **A** | Sequential tick loop | One loop calls all seven in turn, sleeps, repeats |
| **B** | Supervised task per worker *(recommended)* | One asyncio task per worker, each with its own interval, all supervised by the host |
| **C** | External scheduler | cron / Kubernetes `CronJob` invokes a one-shot CLI command per worker |
| **D** | Event-driven | `LISTEN`/`NOTIFY` or a broker replaces polling |

### Evaluation

| Criterion | A — sequential tick | B — task per worker | C — external cron | D — event-driven |
|---|---|---|---|---|
| **Cross-worker interference** | **Unacceptable** — a render batch (10 jobs × minutes) delays the next relay pass by that entire time, so notification and analytics latency becomes a function of render queue depth | **None** — independent tasks | None | None |
| **Per-worker cadence tuning** | One global interval for workloads spanning milliseconds to tens of minutes | **Natural** | Natural | n/a |
| **Fit for long work** | Poor | **Good** | **Poor** — cron overlap semantics and a minutes-long generation are a bad match; `CronJob` concurrency policy becomes load-bearing | Good |
| **Operational surface** | Lowest | Low — internal to the host | High — seven schedules to manage outside the app | Highest — broker or persistent `LISTEN` connections |
| **Latency** | Poor | Good (tunable) | Poor — bounded below by cron granularity | **Best** |
| **Still needs polling anyway** | — | — | — | **Yes** — `scheduled_at` backoff (publish), `next_attempt_at` (email), and generation reaping are all time-triggered, not event-triggered |

### Recommendation — **B, with idle backoff**

One supervised asyncio task per worker, each with its own configurable interval. This is what makes
D1-B safe: F3 established that the workers do not block the event loop, and B ensures they do not
block *each other* through the scheduler either. A is the one arrangement that would squander F3 by
reintroducing serialisation at the host level.

Add **adaptive idle backoff**: when a pass returns work, poll again promptly; when it returns
nothing, back off toward a ceiling. Seven workers each polling a fixed short interval against an
empty queue is pure background load on a database that is also serving the API, and queue depth is
bursty by nature.

D is the right long-term answer for *latency* and should be recorded as a future refinement, but it
is not a replacement: three of the seven workers are driven by time (retry backoff, reaping), not by
events, so polling remains structural. Adopting it now would add a broker or persistent listener
connection to shave seconds off multi-minute jobs.

---

## D3 — Shutdown semantics

**This is the decision the ADR exists for.** It is the only one that can turn a correctly-frozen
invariant into user-hostile behaviour.

### Options

| | Option | Shape |
|---|---|---|
| **A** | Immediate | On `SIGTERM`, cancel everything and exit |
| **B** | Unbounded drain | Stop claiming; wait for all in-flight work to finish, however long |
| **C** | Bounded drain *(recommended)* | Stop claiming immediately; finish in-flight work within a deadline; past it, abandon deliberately and log precisely what was lost |
| **D** | Cooperative checkpoint | Pipeline checkpoints between shots; shutdown resumes elsewhere |

### Evaluation

| Criterion | A — immediate | B — unbounded | C — bounded drain | D — checkpoint |
|---|---|---|---|---|
| **Creator spend preserved (F6)** | **No** — every deploy destroys in-flight generations; per GEN-2 they are never retried | **Yes** | **Mostly** — short work always survives; long work survives if it fits the deadline | **Yes** |
| **Deploy predictability** | **Best** — bounded by the orchestrator grace period | **Worst** — deploy duration is a function of the longest generation; tens of minutes | **Good** — bounded by an explicit, configured deadline | Good |
| **Behaves as designed under a real orchestrator** | Yes | **No** — the orchestrator `SIGKILL`s at its grace period regardless, so B silently degenerates into A at an unpredictable point | **Yes**, if the deadline is set at or below the grace period | Yes |
| **Loss is observable** | Loss is silent (reaper marks `failed`, cause unrecorded) | n/a | **Yes** — abandonment is logged with the generation id and elapsed time | n/a |
| **Complexity** | Trivial | Trivial | Low — a stop flag plus a bounded wait | **High** — requires pipeline-level resumability and revisiting GEN-2 |
| **Correct for cheap workers** | Wasteful but harmless | Harmless | Harmless | Over-engineered |

### Recommendation — **C, with per-worker drain budgets**

On `SIGTERM`, in two phases:

1. **Stop claiming, everywhere, immediately.** Every worker task stops starting new items. This phase
   is instant and is the majority of the benefit: it guarantees the host never *begins* work it
   cannot finish.
2. **Drain in-flight work within a bounded, per-worker deadline**, then cancel and exit. When the
   deadline is hit, log the abandoned item ids and elapsed time so the resulting `failed` rows are
   explainable rather than mysterious.

The deadline must be **per worker, not global**: draining the relay or the email worker takes
sub-second to ~30s, while a generation can take tens of minutes. A single global budget would either
be uselessly short for generation or absurdly long for everything else.

**B must be rejected specifically because it does not survive contact with an orchestrator.** Under
Kubernetes, ECS, systemd, or a plain supervisor, `SIGTERM` is followed by `SIGKILL` after a grace
period. An unbounded drain therefore does not *actually* drain — it becomes option A at a moment
nobody chose, which is strictly worse than choosing A honestly. C is B made operable: the same intent,
with the truncation point moved from the orchestrator's default into an explicit setting that ops sets
to match the configured grace period.

### The residual loss must be stated plainly

C does not eliminate the problem; it bounds and reveals it. At any finite deadline, a generation
longer than the deadline that is unlucky enough to be running during a deploy is lost, and per GEN-2
the creator pays again. Three honest mitigations exist, none of which this ADR adopts:

- Size the drain deadline to the p95 generation duration and the orchestrator grace period to match.
- Deploy the media-heavy worker class on a deliberate cadence (D1-B's selector makes this possible).
- Make generation **resumable** at shot granularity (option D). This is the only real fix, and it is
  explicitly out of scope: it requires pipeline checkpointing and a revision of GEN-2, since "resume"
  and "one execution is one spend opportunity" must be reconciled — partial spend on an interrupted
  run is a question ADR-0052 did not have to answer.

Recording this as a known, bounded, measurable cost is the point. It is currently an *unbounded*
cost that nobody has noticed because nothing runs.

---

## D4 — Replication and concurrency safety

### Terminology

Two properties are easy to conflate and are **not** the same thing. This ADR uses these definitions,
and later ADRs should inherit them.

**Replica-safe processing** means multiple runtime hosts may execute concurrently without corrupting
state and without losing work. It is guaranteed by a **safe claim mechanism** — a distributed lease, a
status CAS, a `FOR UPDATE SKIP LOCKED` fetch, a uniqueness constraint, or an equivalent — that decides
unambiguously which instance owns an item.

**Replica-safe processing does not imply exactly-once external effects.** Where an accepted ADR
deliberately permits at-least-once delivery — ADR-0051 email being the standing example — replication
**preserves** that semantic rather than strengthening it. A worker may therefore be fully replica-safe
and still, by design, produce a duplicate external effect within its documented window. Replication
does not introduce that behaviour, but it does exercise it more often.

The practical consequence: "is this worker replica-safe?" and "does this worker have exactly-once
effects?" are separate questions with separate answers, and only the first is a precondition for
running more than one host.

Note also that the requirement is expressed in terms of **safe claim mechanisms**, not leases. A lease
is one implementation. The relay's `SKIP LOCKED` fetch is another, and is no weaker for not being a
lease.

### Options

| | Option | Shape |
|---|---|---|
| **A** | Singleton | Exactly one host instance; enforced by a global lease |
| **B** | Replicable *(recommended)* | Any number of instances; each worker proves its own claim safety |
| **C** | Mixed | Replicable except designated singleton workers |

### Recommendation — **B, stated as a requirement rather than a claim**

**The host is designed for replicated deployment, and replica safety is a per-worker obligation, not
a host guarantee.** The host provides no coordination whatsoever and must never become the thing that
makes concurrency safe.

Concretely, the contract is:

> Every worker must **individually** satisfy replica safety through its own mechanism — a distributed
> lease, an atomic claim, a status CAS, a uniqueness constraint, or an equivalent. A worker that
> cannot demonstrate this **must not be run in more than one instance**, and the implementing slice
> must either give it such a mechanism or explicitly exclude it from replicated deployment via the
> D1 selector.

This phrasing matters. Saying "all workers are already safe" would make a point-in-time audit sound
like a structural property, and the next worker added would inherit an assumption nobody re-checked.
The requirement is what is being fixed here; the audit below is merely evidence that the requirement
is currently met.

### Audit as of this ADR

| Worker | Mechanism | Assessment |
|---|---|---|
| Relay | `FOR UPDATE SKIP LOCKED` on the outbox fetch (`event_outbox_repository.py:71-78`) | **Satisfied** — concurrent relays claim disjoint row sets. Note this is *not* a lease; it is a different mechanism that meets the same requirement |
| Render / Export / Enrichment | Per-item lease + status CAS | **Satisfied** — the CAS loser does no work |
| Publish | Dual lease (`publish_job` + `project_publish`) + CAS | **Satisfied** — per-project serialisation also survives replication |
| Notification email | Per-item lease, send-then-stamp | **Satisfied for correctness**, with the caveat below |
| Generation | Per-item lease + `queued → planning` CAS | **Satisfied** — the CAS makes claiming exactly-once |

Read this table strictly against the terminology above: every row asserts **replica-safe processing**,
and none of them asserts exactly-once external effects. The email worker is the case where the two
visibly diverge — it is fully replica-safe *and* deliberately at-least-once, per ADR-0051's accepted
duplicate window. Replication preserves that semantic and exercises it more often; it does not create
it, and the implementing slice must not "fix" it.

A singleton host (A) would be a needless availability bottleneck, and — worse — it would let the
per-worker requirement rot unobserved, so the first person to scale would be relying on untested
behaviour.

### Scan behaviour: correct, but not efficient

Six of the seven workers separate **scanning** for candidates from **claiming** one. Only the relay
fuses the two, because `SKIP LOCKED` claims as it fetches. This distinction is recorded explicitly so
that future work on it is not mistaken for correctness work:

- **The unlocked scans are correct.** A scan is only a *suggestion*. Ownership is decided exclusively
  by the claim mechanism that follows it — the lease and the CAS — so two hosts scanning identical
  candidates cannot both proceed. Correctness does not depend on the scan being exclusive, and adding
  locking to it would not make anything safe that is currently unsafe.
- **The unlocked scans are potentially inefficient.** Under N hosts, all N inspect the same head of the
  queue and N−1 lose the claim, wasting a scan and a claim attempt each pass. The waste grows with N
  and with contention, not with correctness risk.

Therefore: adding `SKIP LOCKED` (or an equivalent claim-on-scan) to the six scan queries is a
**throughput optimisation, not a bug fix**. It should be scheduled on evidence of contention, and its
absence must never be cited as a reason the host cannot be replicated.

**One further operational caveat.** Replication multiplies database connections; the host's pool
sizing is a real operational parameter, not a default to inherit.

---

## D5 — Host resilience and liveness

Two coupled concerns: surviving a failing worker, and knowing the host is alive. Both follow from a
single division of responsibility, which this ADR states explicitly because F5 shows it is currently
only implied.

### The worker contract

> **A worker is responsible only for processing available work.** It is not responsible for
> scheduling itself, supervising itself, restarting itself after failure, bounding its own runtime, or
> coordinating shutdown. Those responsibilities belong **exclusively** to the Worker Runtime Host.

Worker owns business logic; host owns execution lifecycle. Nothing else.

This is what makes `run_once()` the right shape and F5 a host problem rather than a worker bug: a
worker that raises has not violated its contract, because handling that is not its job. It also gives
every future worker its specification in one sentence — implement a single pass, return or raise, and
assume nothing about who calls you or how often.

The corollary is that the host may not reach past `run_once()`. It schedules, wraps, times, and stops;
it does not inspect a worker's internals, reorder its items, or interpret its results beyond logging
them (this is invariant 1 restated from the other side).

**Configuration awareness** sits on the worker side of this line where it concerns *what work to do*,
and on the host side where it concerns *whether to run at all*. A worker reads its own batch sizes,
timeouts, and thresholds; the host owns intervals, drain budgets, and worker selection. Enable/disable
flags are the ambiguous case — the pre-flight must place each one deliberately and consistently rather
than by accident.

### Failure isolation

F5 is unambiguous: four of seven workers propagate exceptions out of `run_once()`. If the host
assumes a normal return, a single unclassified error in a render job kills that worker's task, and
the platform loses that capability **silently and permanently** until someone restarts the process.

The host contract must therefore be:

- **Every `run_once()` call is wrapped.** The host treats a worker as opaque and assumes nothing about
  its internal error handling.
- **A failing pass never kills its task.** Log, back off, continue. A worker that fails repeatedly
  backs off toward a ceiling rather than hot-looping against a broken dependency.
- **A task that dies anyway is restarted**, and a task that cannot be kept alive is escalated — a
  worker must never be quietly absent.

Additionally, and recommended for the implementing slice rather than mandated here: bring the four
non-isolating workers up to the per-item pattern the other three already use. Host wrapping prevents
the fatal outcome, but without per-item isolation one bad render job still discards the other nine in
its batch. The pattern is already established in three workers, so this is conformance, not design.

### Liveness

| | Option | Assessment |
|---|---|---|
| **A** | Logs only | Insufficient — a wedged process logs nothing, which is indistinguishable from an idle one |
| **B** | HTTP health endpoint on the worker | Effective, but gives the worker process an HTTP surface and a port purely for observation |
| **C** | DB heartbeat row per worker | Effective and queryable by an ops dashboard, but needs schema and a write on every pass |
| **D** | Liveness marker file touched each pass *(recommended)* | Zero schema, zero ports; works with a standard exec-style liveness probe |

**Recommendation — D now, C later.** A file touched on each successful pass, checked for staleness by
an exec probe, distinguishes "idle" from "wedged" at essentially no cost and no new surface area. A
DB heartbeat (C) is strictly more useful — it answers "is the relay running?" from a dashboard rather
than from inside the container — but it is an observability feature that deserves its own slice
alongside metrics, not a schema change smuggled into the runtime host.

---

## Compatibility with existing ADRs

| ADR | Compatibility |
|---|---|
| **ADR-0042** (platform freeze) | **Additive.** The host *calls* `RelayService.relay_once()`; it does not modify the relay, the runner, or the outbox. No frozen path is edited, and no `Freeze-Override:` trailer is required. |
| **ADR-0046** (Execution Runtime boundaries) | **Respected.** The host is pure scheduling and process lifecycle. No planning, resolution, verification, or policy logic may move into it — it may not even know what a generation *is* beyond calling `run_once()`. |
| **ADR-0051** (lease + at-least-once) | **Preserved.** D3's "stop claiming, then bounded drain" is the natural extension of the per-item lease model to process lifecycle: an abandoned lease expires and the item becomes reclaimable exactly as it does after a crash. |
| **ADR-0052** (**GEN-2**, no automatic retry) | **Upheld, and its cost made explicit.** D3 does not weaken GEN-2 — no abandoned generation is ever requeued. It bounds how often abandonment happens and makes each instance observable. The tension between GEN-2 and deploy cadence is recorded, not resolved; resolving it would require resumable generation and an amendment to ADR-0052. |
| **ADR-0048** (DB-owned idempotency) | **Consistent.** D4 relies on DB-level mechanisms (`SKIP LOCKED`, leases, CAS) for multi-instance correctness rather than host-level coordination. |

---

## Rejected alternatives

1. **Workers in the API process (D1-A).** Couples generation survival to API deploy cadence (F6),
   scales polling load against request traffic rather than queue depth, and puts ffmpeg memory on the
   API host. Rejected despite being the smallest operational change.
2. **One process per worker (D1-D).** Seven runbooks for seven small workers sharing one image.
   Rejected as premature; D1-B's selector reaches the same place when evidence justifies it.
3. **Sequential tick loop (D2-A).** Would make notification and analytics latency a function of render
   queue depth, squandering the independence F3 makes available. Rejected.
4. **External cron / `CronJob` (D2-C).** Overlap semantics against minutes-long work make the
   scheduler's concurrency policy load-bearing, and cadence management leaves the application
   entirely. Rejected.
5. **Event-driven delivery for v1 (D2-D).** Correct long-term for latency, but polling remains
   structural for time-triggered work (retry backoff, reaping), so it adds a broker without removing
   a loop. Deferred, not rejected on merit.
6. **Unbounded drain (D3-B).** Does not survive contact with an orchestrator: `SIGKILL` truncates it
   into option A at an unchosen moment. Rejected as an illusion of safety.
7. **Immediate termination (D3-A).** Honest and simple, but destroys paid creator work on every
   routine deploy, which GEN-2 then refuses to retry. Rejected as an unacceptable product outcome for
   a saving of a few seconds per release.
8. **Checkpointed, resumable generation (D3-D).** The only complete fix, and out of scope: it requires
   pipeline-level resumability and reopening GEN-2's spend accounting for partial runs.
9. **Singleton host (D4-A).** An availability bottleneck that would also let the per-worker replica
   requirement rot unobserved. Rejected.

---

## Consequences

**Positive.**
- Seven shipped capabilities stop being inert. Most visibly, α9.7 generation begins to *generate*,
  and the relay's revival restores notifications and analytics, which were dormant for reasons
  unrelated to their own code.
- The platform gains its first explicit runtime topology, so "what runs where" becomes an answerable
  question with a recorded rationale.
- Abandonment of in-flight work becomes bounded, configured, and logged, rather than an unexamined
  consequence of process death.
- Replica safety becomes an explicit, stated obligation on every worker — including future ones —
  rather than a property each one happens to have.
- The worker/host division of responsibility becomes a written contract, so every future worker has a
  one-sentence specification.

**Negative / accepted.**
- **A second process class** to build, deploy, monitor, and document — the platform's first.
- **A new configuration surface**: no poll-interval concept exists anywhere today (F1), so intervals,
  drain budgets, backoff ceilings, and the worker selector are all new settings.
- **Deploy cadence becomes coupled to generation duration.** At any finite drain deadline, some
  in-flight generations are lost on deploy and, per GEN-2, are not retried. Bounded and observable,
  but real.
- **Database load rises meaningfully for the first time**, from a source that scales with instance
  count rather than user traffic. Idle backoff mitigates it; pool sizing becomes an operational
  parameter.
- **Latent behaviour becomes live.** Code paths that have only ever run in tests will execute against
  real data continuously. This is the point of the slice, and it is also its principal risk.
- **No container artefacts exist today** (no Dockerfile, no compose file), so the deployment story for
  the new process class starts from nothing.

---

## Load-bearing invariants

Fixed here; a pre-flight may not weaken them.

1. **The host schedules; it never decides.** It may start, stop, retry, and time-bound worker passes.
   It may not contain planning, resolution, ownership, authorization, or any domain policy, and it may
   not reach past a worker's public entrypoint (ADR-0046).
2. **The worker contract.** A worker is responsible **only** for processing available work. It does
   not schedule itself, supervise itself, restart itself after failure, bound its own runtime, or
   coordinate shutdown — those belong exclusively to the host. Worker owns business logic; host owns
   execution lifecycle. A worker that raises has not violated its contract.
3. **No frozen path is modified.** The relay, runner, and outbox are *invoked*, never edited
   (ADR-0042).
3. **A worker failure is never silent.** No worker may become permanently absent without escalation;
   every failed pass is logged and retried with backoff.
4. **Shutdown stops claiming before it stops working.** On `SIGTERM` the host must cease claiming new
   items immediately, and must never begin an item it has already decided not to finish.
5. **Abandonment is bounded and observable.** In-flight work is abandoned only after an explicit,
   configured deadline, and every abandonment is logged with enough identity to explain the resulting
   terminal state.
6. **GEN-2 is not weakened.** No abandoned generation is ever automatically re-run or requeued. The
   host bounds the frequency of loss; it must never respond to loss by re-spending.
7. **Replica safety is a per-worker obligation, never a host guarantee.** Correctness must never
   depend on running exactly one host. Each worker satisfies this through its own mechanism — a lease,
   an atomic claim, a CAS, a `SKIP LOCKED` fetch, or an equivalent — and a worker that cannot must be
   excluded from replicated deployment rather than assumed safe. The host adds no coordination and
   must not become the thing that makes concurrency safe.
8. **The host is not a product surface.** It exposes no API, accepts no user input, and serves no
   requests; observability output is not an interface.

---

## Open questions for the pre-flight

Genuine sub-decisions, none of which change the architecture above:

1. **Default intervals and drain budgets per worker** — the numbers, not the model.
2. **Where enable/disable flags are honoured.** The ADR fixes the principle (D5: workers own *what
   work to do*, the host owns *whether to run at all*); the pre-flight must apply it consistently.
   The concrete instance grounding found is `email_delivery_enabled`, defined at `config.py:501` and
   **read nowhere** — it will start mattering the moment email actually sends. The pre-flight should
   decide whether such a flag is checked by the host, at worker registration, or inside the pass, and
   apply one answer everywhere rather than case by case.
3. **Relay `batch_size` and `max_attempts` are module constants** (`relay_service.py:39-40`), not
   settings, unlike every other worker's batch size. Promote them?
4. **`SKIP LOCKED` on the six unlocked scan queries** — a D4 **throughput** refinement, explicitly not
   a correctness fix; in this slice or a later one?
5. **Per-item isolation for the four workers that lack it** (F5) — recommended above; the pre-flight
   should confirm scope.
6. **Container artefacts.** Does this slice ship a Dockerfile and compose service, or only the
   entrypoint plus documentation? Nothing exists today, and the answer determines whether the slice
   has a deployment story or merely a runnable command.

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-29 | **Proposed.** Drafted at the post-α9.7 grounding stop, which found that no background work has ever executed in production and that seven primitives — including the relay, and therefore notifications and analytics — are dormant. Scoped platform-wide at reviewer direction: generation exposed the problem but does not own it. Recommends **D1-B** (dedicated worker process class with a selector), **D2-B** (supervised task per worker, idle backoff), **D3-C** (bounded drain with per-worker budgets), **D4-B** (replicable), **D5** (mandatory host-level wrapping; touch-file liveness). No pre-flight, no implementation. |
| 2026-07-29 | **Amended (Proposed).** Three reviewer amendments, none altering a recommendation. **(1) D4 restated as a requirement rather than a claim**: the host is *designed for* replicated deployment, and replica safety is a per-worker obligation the host never provides — a worker that cannot demonstrate a lease, atomic claim, CAS, or equivalent must not be replicated. The point-in-time audit is retained as evidence, explicitly labelled as such, and qualified: "safe" means no corruption and no lost work, not uniformly no duplicate external effect (the email worker's ADR-0051 duplicate window widens under replication). **(2) A worker contract added as load-bearing invariant 2**: a worker processes available work and nothing else — scheduling, supervision, restart, runtime bounding, and shutdown coordination belong exclusively to the host. This makes F5 a host responsibility by definition rather than by argument, and gives every future worker a one-sentence specification. **(3) Configuration awareness placed** — workers own *what work to do*, the host owns *whether to run at all*; the `email_delivery_enabled` gap moves from an ADR observation to a scoped pre-flight decision about applying that principle consistently. |
| 2026-07-29 | **Amended, then Accepted.** A terminology block was added to D4 separating two properties the draft had discussed together: **replica-safe processing** (concurrent hosts corrupt nothing and lose nothing, guaranteed by a safe claim mechanism) and **exactly-once external effects** (a distinct property that replica safety does not imply). Where an accepted ADR permits at-least-once delivery — ADR-0051 email — replication **preserves** that semantic rather than strengthening it, and the implementing slice must not "fix" it. Scan behaviour was likewise classified explicitly: the six unlocked scans are **correct**, because ownership is decided by the claim that follows, and merely **potentially inefficient**, because N replicas inspect the same candidates — so `SKIP LOCKED` on scans is a throughput optimisation that must never be cited as a blocker to replication. Requirement wording is now uniformly in terms of *safe claim mechanisms* rather than leases. D1–D5 accepted as drafted; the α9.8 pre-flight follows. |
