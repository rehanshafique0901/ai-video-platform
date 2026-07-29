# Phase 3 — α9.8 Pre-flight: Worker Runtime Host

> **Status:** **Approved.** **Baseline (frozen):** `v0.4.50-phase3-alpha9.7`
> **Target version:** `0.4.51-phase3-alpha9.8-dev` → `0.4.51-phase3-alpha9.8`
> **Governed by:** [`ADR-0053`](../decisions/ADR-0053-worker-runtime-host.md) (Accepted)
>
> Design blueprint only. No code accompanies this document.

---

## 1. What the ADR fixed, and what this document adds

ADR-0053 settled five decisions: a **dedicated worker process class with a selector** (D1), **one
supervised asyncio task per worker** with idle backoff (D2), **bounded drain** on shutdown with
per-worker budgets (D3), **replicable by construction** with replica safety as a per-worker obligation
(D4), and **mandatory host-level exception containment** plus touch-file liveness (D5). It also fixed
the **worker contract**: a worker processes available work and nothing else; scheduling, supervision,
restart, runtime bounding, and shutdown coordination belong exclusively to the host.

This pre-flight turns those into a build plan. It resolves the ADR's six open questions, and raises
**three findings the ADR could not have known**, one of which changes scope:

- The relay is a **frozen path** (ADR-0042). One ADR open question, taken literally, would have
  required a freeze override for a cosmetic gain. It is withdrawn (PF7).
- "Stop claiming immediately" is **not achievable at pass granularity** for the workers where it
  matters, because a single pass claims up to `batch_size` items serially. Fixing this is a
  configuration change, not a code change (PF4). Without it, D3 does not deliver what it promises.
- The worker entrypoint inherits `run_dev.py`'s **Windows event-loop constraint**, which is easy to
  miss and would make the host unrunnable locally for part of the team (§10).

---

## 2. Pre-flight rulings that need your explicit sign-off

### PF1 — the host lives in a new top-level `app/runtime/` package, peer to `app/api/`

The host is a **delivery mechanism**, exactly as the API is. `app/api/` turns HTTP requests into
application calls; `app/runtime/` turns elapsed time into application calls. They are peers: both are
entrypoint layers that depend on `application` and `core` and are depended on by nothing.

Rejected placements: `app/core/` (that is configuration, container, and security — the host is not
infrastructure plumbing but a process); `app/application/use_cases/` (the host is not a use case and
must never be invokable from one); `backend/scripts/` alone (the supervisor is real logic and needs
real unit tests, so only the thin entrypoint belongs in `scripts/`).

```
backend/app/runtime/
    __init__.py
    worker_host.py       # WorkerHost: supervision, scheduling, shutdown
    worker_registry.py   # WorkerSpec + build_registry(settings) — the only type-aware module
    liveness.py          # touch-file marker
backend/scripts/run_worker.py   # thin entrypoint, mirrors run_dev.py
```

### PF2 — the supervisor is result-agnostic; the registry is the only type-aware component

ADR-0053 invariant 1 forbids the host from interpreting worker results. But the seven `run_once()`
methods return seven different types (`RelayResult`, `RenderPollResult`, `GenerationPollResult`, …),
and idle backoff needs to know whether a pass found work. Both constraints are satisfied by putting
the type knowledge in the **registry**, not the supervisor:

```python
@dataclass(frozen=True)
class WorkerSpec:
    name: str                                  # "relay", "generation", …
    run_pass: Callable[[], Awaitable[object]]  # build-and-run exactly one pass
    found_work: Callable[[object], bool]       # registry-supplied; supervisor never introspects
    interval: timedelta                        # poll interval when the last pass found work
    idle_ceiling: timedelta                    # backoff ceiling when idle
    drain_budget: timedelta                    # D3 bound for an in-flight pass
```

`WorkerHost` receives a `list[WorkerSpec]` and treats every result as an opaque `object`, passing it
straight to the spec's own `found_work` and to the log. It cannot import a worker, a use case, or a
result type. The registry (`worker_registry.py`) is where `container.get_relay_service()` and
`lambda r: r.fetched > 0` live — composition-root knowledge, in the composition root.

`run_pass` **constructs a fresh worker per pass** through the existing container factory. Every
factory already returns a new unit of work per call, so this costs nothing and guarantees no state
leaks between passes.

### PF3 — selector semantics: default all, unknown names fail fast

