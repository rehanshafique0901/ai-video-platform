"""SQLAlchemy implementation of ``INotificationRepository`` (α8.5b.3 write + α8.5b.3r read).

α8.5b.3 shipped the write sink of the notification projection: one ``INSERT`` per
delivered export terminal event. α8.5b.3r adds the owner-scoped read/query surface
(``list_for_user`` / ``count_unread`` / ``mark_read`` / ``mark_all_read``) — pure
additive methods; the write path is untouched. The adapter never reads outbox events,
never orchestrates, and never mutates export/render state (W8.5b.6).

**Exactly-once is DB-owned (W8.5b.7).** :meth:`add` maps the partial-unique
``uq_notifications_user_id_source_event_id`` violation to ``ConflictError`` so the use
case resolves a relay redelivery as an already-notified no-op — the constraint, not
subscriber control flow, is the race-safe backstop. Mirrors the shape of
``MediaRepository.add`` (deterministic-key idempotency + ``ConflictError`` recovery).

**Owner-scoped reads (W8.5b.8).** Every query filters ``user_id = :uid`` (no ``tenant_id``
scope — a notification is addressed to a user). ``list_for_user`` reuses the α5a keyset
scan shape (``created_at DESC, id DESC`` + row-value cursor comparison) over the existing
``ix_notifications_user_id_created_at`` index; ``count_unread`` matches the partial
``ix_notifications_user_id_unread`` predicate. **Read-state mutations write only ``read_at``**
(W8.5b.9) and never reshuffle the feed (W8.5b.10).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.interfaces.repositories import (
    INotificationRepository,
    NotificationEmailDelivery,
)
from app.core.errors import ConflictError
from app.domain.notifications.notification import Notification as NotificationEntity
from app.infrastructure.db.models.notifications import Notification as NotificationRow

# α9.5 (ADR-0051): ``payload`` keys whose name starts with this prefix are reserved for internal
# runtime bookkeeping (e.g. ``_email`` — email retry/terminal state). They are an implementation
# detail, NEVER part of the public notification contract, and are stripped from every row this
# adapter hands back to the application/read model. Sanitisation is centralised in the single
# row→entity boundary (:func:`_row_to_entity`), so no endpoint can accidentally expose them. A
# future migration may relocate this bookkeeping into dedicated columns with no external change.
_RESERVED_PAYLOAD_PREFIX = "_"

# The one reserved namespace in use today: email delivery retry/terminal bookkeeping.
_EMAIL_NS = "_email"


def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip reserved (``_``-prefixed) bookkeeping keys from a stored payload (α9.5, ADR-0051)."""
    return {k: v for k, v in payload.items() if not k.startswith(_RESERVED_PAYLOAD_PREFIX)}


