"""Billing context — plans, subscriptions, invoices, credit ledger.

Schema reference: ``docs/database/schema.md`` §20–§23.
``credit_ledger`` is append-only and protected by an immutability trigger
emitted in the baseline migration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base
from app.infrastructure.db.enums import (
    billing_cycle_enum,
    invoice_status_enum,
    ledger_entry_type_enum,
    subscription_status_enum,
)
from app.infrastructure.db.mixins import (
    CreatedAtOnlyMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)


class Plan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    cycle: Mapped[str] = mapped_column(billing_cycle_enum, nullable=False)
    monthly_credits: Mapped[float] = mapped_column(
        Numeric(18, 4), nullable=False, server_default=text("0")
    )
    monthly_price: Mapped[float] = mapped_column(
        Numeric(18, 4), nullable=False, server_default=text("0")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'USD'"))
    features: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (UniqueConstraint("code", name="uq_plans_code"),)


class Subscription(UUIDPrimaryKeyMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "subscriptions"

    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    plan_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(subscription_status_enum, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renews_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_provider: Mapped[str] = mapped_column(Text, nullable=False)
    external_customer_id: Mapped[str | None] = mapped_column(Text)
    external_subscription_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index(
            "uq_subscriptions_tenant_id_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("status IN ('active','trialing','past_due')"),
        ),
        Index("ix_subscriptions_status_renews_at", "status", "renews_at"),
    )


class Invoice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "invoices"

    subscription_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    number: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(invoice_status_enum, nullable=False)
    amount_due: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    amount_paid: Mapped[float] = mapped_column(
        Numeric(18, 4), nullable=False, server_default=text("0")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_invoice_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("number", name="uq_invoices_number"),
        CheckConstraint("period_end > period_start", name="period_valid"),
        Index("ix_invoices_subscription_id_period_start", "subscription_id", "period_start"),
        Index("ix_invoices_status", "status"),
    )


class CreditLedger(CreatedAtOnlyMixin, Base):
    """Immutable. ``balance_after`` is enforced by trigger ``credit_ledger_balance``."""

    __tablename__ = "credit_ledger"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    entry_type: Mapped[str] = mapped_column(ledger_entry_type_enum, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    balance_after: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    related_invoice_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="SET NULL"),
    )
    related_usage_record_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
    )  # No FK: usage_records is partitioned; integrity enforced in service layer.
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("balance_after >= 0", name="balance_nonnegative"),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_credit_ledger_tenant_id_idempotency_key"
        ),
        Index("ix_credit_ledger_tenant_id_created_at", "tenant_id", "created_at"),
    )


__all__ = ["Plan", "Subscription", "Invoice", "CreditLedger"]
