"""Rendering & Export bounded context (Slice α7.1 — first orchestration slice).

A :class:`~app.domain.render.render_job.RenderJob` is the **request to render a
project's timeline** and the record of that request's lifecycle. It is the first
*orchestration* aggregate (contrast the α5–α6 *domain-model* aggregates): it owns
its own state machine (:class:`~app.domain.render.render_status.RenderStatus`)
and is coordinated purely through its own status + domain events on the
``event_outbox`` (ADR-0039, blueprint §7.1 / D9).

Boundary invariant (α7.1 pre-flight D3.10): a ``RenderJob`` owns **only
orchestration metadata** (queue, priority, status, error envelope). It does not
own rendered files (``MediaAsset``), exported files (``ExportJob``), workflow
state (``WorkflowRun``), or timeline edits (``Timeline``) — it merely *references*
them by FK and *coordinates* via events. α7.1 ships CRUD + OCC + the cancel
transition only; the background render worker (and Release/Draft
ProjectVersion-binding) is deferred to α8.x.
"""