class NotificationRepository(INotificationRepository):
    """Notification persistence adapter (α8.5b.3 write + α8.5b.3r read)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        user_id: UUID,
        kind: str,
        title: str,
        body: str | None,
        payload: dict[str, Any],
        source_event_id: UUID | None,
    ) -> NotificationEntity:
        row = NotificationRow(
            user_id=user_id,
            kind=kind,
            title=title,
            body=body,
            payload=payload,
            source_event_id=source_event_id,
            # In-app "delivery" = the committed, visible row (Fork A). Email stays NULL.
            delivered_in_app_at=func.now(),
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as e:
            # 23505 on ``uq_notifications_user_id_source_event_id`` → this recipient was
            # already notified for this source event (relay redelivery). Surface as
            # ConflictError so ``CreateNotification`` maps it to an idempotent no-op
            # (W8.5b.7 — exactly-once is enforced by the DB, not control flow).
            raise ConflictError(
                "notification already exists for this recipient + source event",
                details={"constraint": _extract_constraint_name(e) or "unknown"},
            ) from e
        await self._session.refresh(row)
        return _row_to_entity(row)

    # --- Read / query surface (α8.5b.3r) ----------------------------------------

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None = None,
    ) -> list[NotificationEntity]:
        # Keyset scan — the α5a ``ProjectRepository.list_owned`` shape. Owner-scoped
        # (W8.5b.8), archived excluded, ``read_at`` irrelevant to membership/order
        # (W8.5b.10). ``created_at DESC, id DESC`` is a total order over the existing
        # ``ix_notifications_user_id_created_at`` index; the ``id`` tie-break resolves
        # equal-timestamp rows without a composite keyset index (E1 — deferred).
        stmt = (
            select(NotificationRow)
            .where(NotificationRow.user_id == user_id)
            .where(NotificationRow.archived.is_(False))
        )
        if after is not None:
            after_created_at, after_id = after
            stmt = stmt.where(
                tuple_(NotificationRow.created_at, NotificationRow.id)
                < (after_created_at, after_id)
            )
        stmt = stmt.order_by(NotificationRow.created_at.desc(), NotificationRow.id.desc()).limit(
            limit
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_row_to_entity(row) for row in rows]

    async def count_unread(self, user_id: UUID) -> int:
        # Matches the ``ix_notifications_user_id_unread`` partial predicate exactly, so
        # the badge count is an index-only scan. Owner-scoped (W8.5b.8).
        stmt = (
            select(func.count())
            .select_from(NotificationRow)
            .where(NotificationRow.user_id == user_id)
            .where(NotificationRow.read_at.is_(None))
            .where(NotificationRow.archived.is_(False))
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def mark_read(self, user_id: UUID, notification_id: UUID) -> NotificationEntity | None:
        # Owner-scoped CAS on ``read_at IS NULL``: writes ONLY ``read_at`` (+ trigger-owned
        # ``updated_at``) — never identity / source-event / provenance (W8.5b.9). A foreign
        # id can neither be read nor mutated (W8.5b.8).
        upd = (
            update(NotificationRow)
            .where(NotificationRow.id == notification_id)
            .where(NotificationRow.user_id == user_id)
            .where(NotificationRow.read_at.is_(None))
            .values(read_at=func.now(), updated_at=func.now())
            .returning(NotificationRow)
        )
        updated_row = (await self._session.execute(upd)).scalar_one_or_none()
        if updated_row is not None:
            return _row_to_entity(updated_row)
        # The CAS matched nothing: either the row does not belong to the caller / is
        # missing (→ None → 404), or it was ALREADY read (→ return it unchanged so a
        # repeat mark-read is an idempotent 200, not a spurious 404). Disambiguate with
        # an owner-scoped read.
        existing = (
            await self._session.execute(
                select(NotificationRow)
                .where(NotificationRow.id == notification_id)
                .where(NotificationRow.user_id == user_id)
            )
        ).scalar_one_or_none()
        return _row_to_entity(existing) if existing is not None else None

    async def mark_all_read(self, user_id: UUID) -> int:
        # Bulk owner-scoped CAS over the unread, non-archived set. Writes only ``read_at``
        # (W8.5b.9); ``RETURNING id`` yields one row per row marked, so its length is the
        # affected count (0 → idempotent no-op) — typed, unlike ``Result.rowcount``.
        upd = (
            update(NotificationRow)
            .where(NotificationRow.user_id == user_id)
            .where(NotificationRow.read_at.is_(None))
            .where(NotificationRow.archived.is_(False))
            .values(read_at=func.now(), updated_at=func.now())
            .returning(NotificationRow.id)
        )
        marked = (await self._session.execute(upd)).scalars().all()
        return len(marked)

    # --- Email delivery surface (α9.5, ADR-0051) --------------------------------

    async def list_email_deliverable(
        self, *, now: datetime, limit: int
    ) -> list[NotificationEmailDelivery]:
        # Undelivered, not terminally failed, and past its backoff gate — oldest first (FIFO).
        # Reads the reserved ``_email`` counter directly (never via the sanitised entity), so the
        # worker sees ``attempts`` without that bookkeeping ever reaching the read model. The
        # ``delivered_email_at IS NULL`` scan is intentionally unindexed at beta scale (ADR-0051 §9).
        stmt = text(
            """
            SELECT id, user_id, title, body,
                   COALESCE((payload #>> '{_email,attempts}')::int, 0) AS attempts
            FROM notifications
            WHERE delivered_email_at IS NULL
              AND COALESCE(payload #>> '{_email,state}', 'pending') <> 'failed'
              AND COALESCE(
                    (payload #>> '{_email,next_attempt_at}')::timestamptz, created_at
                  ) <= :now
            ORDER BY created_at ASC, id ASC
            LIMIT :limit
            """
        )
        rows = (await self._session.execute(stmt, {"now": now, "limit": limit})).mappings().all()
        return [
            NotificationEmailDelivery(
                id=row["id"],
                user_id=row["user_id"],
                title=row["title"],
                body=row["body"],
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    async def mark_email_delivered(self, *, notification_id: UUID) -> None:
        # Send-then-stamp (ADR-0051 D1-C): success uses the existing ``delivered_email_at`` column,
        # so "delivered" is unambiguous and the row is never re-scanned. Idempotent by construction.
        await self._session.execute(
            update(NotificationRow)
            .where(NotificationRow.id == notification_id)
            .values(delivered_email_at=func.now(), updated_at=func.now())
        )

    async def record_email_delivery_failure(
        self,
        *,
        notification_id: UUID,
        terminal: bool,
        code: str,
        attempts: int,
        next_attempt_at: datetime | None,
    ) -> None:
        # Persist the caller-computed failure state into the reserved ``_email`` namespace via JSONB
        # concatenation (``payload || {...}``) — ``delivered_email_at`` is never touched, so a failed
        # send can never masquerade as delivered. The whole ``_email`` object is replaced each pass.
        email_state: dict[str, Any] = {
            "attempts": attempts,
            "state": "failed" if terminal else "pending",
            "last_error": code,
        }
        if next_attempt_at is not None:
            email_state["next_attempt_at"] = next_attempt_at.isoformat()
        if terminal:
            email_state["failed_at"] = datetime.now(UTC).isoformat()
        await self._session.execute(
            text(
                """
                UPDATE notifications
                SET payload = payload || jsonb_build_object('_email', CAST(:email AS jsonb)),
                    updated_at = now()
                WHERE id = :id
                """
            ),
            {"id": notification_id, "email": json.dumps(email_state)},
        )


def _row_to_entity(row: NotificationRow) -> NotificationEntity:
    return NotificationEntity(
        id=row.id,
        user_id=row.user_id,
        kind=row.kind,
        title=row.title,
        body=row.body,
        # Centralised sanitisation (α9.5, ADR-0051): reserved ``_``-prefixed bookkeeping keys never
        # cross this boundary, so no read path / endpoint can expose internal email delivery state.
        payload=_public_payload(dict(row.payload)),
        source_event_id=row.source_event_id,
        delivered_in_app_at=row.delivered_in_app_at,
        read_at=row.read_at,
        archived=row.archived,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _extract_constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the failed constraint name from psycopg.

    Mirrors the helper in ``media_repository.py`` / ``project_repository.py``.
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return str(name) if name else None