`--workers relay,generation` on the CLI, `WORKER_SET` in the environment, unset means **all
registered workers**. An unrecognised name is a **startup error**, never a silent no-op — the failure
mode of a typo'd selector silently disabling generation is exactly the class of bug this whole slice
exists to eliminate. An explicitly empty selector is also an error.

### PF4 — long-item workers get `batch_size = 1`, so drain granularity equals item granularity

**This ruling is what makes D3 honest, and it is the finding I most want reviewed.**

> **Principle.** **Batch size is chosen according to shutdown semantics, not throughput.** Where a
> single work item is materially longer than a queue scan, `batch_size = 1` preserves prompt drain
> behaviour. Where the scan cost dominates the item cost, batching remains correct and useful.

This inverts the usual reason for the setting, so it is stated first: the number below is not a
performance tuning parameter, and anyone later "optimising" it upward would be trading away the
host's shutdown contract without realising it.

ADR-0053 D3 requires that on `SIGTERM` the host "cease claiming new items immediately". The host can
only stop *passes*, because reaching inside a running pass would violate the worker contract. But a
single pass claims up to `batch_size` items **serially**:

| Worker | Current batch | Item duration | Worst-case pass |
|---|---|---|---|
| Generation | 5 | minutes | **5 × minutes** |
| Render | 10 | minutes | **10 × minutes** |
| Export | 10 | minutes | **10 × minutes** |
| Enrichment | 10 | seconds–minutes | up to 10 × minutes |
| Publish | 10 | seconds–minutes | up to 10 × minutes |

So a `SIGTERM` arriving during item 1 of a generation pass would still let the worker claim items 2–5
— starting *four new expensive runs* the host has already decided to abandon. That is precisely what
D3 forbids, and no drain budget can rescue it.

**Ruling: set `batch_size = 1` for generation, render, export, enrichment, and publish.** A pass then
claims exactly one item, so "do not start a new pass" *is* "do not claim another item", and the drain
budget bounds one item rather than ten.

This costs nothing. Batch size exists to amortise the scan across several items; when item work
exceeds the scan by three or four orders of magnitude, batching buys no throughput whatsoever. Ten
renders processed serially in one pass and ten processed across ten passes take the same time, minus
a few seconds of polling interval. Throughput scales by adding hosts (D4), not by lengthening passes.

Relay (100 tiny events) and email (20 quick sends) **keep their batches**: their item work is
comparable to their scan cost, so batching is genuinely useful and a whole pass still drains fast.

### PF5 — shutdown is stop-claiming plus a bounded drain of the single in-flight pass

`SIGTERM`/`SIGINT` set an `asyncio.Event`. Each worker task checks it before starting a pass and exits
its loop if set; a task sleeping between passes wakes immediately rather than serving out its
interval. The pass already running is awaited under `asyncio.wait_for(..., drain_budget)`; on timeout
it is cancelled and the abandonment is logged at `error` with the worker name and elapsed time.

The host then awaits all tasks, calls `container.shutdown()`, and exits `0` for a clean drain or a
distinguishable non-zero code if anything was abandoned, so a deploy pipeline can surface lost work.

Per-worker budgets, because a relay pass drains in milliseconds and a generation pass in minutes.
Operators must set the orchestrator's termination grace period to at least the largest configured
budget; the pre-flight documents this, and §14 records what happens if they do not.

### PF6 — enable/disable flags are honoured at **registration**, mirroring the destination registry

ADR-0053 D5 fixed the principle (host decides *whether to run*, worker decides *what work to do*) and
left placement to this document. Ruling: a disabled capability is **absent from the registry** — it is
never scheduled, never logged as idle, and never touched.

This is the platform's existing idiom: α8.6c and α9.6 register a destination adapter only when its
credentials are configured, fail-soft. Applying it here means `email_delivery_enabled=false` produces
a host with no email worker, exactly as an unconfigured TikTok produces a registry with no TikTok.

The alternative — a flag checked inside the pass — would have the host faithfully scheduling a worker
that immediately returns, forever, and would put a "should I exist?" decision inside a component whose
contract says it only decides what work to do.

This resolves the `email_delivery_enabled` gap: the setting exists at `config.py:501` and is currently
read **nowhere**. It becomes a registration predicate.

### PF7 — the relay's constants stay put: promoting them would edit a frozen path

> **Principle.** **Prefer an existing extension point over modifying a frozen component.** Where a
> frozen module already exposes a seam that satisfies the need, using it is not a workaround — it is
> the intended path, and it is strictly better than a freeze override.

