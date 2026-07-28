"""Media Library bounded context (Slice α9.2).

Frozen, ORM-free domain entities for the Asset Library that ADR-0037's Future
Extensions reserved (**CR-8** — ``library_assets`` with its own ``VersionMixin``,
folders, tags, usage counters, over registered ``media_assets``). The library is a
*sibling* built over the Media aggregate: it never mutates ``media_assets`` and
only references a registered asset by id.
"""

from app.domain.library.library_asset import LibraryAsset
from app.domain.library.library_folder import LibraryFolder

__all__ = ["LibraryAsset", "LibraryFolder"]
