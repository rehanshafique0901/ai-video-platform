"""Shared ``media_asset_id`` link validation for the Clip use cases (α6.3b / D4).

A clip may link to a registered media asset (``clips.media_asset_id``, nullable,
``ON DELETE SET NULL``). When **present**, the link must reference a live media
asset owned by the caller — the FK alone would silently accept a foreign or
soft-deleted asset. A failure raises :class:`ValidationFailedError` (→ ``422``):
the *body* is invalid, the route target (the caller's own track) is fine — this
is NOT a ``404`` (contrast the project/timeline/track *route* gates). Mirrors the
α6.2 :func:`app.application.use_cases.media._links.validate_media_links` pattern.

Shared by :class:`CreateClip` (validates the provided ``media_asset_id``) and
:class:`UpdateClip` (validates only when the client (re)links a non-null asset).
"""

from __future__ import annotations

from uuid import UUID

from app.application.interfaces.unit_of_work import IUnitOfWork
from app.core.errors import ValidationFailedError


async def validate_clip_media_link(
    uow: IUnitOfWork,
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    media_asset_id: UUID | None,
) -> None:
    """Raise ``ValidationFailedError`` if ``media_asset_id`` is set but not linkable.

    A ``None`` link (unset, or an explicit unlink on PATCH) is always valid.
    """
    if media_asset_id is None:
        return
    asset = await uow.media.get_owned(media_asset_id, tenant_id, owner_user_id)
    if asset is None:
        raise ValidationFailedError(
            "media_asset_id does not reference a live media asset you own",
            details={"field": "media_asset_id", "media_asset_id": str(media_asset_id)},
        )