ADR-0053 open question 3 asked whether `RelayService`'s module-level `DEFAULT_BATCH_SIZE` and
`DEFAULT_MAX_ATTEMPTS` (`relay_service.py:39-40`) should become settings like every other worker's
batch size.

**They must not, and I am withdrawing the question.** `backend/app/application/use_cases/relay/relay_service.py`
is **frozen** (`check_frozen_platform.py:72`, mirroring ADR-0042 §D1). Editing it requires a
`Freeze-Override:` trailer and a new ADR — an enormous instrument for a consistency nicety.

It is also unnecessary. `relay_once(self, *, batch_size: int | None = None)` already accepts a
per-call override, so the registry can pass a configured batch size **without touching the frozen
file at all**. If tuning is ever wanted, a new `relay_batch_size` setting read by the *registry* gets
it for free. I propose not even adding that setting in this slice — the default of 100 is untested in
production and inventing a knob before there is evidence is how config surfaces rot — but the seam is
there.

**This slice touches no frozen path.** The relay is called, never modified; `distributed_lock_manager.py`
and `locks.py` are likewise only consumed.

### PF8 — the four non-isolating workers gain per-item isolation, in this slice

F5 in the ADR: render, export, enrichment, and publish let an unclassified exception propagate out of
`run_once()`, discarding the rest of the batch. Host wrapping (D5) makes this non-fatal, but without
per-item isolation one bad job still destroys its batch-mates' pass.

PF4 reduces the blast radius to a single item, which weakens the argument — but does not remove it,
because the four workers are also the ones whose exceptions are least predictable (ffmpeg, object
storage, third-party HTTP), and the pattern already exists verbatim in relay, email, and generation.
Bringing four workers into conformance with three is cheap and makes the fleet uniform.

None of the four is frozen. Confirmed against `FROZEN_PATHS`.

### PF9 — liveness is one touch-file per worker

`<liveness_dir>/<worker_name>.alive`, updated after each pass **completes** — success or contained
failure alike, because the signal is "the loop is turning", not "the work succeeded". Per-worker
rather than per-host, so a single wedged worker is detectable while the others keep the process
looking healthy. A probe compares file age against that worker's `idle_ceiling` times a factor.

Disabled by default (unset directory ⇒ no marker written), so tests and local runs write nothing.

### PF10 — no container artefacts in this slice

ADR-0053 open question 6. The repository has **no Dockerfile, no compose file, no CI deployment**
whatsoever. Introducing containerisation properly means base image selection, a multi-stage build,
getting ffmpeg into the image, secret handling, and a compose topology for API + worker + Postgres —
a deployment slice with its own decisions, not a coda to this one.

This slice ships a **runnable entrypoint plus operator documentation**: how to start the host, what
the selector does, how to size the drain budget against a termination grace period, and what the exit
codes mean. Containerisation becomes its own roadmap row.

---

## 3. `WorkerHost` — the supervisor

One class, roughly 150 lines, with no knowledge of any worker.

```python
class WorkerHost:
    def __init__(self, specs: Sequence[WorkerSpec], *, liveness: Liveness | None = None,
                 clock: Callable[[], datetime] | None = None) -> None: ...

    async def run(self) -> HostResult:      # runs until stop is requested
    def request_stop(self) -> None:         # idempotent; also wired to SIGTERM/SIGINT
```

`run()` starts one `asyncio.Task` per spec and awaits them. Each task is a loop:

1. If stopping, exit.
2. Run one pass inside `try/except Exception`.
3. On success: reset the interval to `spec.interval` if `found_work(result)` else back off; touch the
   liveness marker; log the pass at `debug` with the worker name and duration.
4. On exception: log at `error` with the traceback, apply failure backoff, touch liveness, continue.
   **The task never dies from a failed pass** (ADR-0053 invariant 3).
5. Sleep until the next tick, interruptibly — the sleep must abort the instant stop is requested, so
   a shutdown is never delayed by an idle worker's ceiling.

`HostResult` reports, per worker, passes run, failures, and whether it was abandoned at drain — enough
for the entrypoint to choose an exit code, and nothing that interprets a worker's own result type.

**Backoff.** Idle: `interval` doubling to `idle_ceiling`, reset the moment a pass finds work. Failure:
the same curve, tracked separately so a failing worker backs off even while the queue is full — this
is what prevents a hot loop against a broken dependency, which is the realistic way a worker host
melts a database.

### Host invariants

Two rules the rest of this document already assumes. Naming them makes them survivable across a
future refactor, which is the point — both are the kind of property that is obvious while you are
writing the scheduler and invisible to whoever next changes it.

