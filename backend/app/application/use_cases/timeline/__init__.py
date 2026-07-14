"""Timeline aggregate use cases (Slices α6.3a — Timeline + Tracks; α6.3b — Clips).

Provision / read / update the timeline root and CRUD its tracks and clips. Every
use case runs the visibility gate — project ownership
(:meth:`IProjectRepository.get_owned` → ``404``) then timeline resolution
(:meth:`ITimelineRepository.get_by_project` → ``404``), and for clips the track
(→ ``404``) and clip (→ ``404``) — before any write, and every child (track /
clip) mutation advances the single aggregate OCC token ``timelines.version``
(ADR-0038). Clips never touch ``projects.version``.
"""
