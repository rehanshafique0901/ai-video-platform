"""Use-case layer — one class per API operation, one transaction per class.

Use cases orchestrate domain entities + repositories + security
primitives inside a single Unit of Work. They own transactional
boundaries; repositories do not. Domain entities own invariants; use
cases do not duplicate them.

Slice α2a: ``use_cases/auth/register_user`` + ``use_cases/auth/login_user``.

Import policy (enforced by import-linter contract "Application
use_cases never import infrastructure or api"): use-case modules may
import only from ``app.domain``, ``app.application.interfaces``, and
``app.core.errors``. Concrete SQLAlchemy repositories, JWT / Argon2,
and any FastAPI type are off-limits.
"""