**HOST-1 — worker registration is immutable for the lifetime of the process.**
The host computes its enabled worker set **once, at startup**, and never adds, removes, replaces, or
re-reads it while running. Configuration changes take effect on the next process start, exactly as
they do for the API. There is no dynamic task creation, no hot reload, and no runtime enable/disable
path.

This is what keeps supervision deterministic: the set of tasks is fixed the moment `run()` begins, so
shutdown has a known set to drain, liveness has a known set of markers, and `HostResult` has a known
set of rows. It also follows directly from PF6 — if a disable flag is a *registration* predicate, then
registration must be a startup event, or the flag's meaning becomes ambiguous the moment someone edits
config on a running host.

**HOST-2 — a worker's failure never suppresses the scheduling of any other worker.**
Failure is contained to the worker that produced it. A raising pass, a worker in failure backoff, a
worker wedged for its entire drain budget, or a task that has stopped entirely must have **no effect
whatsoever** on when any other worker's next pass is scheduled. The only cross-worker coupling the host
may have is the shutdown signal.

This is the invariant a future "simplification" is most likely to break — collapsing seven supervised
tasks into one loop, or awaiting passes in sequence, would satisfy every other requirement here while
silently violating this one and reintroducing the coupling ADR-0053 D2-A rejected. §9 tests it
directly rather than trusting the structure to preserve it.

---

## 4. The registry

`build_registry(settings) -> list[WorkerSpec]` is the only place that knows what a worker is. One
entry each:

| Worker | `found_work` | Interval | Idle ceiling | Drain budget | Batch |
|---|---|---|---|---|---|
| `relay` | `r.fetched > 0` | 1s | 15s | 30s | 100 (unchanged) |
| `generation` | `r.scanned > 0 or r.reaped > 0` | 5s | 60s | **600s** | **1** (PF4) |
| `render` | `r.scanned > 0` | 5s | 60s | **600s** | **1** (PF4) |
| `export` | `r.scanned > 0` | 5s | 60s | **600s** | **1** (PF4) |
| `enrichment` | `r.scanned > 0` | 10s | 120s | 300s | **1** (PF4) |
| `publish` | `r.scanned > 0` | 5s | 60s | 300s | **1** (PF4) |
| `email` | `r.scanned > 0` | 10s | 120s | 60s | 20 (unchanged) |

All values are settings with these defaults, not literals. The relay's 1s interval reflects that it
is the delivery path for notifications and analytics, where latency is user-visible.

Registration predicates (PF6): `email` is registered only when `email_delivery_enabled` is true.
No other worker currently has an enable flag; the seam exists if one gains it.

---

## 5. Configuration

New `Settings` fields, following the existing `Field(default=…, description=…, gt=0)` convention:

```
worker_set                              str | None = None   # None ⇒ all
worker_liveness_dir                     str | None = None   # None ⇒ disabled
worker_<name>_interval_seconds          float               # ×7
worker_<name>_idle_ceiling_seconds      float               # ×7
worker_<name>_drain_budget_seconds      float               # ×7
worker_failure_backoff_cap_seconds      float = 300.0
```

**Changed defaults** (PF4): `generation_worker_batch_size` 5 → 1, `render_batch_size` 10 → 1,
`export_batch_size` 10 → 1, `enrichment_batch_size` 10 → 1, `publish_batch_size` 10 → 1. These are
behaviour changes to existing settings and must be called out in the changelog, not slipped in.

Twenty-two new settings is a lot. The alternative — one global interval — was rejected by D2 for good
reason, and per-worker naming keeps them greppable. They are grouped under a `# --- α9.8 worker
runtime ---` banner in `config.py`.

---

## 6. Entrypoint — `scripts/run_worker.py`

Mirrors `run_dev.py` deliberately, including the part that is easy to miss:

```python
python scripts/run_worker.py                          # all workers
python scripts/run_worker.py --workers relay,email    # a subset
python scripts/run_worker.py --log-level debug
```

It parses arguments, calls `container.init(settings)`, builds the registry, installs signal handlers
via `loop.add_signal_handler`, runs the host, then `await container.shutdown()` and exits with the
code from `HostResult`.

**Windows.** `run_dev.py` exists largely because `psycopg`'s async driver needs `SelectorEventLoop`
and Python 3.12+ bypasses the global policy (`run_dev.py:1-21`). The worker entrypoint has the same
database dependency and needs the same `asyncio.run(..., loop_factory=...)` treatment, or it simply
will not connect on Windows. `loop.add_signal_handler` is additionally unavailable on Windows, so
`SIGINT` falls back to `KeyboardInterrupt` handling there. Documented as a local-development
limitation; production is Linux.

