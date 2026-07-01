"""Authentication use cases.

Slice α2a: ``RegisterUser``, ``LoginUser``.
Slice α2b: adds ``RefreshSession``, ``LogoutSession``.

Auth-related structured logging event taxonomy (locked in α2a per the
approved improvement F, extended in α2b):

    auth.register.succeeded         info    user_id, tenant_id, session_id
    auth.register.conflict          warn    email_domain
    auth.login.succeeded            info    user_id, session_id, family_id
    auth.login.failed               warn    reason, email_domain
    auth.refresh.rotated            info    user_id, family_id, old_sid, new_sid
    auth.refresh.reuse_detected     warn    user_id, family_id, replayed_sid
    auth.logout.succeeded           info    user_id, session_id

The client always sees the same error for ``login.failed`` — the
``reason`` field is server-side only.
"""
