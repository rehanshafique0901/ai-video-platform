"""The Workflow orchestration aggregate (Slice α7.2).

``WorkflowRun`` is the **second** orchestration aggregate (after α7.1's
``RenderJob``) and the first that *sequences* work: a project-scoped record of a
workflow execution that owns an ordered graph of ``WorkflowStep`` children and
append-only ``WorkflowCheckpoint`` children. A **synchronous, deterministic
runner** advances a run through an **in-code workflow definition** of pure step
handlers — no external providers, no async worker, no scheduler (those are α8.x).

Architectural principles (WORKFLOW_RUN_AGGREGATE.md / ADR-0040 / α7.2 pre-flight):

* **Self-owned orchestration aggregate (D9).** The run owns its own run/step
  status machines and coordinates purely through its status + domain events
  written to the ``event_outbox``. It never mutates ``projects.version`` and never
  reaches into ``RenderJob`` / ``MediaAsset`` / ``Timeline``.
* **Status-guarded CAS concurrency (D3.2).** ``workflow_runs`` / ``workflow_steps``
  carry **no ``version`` column** (they are not in ``_VERSION_BUMP_TABLES``), so
  every lifecycle transition is a status-predicated compare-and-swap
  (``UPDATE … WHERE status IN (<allowed_from>)``); non-transition metadata is
  last-writer-wins. This is the workflow-specific concurrency model — a documented
  divergence from ``RenderJob``'s version-fenced cancel, forced by the schema.
* **Steps are deterministic and side-effect-free (D3.11).** A step handler is a
  **pure function** returning a :class:`~app.domain.workflow.registry.StepResult`
  that *describes* what should happen (output + resume state + declarative
  provider commands) — it never calls providers directly. The runner (the
  imperative shell) interprets the result. This makes the eventual move to
  Celery/LangGraph an execution concern, not a domain rewrite.
* **Ownership is derived through the project** (``project_id → projects.owner_user_id``);
  the run carries no owner columns of its own.
* **Boundary invariant (D3.10).** The run owns orchestration/graph state only —
  run status, step sequence + status, and checkpoints. A render-producing step
  (α8.x) creates a ``RenderJob`` and links it by ``render_jobs.workflow_run_id``;
  it does not fold render state into the run.
"""
