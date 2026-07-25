"""Port: model manager — the persistent Model Cache for local execution.

Downloading model weights repeatedly is painful on a laptop and wasteful on any
machine. The Model Manager owns a download-once / verify-checksum / register /
reuse cache: given a model reference, it guarantees the weights are present
locally and returns a handle the local adapter runs against. Remote-tier
generation never touches this port. The concrete implementation manages a cache
directory; tests use a fake that records calls.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ModelUnavailableError(RuntimeError):
    """Raised when a model cannot be made available (download/checksum failure)."""


@dataclass(frozen=True, slots=True)
class LocalModel:
    """A model that is present locally and ready to run."""

    model_ref: str
    local_path: str
    revision: str | None = None
    from_cache: bool = False


class IModelManager(ABC):
    @abstractmethod
    async def ensure_available(self, model_ref: str) -> LocalModel:
        """Ensure ``model_ref`` is downloaded, verified and registered locally.

        Returns quickly with ``from_cache=True`` when already cached; otherwise
        downloads, verifies the checksum, registers, and returns the new handle.
        """
        ...
