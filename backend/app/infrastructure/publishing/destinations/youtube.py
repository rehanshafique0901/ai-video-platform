"""``YouTubeDestination`` — the first production-quality destination adapter (α8.6c).

The upload half of the first real destination (grounding §2). It uploads a finished
export-delivery artifact to YouTube via the **Data API v3 ``videos.insert`` resumable
upload** and returns the created video's identity. It is a **credential-blind leaf**
(PUB-5 / ADR-0047 C4): it receives a ready-to-use
:class:`~app.application.interfaces.social_credential_store.AuthorizedContext` bearer and
never touches the credential store, tokens, refresh, or key material — the import-linter
"destination adapters are credential-blind leaves" contract enforces this mechanically.

Design (pre-flight EQ2/EQ3):

* **Thin httpx.** All I/O goes through an injected :class:`httpx.AsyncClient` (a
  ``MockTransport`` in tests); no Google SDK.
* **Adapter owns classification, not retry.** Google outcomes are translated into a single
  :class:`DestinationError` with a ``retryable`` flag; the runtime owns backoff/attempts and
  never inspects provider codes.
* **PUB-11 — correctness over retry.** Once the upload ``PUT`` has begun and the outcome is
  **ambiguous** (a non-connect transport error, a 5xx/429 after bytes, or a success body we
  cannot parse into a video id), the adapter fails **permanently**
  (``code="ambiguous_upload_outcome"``) rather than risk a duplicate public post. Only
  pre-upload / clearly-transient-before-acceptance failures are retryable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.application.interfaces.destination_publisher import (
    DestinationError,
    IDestinationPublisher,
    PublishResult,
    UploadMedia,
)
from app.application.interfaces.social_credential_store import AuthorizedContext
from app.domain.publishing.content_package import ContentPackage

_PLATFORM = "youtube"
_WATCH_URL = "https://www.youtube.com/watch?v="
_MAX_TITLE = 100
_MAX_DESCRIPTION = 5000
_MAX_TAGS_TOTAL_CHARS = 500


class YouTubeDestination(IDestinationPublisher):
    """Upload one finished video to YouTube via the resumable ``videos.insert`` API."""

    def __init__(self, *, http: httpx.AsyncClient, api_base_url: str) -> None:
        self._http = http
        self._api_base_url = api_base_url.rstrip("/")

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
        # Credential-blind sanity: the runtime must supply a usable bearer + real bytes.
        if not auth.access_token:
            raise DestinationError(
                "authorized context carried no access token",
                retryable=False,
                code="missing_bearer",
            )
        if media.size_bytes <= 0:
            raise DestinationError("media artifact is empty", retryable=False, code="empty_media")

        body = self._build_request_body(package)  # validates platform limits (permanent on breach)
        session_url = await self._initiate(auth.access_token, body, media)
        return await self._transmit(session_url, media)

    def _build_request_body(self, package: ContentPackage) -> dict[str, object]:
        """Map the platform-agnostic ``ContentPackage`` → a ``videos.insert`` body (§4)."""
        title = package.title.strip()
        if not title:
            raise DestinationError("title is empty", retryable=False, code="invalid_metadata")
        if len(title) > _MAX_TITLE:
            raise DestinationError(
                f"title exceeds {_MAX_TITLE} characters", retryable=False, code="invalid_metadata"
            )
        if len(package.description) > _MAX_DESCRIPTION:
            raise DestinationError(
                f"description exceeds {_MAX_DESCRIPTION} characters",
                retryable=False,
                code="invalid_metadata",
            )
        if sum(len(t) for t in package.tags) > _MAX_TAGS_TOTAL_CHARS:
            raise DestinationError(
                f"tags exceed {_MAX_TAGS_TOTAL_CHARS} total characters",
                retryable=False,
                code="invalid_metadata",
            )

        snippet: dict[str, object] = {"title": title, "description": package.description}
        if package.tags:
            snippet["tags"] = list(package.tags)

        # publish_at requires a private video (YouTube schedules the transition to public);
        # otherwise the requested visibility maps 1:1 (public/unlisted/private).
        status: dict[str, object] = {"selfDeclaredMadeForKids": False}
        if package.publish_at is not None:
            status["privacyStatus"] = "private"
            status["publishAt"] = package.publish_at.isoformat()
        else:
            status["privacyStatus"] = package.visibility.value

        return {"snippet": snippet, "status": status}

    async def _initiate(
        self, access_token: str, body: dict[str, object], media: UploadMedia
    ) -> str:
        """Open a resumable upload session; return the session URL. Pre-upload = retryable."""
        url = f"{self._api_base_url}/upload/youtube/v3/videos"
        try:
            response = await self._http.post(
                url,
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": media.mime_type,
                    "X-Upload-Content-Length": str(media.size_bytes),
                },
                json=body,
            )
        except httpx.HTTPError as exc:
            # No bytes have been sent yet — safe to retry.
            raise DestinationError(
                f"resumable session initiation transport error: {exc}",
                retryable=True,
                code="youtube_transient",
            ) from exc

        self._raise_for_permanent_status(response.status_code, phase="initiate")
        if response.status_code not in (httpx.codes.OK, httpx.codes.CREATED):
            # 429 / 5xx / anything else pre-upload — no bytes sent, safe to retry.
            raise DestinationError(
                f"resumable session initiation returned HTTP {response.status_code}",
                retryable=True,
                code="youtube_transient",
            )
        session_url = response.headers.get("location")
        if not session_url:
            # Protocol anomaly, but still pre-upload — safe to retry.
            raise DestinationError(
                "resumable session initiation returned no upload URL",
                retryable=True,
                code="youtube_transient",
            )
        return str(session_url)

    async def _transmit(self, session_url: str, media: UploadMedia) -> PublishResult:
        """Stream bytes to the session URL. Past this point, ambiguity ⇒ permanent (PUB-11)."""
        data = await asyncio.to_thread(Path(media.path).read_bytes)
        try:
            response = await self._http.put(
                session_url,
                headers={
                    "Content-Type": media.mime_type,
                    "Content-Length": str(media.size_bytes),
                },
                content=data,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The connection never established — no bytes were accepted, so a retry is safe.
            raise DestinationError(
                f"upload connection error before transmission: {exc}",
                retryable=True,
                code="youtube_transient",
            ) from exc
        except httpx.HTTPError as exc:
            # Any other transport failure (read timeout, write error, protocol error) happens
            # after we began sending bytes — the platform may have accepted them (PUB-11).
            raise DestinationError(
                f"ambiguous upload outcome after transmission: {exc}",
                retryable=False,
                code="ambiguous_upload_outcome",
            ) from exc

        self._raise_for_permanent_status(response.status_code, phase="upload")
        if response.status_code not in (httpx.codes.OK, httpx.codes.CREATED):
            # 429 / 5xx / unknown after bytes were sent — cannot rule out acceptance (PUB-11).
            raise DestinationError(
                f"ambiguous upload outcome (HTTP {response.status_code} after transmission)",
                retryable=False,
                code="ambiguous_upload_outcome",
            )
        return self._parse_result(response)

    @staticmethod
    def _raise_for_permanent_status(status_code: int, *, phase: str) -> None:
        """Definitive client-side rejections are permanent in either phase (no double-post risk)."""
        if status_code == httpx.codes.UNAUTHORIZED:
            raise DestinationError(
                f"youtube rejected the credential during {phase} (HTTP 401)",
                retryable=False,
                code="unauthorized",
            )
        if status_code == httpx.codes.FORBIDDEN:
            raise DestinationError(
                f"youtube forbade the {phase} (HTTP 403)",
                retryable=False,
                code="forbidden",
            )
        if status_code == httpx.codes.BAD_REQUEST:
            raise DestinationError(
                f"youtube rejected the request metadata during {phase} (HTTP 400)",
                retryable=False,
                code="invalid_metadata",
            )

    @staticmethod
    def _parse_result(response: httpx.Response) -> PublishResult:
        """A 2xx body must yield a video id; an unparseable success is ambiguous (PUB-11)."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise DestinationError(
                "upload succeeded but the response body was not JSON",
                retryable=False,
                code="ambiguous_upload_outcome",
            ) from exc
        video_id = payload.get("id") if isinstance(payload, dict) else None
        if not video_id:
            raise DestinationError(
                "upload succeeded but the response carried no video id",
                retryable=False,
                code="ambiguous_upload_outcome",
            )
        video_id = str(video_id)
        return PublishResult(external_post_id=video_id, post_url=f"{_WATCH_URL}{video_id}")


__all__ = ["YouTubeDestination"]
