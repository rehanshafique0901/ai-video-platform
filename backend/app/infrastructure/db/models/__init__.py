"""ORM model registry.

Importing this module registers every table on the shared ``metadata`` object.
Order is intentional: tables with no FK dependencies come first so that
Alembic's autogenerate sort is deterministic in case future contributors regenerate.
"""

from app.infrastructure.db.models import (  # noqa: F401
    agent_memory,
    ai_models,
    analytics,
    audit,
    billing,
    configuration,
    events,
    feature_flags,
    identity,
    identity_runtime,
    jobs,
    media,
    notifications,
    operations,
    projects,
    publishing,
    scenes,
    sentinel,
    templates,
    timeline,
    usage,
    webhooks,
    workflows,
)