`container.init()`/`shutdown()` need **no changes** — factories already produce a fresh unit of work
per call, and `shutdown()` disposes the engine and closes shared HTTP clients.

---

## 7. Import-linter

One new contract, expressing the peer relationship in PF1:

```
[importlinter:contract:runtime-is-an-entrypoint]
name = Nothing imports the worker runtime (ADR-0053 PF1)
type = forbidden
source_modules = app.api, app.application, app.domain, app.infrastructure
forbidden_modules = app.runtime
```

`app.runtime` may import `app.application` and `app.core`; nothing may import it. This is what makes
"the host schedules, it never decides" (invariant 1) mechanical rather than aspirational — a use case
cannot reach the scheduler even by accident.

A second contract is tempting — forbidding `app.runtime.worker_host` from importing anything except
`app.runtime.*` and stdlib, to pin PF2's result-agnosticism — but import-linter cannot express "may
not import concrete worker types" cleanly when the registry legitimately does. The supervisor's
purity is instead enforced by a unit test that drives it entirely with fake specs (§9).

---

## 8. Gate impact — Stage 26

The gate becomes **27 stages** (0–26). New stage, following the Stage 24/25 shape:

```python
Stage(
    number=26,
    title="worker runtime host integration",
    cmd=[py, "-m", "pytest", "-m", "integration",
         "tests/integration/runtime/test_worker_host.py"],
    requires_db=True,
),
```

It proves the thing the slice exists for, against the live database: a **real** queued generation,
seeded through the real ingress, is drained to completion by a **real** host built from the real
container — no fakes in the path. This is the first test in the repository that demonstrates
background work actually executing without a test calling `run_once()` by hand.

The provider pipeline is stubbed as in Stage 25 (the point is the host, not ffmpeg), but the
container wiring, registry, scheduling, and shutdown are genuine.

---

## 9. Test plan

**Unit — supervisor (fake specs only, no container, no DB).** This is where PF2 is enforced: if the
supervisor can be fully driven by fakes, it demonstrably knows nothing about real workers.

- A pass that raises does not kill the task; the next pass still runs (F5, invariant 3).
- Repeated failures back off, capped, and never hot-loop.
- Idle backoff grows to the ceiling, and resets on the first pass that finds work.
- **HOST-2**, three ways, because this is the invariant most likely to be refactored away:
  a spec that sleeps 2s does not delay a 10ms spec's next pass; a spec that raises on every pass does
  not change a healthy spec's pass count; and a spec wedged for its entire drain budget does not
  prevent other workers from being scheduled meanwhile. Any of these fails the moment someone
  collapses the supervisor into a sequential tick (ADR-0053 D2-A).
- **HOST-1**: mutating the settings object after `run()` has started changes nothing about the running
  worker set — no task appears, none disappears.
- `request_stop()` starts no further passes (invariant 4).
- An in-flight pass exceeding its drain budget is cancelled, logged, and reported as abandoned
  (invariant 5); one within budget completes untouched.
- An interruptible sleep: a worker idling on a 120s ceiling stops within milliseconds of a stop
  request, rather than after its ceiling.
- The liveness marker is touched after both successful and failed passes.

**Unit — registry.**
- The selector resolves names; an unknown name raises at build time (PF3); empty raises.
- `email_delivery_enabled=false` omits the email worker entirely (PF6).
- Every spec's `found_work` returns `True` for a work-bearing result and `False` for an empty one —
  a table test across all seven real result types, which is the only place those types are touched.

**Unit — the four newly isolating workers (PF8).**
- Each: given three items where the second raises an unclassified exception, all three are attempted
  and the pass returns normally. These are the regressions that fail today.

**Integration — Stage 26 (live DB).**
- Seed a queued generation via the real `CreateGeneration`; run the host with `--workers generation`;
  assert the generation reaches a terminal state with no manual `run_once()` call.
- Seed an outbox event; run with `--workers relay`; assert it is marked published — proving the
  notification and analytics revival, which is the slice's least visible but most valuable effect.
- Request stop mid-flight and assert the drain completes cleanly and no further items are claimed.
- Run **two hosts concurrently** against the same queue and assert every item is processed exactly
  once, with no lost or duplicated work. This is the D4 replica-safety claim, tested rather than
  asserted.

---

## 10. Risks and residuals

