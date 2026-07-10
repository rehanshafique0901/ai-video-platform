"""User-management use cases (Phase 3 α4+).

α4 ships :class:`UpdateUserProfile` — the version-fenced display-name
mutation underpinning ``PATCH /api/v1/users/me``, and the canonical
example of the α4 authenticated-write pattern (see α4 pre-flight §10
exit criteria + ``docs/api/AUTH_ENDPOINTS.md`` §8).

Later slices extend this package with additional user-management use
cases (password change, email change, account closure, admin edits);
each new use case is a peer file, never bloated into
``update_profile.py``.
"""
