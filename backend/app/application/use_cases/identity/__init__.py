"""Identity Runtime use cases (Slice α10.0).

Owner-scoped authoring of the creator's world — the profile root and its character,
location and prop children (ADR-0055). The profile is the aggregate: every child write is
fenced on the root's version and bumps it (PF8), so a world is never observed half-edited
by the ingress snapshot that binds it to a generation.

Not the authentication context — that is ``use_cases/auth``. Different bounded context.
"""

from app.application.use_cases.identity._children import CAPS, CHILD_KINDS, ChildKind
from app.application.use_cases.identity.add_identity_child import AddIdentityChild
from app.application.use_cases.identity.create_identity_profile import CreateIdentityProfile
from app.application.use_cases.identity.delete_identity_profile import DeleteIdentityProfile
from app.application.use_cases.identity.get_identity_profile import GetIdentityProfile
from app.application.use_cases.identity.list_identity_profiles import ListIdentityProfiles
from app.application.use_cases.identity.remove_identity_child import RemoveIdentityChild
from app.application.use_cases.identity.update_identity_child import UpdateIdentityChild
from app.application.use_cases.identity.update_identity_profile import UpdateIdentityProfile

__all__ = [
    "CAPS",
    "CHILD_KINDS",
    "AddIdentityChild",
    "ChildKind",
    "CreateIdentityProfile",
    "DeleteIdentityProfile",
    "GetIdentityProfile",
    "ListIdentityProfiles",
    "RemoveIdentityChild",
    "UpdateIdentityChild",
    "UpdateIdentityProfile",
]
