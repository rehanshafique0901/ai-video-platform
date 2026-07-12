"""Timeline aggregate use cases (Slice α6.3a — Timeline + Tracks).

Provision / read / update the timeline root and CRUD its tracks. Every use case
runs the two-level gate — project ownership (:meth:`IProjectRepository.get_owned`
→ ``404``) then timeline resolution (:meth:`ITimelineRepository.get_by_project`
→ ``404``) — before any write, and every child (track) mutation advances the
single aggregate OCC token ``timelines.version`` (ADR-0038). α6.3b adds clips.
"""
