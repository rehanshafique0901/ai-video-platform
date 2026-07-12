"""Media use cases (Slice α6.2).

`RegisterMedia`, `ListMedia`, `GetMedia`, `UpdateMedia`, `DeleteMedia` — the
register-by-metadata CRUD surface for the Media aggregate.

**Owner gate, not project gate.** Media assets carry their own ``tenant_id`` +
``owner_user_id``; access is resolved directly against the row (α6.2 Q1/Q4),
unlike prompts/scenes which gate through a project. ``project_id`` / ``scene_id``
/ ``prompt_id`` are *optional links* validated on register/update (a present
link must be owned/live → else ``422``), never the access key.

Per ADR-0037 (which adopts ADR-0036), media is a generation **output**, not
versioned editorial content: no OCC, no ``projects.version`` bump, excluded from
snapshots/restore/diff. α6.2 registers metadata only — no provider or
object-storage calls.
"""

from app.application.use_cases.media.delete_media import DeleteMedia
from app.application.use_cases.media.get_media import GetMedia
from app.application.use_cases.media.list_media import ListMedia
from app.application.use_cases.media.register_media import RegisterMedia
from app.application.use_cases.media.update_media import UpdateMedia

__all__ = ["RegisterMedia", "ListMedia", "GetMedia", "UpdateMedia", "DeleteMedia"]
