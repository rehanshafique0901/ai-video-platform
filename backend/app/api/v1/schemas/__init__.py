"""Pydantic request / response DTOs for the v1 HTTP surface.

One module per route group (``auth``, ``projects``, …). DTOs are
Pydantic v2 models; domain entities are **not** Pydantic models per
CONTRIBUTING.md §3 — the DTO is the wire format, the entity is the
in-memory business object, and the router maps between them.
"""