| Risk | Assessment |
|---|---|
| **Latent code paths execute for the first time** | The principal risk of the slice, and unavoidable: this is what "nothing has ever run" means. Six workers have only ever run under tests. Mitigated by the selector — a cautious rollout can enable workers one at a time. |
| **Drain budget exceeds the orchestrator grace period** | Then `SIGKILL` truncates the drain and ADR-0053 D3-B's failure mode returns. Not preventable in code; documented for operators, and abandonment is logged either way so the loss is visible. |
| **Generation loss on deploy** | Accepted and bounded by ADR-0053 D3. PF4 improves it materially: one in-flight generation per host rather than up to five. |
| **Idle polling load** | Seven workers × N hosts against Postgres. Idle backoff mitigates; the relay's 1s floor is the largest contributor and is a deliberate latency choice. First real measurement should follow deployment. |
| **`batch_size = 1` throughput concern** | Analysed in PF4 and judged nil: pass-serial and poll-serial processing are the same wall-clock time when item work dominates. Worth re-measuring, not worth pre-optimising. |
| **Two hosts contending on unlocked scans** | Correct but wasteful (ADR-0053 D4). Explicitly a throughput matter; `SKIP LOCKED` on scans is deferred with no correctness implication. |
| **22 new settings** | Real config-surface growth. Grouped and defaulted so nothing must be set to run. |

---

## 11. Explicitly out of scope

- **Containerisation** — no Dockerfile or compose file (PF10). Its own slice.
- **Metrics, tracing, dashboards** — structured logs and a liveness file only; a DB heartbeat is
  ADR-0053 D5's deferred option C.
- **`SKIP LOCKED` on the six scan queries** — throughput, not correctness.
- **Resumable generation** — the complete fix for deploy-time loss; needs pipeline checkpointing and
  a GEN-2 amendment (ADR-0053 D3-D).
- **Event-driven wakeup (`LISTEN`/`NOTIFY`)** — ADR-0053 D2-D, deferred.
- **Any change to what a worker does.** PF8 changes only *isolation*, never a claim rule, a lease, a
  retry policy, or an outcome. No behaviour a previous ADR fixed is revisited.
- **Autoscaling policy, deployment manifests, runbooks** beyond the operator notes in §6.

---

## 12. Implementation order

1. `app/runtime/` package: `WorkerSpec`, `Liveness`, `WorkerHost` — supervisor first, driven entirely
   by fakes, with its unit tests green before any real worker is wired.
2. Supervisor unit tests (§9), including the HOST-1, HOST-2, and drain-budget regressions.
3. PF8: per-item isolation for render, export, enrichment, publish, plus their unit tests.
4. `worker_registry.py`: the seven specs, selector resolution, registration predicates.
5. Configuration: new settings, the five changed batch defaults (PF4).
6. `scripts/run_worker.py`: signal handling, Windows loop factory, exit codes.
7. Import-linter contract.
8. Integration test + CI Stage 26.
9. Full 27-stage ephemeral-PostgreSQL gate; version bump to `0.4.51-phase3-alpha9.8-dev`; CHANGELOG;
   one feature commit; push; release-review PR.

---

## 13. Architectural decision check

Everything here is a consequence of ADR-0053, or a placement decision it explicitly delegated.

- **PF1/PF2** implement invariant 1 (the host schedules, never decides) and invariant 2 (the worker
  contract).
- **HOST-1/HOST-2** name behaviours the ADR already implies rather than adding any: HOST-1 is what
  makes PF6's registration-time predicates well-defined, and HOST-2 is the testable form of ADR-0053
  D2's rejection of a sequential tick. Neither constrains anything the ADR left open.
- **PF4** is the mechanism by which D3's "stop claiming immediately" becomes true rather than
  aspirational. It changes configuration defaults, not architecture.
- **PF6** applies the D5 configuration-awareness principle, using the existing fail-soft registration
  idiom.
- **PF7** withdraws an ADR open question on the discovery that answering it would breach ADR-0042.
  This is the pre-flight protecting a freeze, not overruling the ADR.
- **PF8** brings four workers into conformance with a pattern three already follow. No worker's
  behaviour changes; only whether its neighbours survive its failure.
- **PF10** defers a question the ADR asked, on scope grounds.

**No new ADR is required.** No decision here is architectural in the ADR-0053 sense: none is
irreversible, none changes a bounded-context boundary, none touches a frozen path, and none revisits
a previously frozen invariant. GEN-2 is upheld unchanged — nothing in this slice re-runs, requeues, or
retries a generation.
