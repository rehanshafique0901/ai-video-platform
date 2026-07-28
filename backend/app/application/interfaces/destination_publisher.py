"""Port: ``IDestinationPublisher`` — upload one finished video to one destination (α8.6b).

The **destination boundary** (contract §8, PUB-4/PUB-5). A destination is *not* an AI
provider: it neither generates nor transforms content — it uploads an already-finished
artifact and returns the platform's post identity. α8.6b ships the ``mock`` adapter; the
real YouTube adapter is α8.6c (no port change).

**Credential-blind (PUB-5, ADR-0047 C4).** :meth:`publish` receives a short-lived
:class:`~app.application.interfaces.social_credential_store.AuthorizedContext` (a ready-to-use
bearer) — never the credential store, a ``SocialAccount``, or key material. The adapter can
call the platform API but can never read, refresh, persist, or leak a stored credential.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.application.interfaces.social_credential_store import AuthorizedContext
from app.domain.publishing.content_package import ContentPackage


class DestinationError(Exception):
    """A destination upload failed. ``retryable`` drives the worker's retry decision (DQ6).

    Adapters classify their own failures (contract §6 / §8): a transient platform error
    (5xx, rate limit, network) is ``retryable=True`` (the worker reschedules while attempts
    remain); a permanent one (invalid metadata, rejected content, auth refused) is
    ``retryable=False`` (the worker fails the job immediately). ``code`` is a neutral, safe
    token for the event/log — never platform credentials or internals (PUB-8 / C8).
    """

    def __init__(self, message: str, *, retryable: bool, code: str = "destination_error") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


@dataclass(frozen=True, slots=True)
class UploadThumbnail:
    """A materialised thumbnail image to set on the created post (α9.3, ADR-0050).

    Same shape as :class:`UploadMedia` — a local path + non-secret descriptors of an owned image
    ``MediaAsset`` the **worker** has already resolved (owner-scoped) and materialised to the temp
    workspace. The adapter never resolves it, never touches storage, and never generates it; it
    only uploads these bytes (best-effort) after the primary video upload succeeds.
    """

    path: str
    mime_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class UploadMedia:
    """The finished delivery artifact to upload — a local path + its non-secret descriptors.

    The worker materialises the export-delivery ``MediaAsset`` bytes to a temp workspace
    (outside any DB transaction, mirroring the export worker) and hands the adapter this
    handle. No provider URLs, checkpoints, or storage internals cross the boundary (PUB-1).

    ``thumbnail`` (α9.3, ADR-0050) is an **optional additive** field: when the creator supplied a
    thumbnail the worker materialises it too and attaches it here. Adapters that do not support
    thumbnails simply ignore it — the ``IDestinationPublisher.publish`` method signature is
    unchanged and existing adapters remain behaviourally identical (ADR-0050 §Boundary verification).
    """

    path: str
    mime_type: str
    size_bytes: int
    thumbnail: UploadThumbnail | None = None


@dataclass(frozen=True, slots=True)
class PublishResult:
    """The platform's identity for the created post (on success)."""

    external_post_id: str
    post_url: str | None


class IDestinationPublisher(ABC):
    """One destination's upload adapter (credential-blind, leaf)."""

    @property
    @abstractmethod
    def platform(self) -> str:
        """The free-text platform key this adapter serves (e.g. ``mock`` / ``youtube``)."""
        ...

    @abstractmethod
    async def publish(
        self,
        *,
        package: ContentPackage,
        auth: AuthorizedContext,
        media: UploadMedia,
    ) -> PublishResult:
        """Upload ``media`` described by ``package`` using the bearer in ``auth``.

        Returns the platform post identity on success. Raises :class:`DestinationError`
        (with a ``retryable`` classification) on failure. Must never touch the credential
        store, persist tokens, or emit credential material (PUB-5 / C4 / C8).
        """
        ...


class IDestinationRegistry(ABC):
    """Resolve a ``platform`` key to its :class:`IDestinationPublisher` (composition root).

    The use case depends on this port (never the concrete registry), so it stays free of
    infrastructure. An unknown platform is a **permanent** publish failure.
    """

    @abstractmethod
    def for_platform(self, platform: str) -> IDestinationPublisher:
        """Return the adapter for ``platform``, or raise a permanent :class:`DestinationError`."""
        ...

    @abstractmethod
    def supported_platforms(self) -> frozenset[str]:
        """The set of platform keys this registry can serve (create-time validation)."""
        ...


__all__ = [
    "DestinationError",
    "UploadThumbnail",
    "UploadMedia",
    "PublishResult",
    "IDestinationPublisher",
    "IDestinationRegistry",
]
