"""``MockDestination`` — a deterministic, network-free destination adapter (α8.6b).

The first :class:`app.application.interfaces.destination_publisher.IDestinationPublisher`
implementation. It proves the publish runtime end-to-end (create → claim → authorize →
upload → settle → events) without any external platform, and is the CI default for Stage 14.

* **Deterministic (PUB-9-friendly).** The returned ``external_post_id`` / ``post_url`` are a
  pure function of the source media asset id, so tests can assert exact values.
* **Credential-blind (PUB-5).** It receives an ``AuthorizedContext`` and requires a
  non-empty bearer (a missing token is a *permanent* failure — the runtime should never have
  called it), but it neither stores nor logs the token (C8).
* **Leaf.** Imports only the destination port + domain ``ContentPackage``; it never reaches
  into the credential store, repositories, or other bounded contexts.
"""

from __future__ import annotations

from app.application.interfaces.destination_publisher import (
    DestinationError,
    IDestinationPublisher,
    PublishResult,
    UploadMedia,
)
from app.application.interfaces.social_credential_store import AuthorizedContext
from app.domain.publishing.content_package import ContentPackage

_PLATFORM = "mock"
_POST_BASE_URL = "https://mock.publish.local/watch"


class MockDestination(IDestinationPublisher):
    """A network-free destination that echoes a deterministic post identity."""

    @property
    def platform(self) -> str:
        return _PLATFORM

    async def publish(
        self,
        *,
        package: ContentPackage,
        auth: AuthorizedContext,
        media: UploadMedia,
    ) -> PublishResult:
        # Credential-blind sanity: the runtime must supply a usable bearer. An empty token is
        # a caller bug, not a transient platform error → permanent (never retried).
        if not auth.access_token:
            raise DestinationError(
                "authorized context carried no access token",
                retryable=False,
                code="missing_bearer",
            )
        # A zero-byte artifact is not uploadable — permanent.
        if media.size_bytes <= 0:
            raise DestinationError("media artifact is empty", retryable=False, code="empty_media")
        external_post_id = f"mock-post-{package.media_asset_id.hex[:12]}"
        return PublishResult(
            external_post_id=external_post_id,
            post_url=f"{_POST_BASE_URL}?v={external_post_id}",
        )


__all__ = ["MockDestination"]
