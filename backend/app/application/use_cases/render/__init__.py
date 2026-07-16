"""Rendering & Export use cases (Slice α7.1 — RenderJob CRUD + cancel).

The first *orchestration* slice: create / list / get a render job, and cancel one
(a version-fenced status transition). Cross-aggregate coordination is via domain
events on the ``event_outbox`` only (blueprint §7.1 / D9) — these use cases never
mutate another aggregate's state. The background render worker that actually
drives ``queued → running → succeeded/failed`` (and resolves Release/Draft
ProjectVersion binding) is deferred to α8.x.
"""
