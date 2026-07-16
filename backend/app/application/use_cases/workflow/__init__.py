"""Workflow-run use cases (Slice α7.2) — the runner and its lifecycle.

``create`` / ``list`` / ``get`` / ``cancel`` mirror the α7.1 ``RenderJob`` surface;
``advance`` is the new piece — a **synchronous, deterministic runner** that drives
a run through its in-code workflow definition of pure step handlers (D3.11),
persisting step transitions + append-only checkpoints and producing outbox events
(D9), all within one UnitOfWork transaction. No external providers, no async
worker, no scheduler (α8.x).
"""
