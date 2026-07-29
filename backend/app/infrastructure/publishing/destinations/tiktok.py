"""``TikTokDestination`` — the second production destination adapter (α9.6).

Uploads a finished export-delivery artifact to TikTok via the **Content Posting API
"Direct Post"** flow (``FILE_UPLOAD``) and returns the publish identity. Like
:class:`~app.infrastructure.publishing.destinations.youtube.YouTubeDestination` it is a
**credential-blind leaf** (PUB-5 / ADR-0047 C4): it receives a ready-to-use
:class:`~app.application.interfaces.social_credential_store.AuthorizedContext` bearer and
never touches the credential store, tokens, refresh, or key material.

**The frozen publish contract is preserved.** TikTok publishes *asynchronously*, but that is
adapted **entirely within this adapter** — no use case, worker, scheduler, or API gains any
TikTok-specific logic. ``publish()`` remains one synchronous call returning a
:class:`PublishResult` or raising :class:`DestinationError`.

Flow (grounding §4.1): ``creator_info/query`` → ``video/init`` → chunked ``PUT`` →
``status/fetch`` polling.

**Exactly three outcomes are possible** (α9.6 ruling 2):

1. **Success** — a terminal ``PUBLISH_COMPLETE`` observed inside the polling budget.
2. **Permanent failure** — a terminal ``FAILED`` (or a definitive pre-upload rejection)
   returned before the budget expires.
3. **Indeterminate timeout** — the budget expires with no terminal state. Treated as
   ``ambiguous_upload_outcome``: **no automatic retry, no second upload attempt, no
   duplicate-risk recovery**. Diagnostic detail (``publish_id``, last status, uploaded bytes,
   elapsed, poll count) is logged for future reconciliation. This preserves **PUB-11 — never
   risk duplicate publication**.

Two deliberate rulings are encoded here and must not be "fixed" without revisiting α9.6:

* **``fail_reason="internal"`` is PERMANENT.** TikTok documents it as retryable, but it can
  only surface *after* our bytes were accepted. Our architectural invariant against duplicate
  external publications outranks a provider's retry recommendation (see ``_fail_reason_error``).
* **The identifier is stable.** ``external_post_id`` is always the ``publish_id`` minted at
  init and is **never** overwritten by a later public post id (see :meth:`_succeed`).
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.application.interfaces.destination_publisher import (
    DestinationError,
    IDestinationPublisher,
    PublishResult,
    UploadMedia,
)
from app.application.interfaces.social_credential_store import AuthorizedContext
from app.domain.publishing.content_package import ContentPackage, Visibility

_LOGGER = structlog.get_logger(__name__)

_PLATFORM = "tiktok"

# Caption limit is expressed by TikTok in UTF-16 runes, not Python characters.
_MAX_CAPTION_UTF16 = 2200

# TikTok chunking rules (media transfer guide): 5MB..64MB per chunk, final chunk may carry the
# trailing bytes up to 128MB, at most 1000 chunks, and a <5MB file must be sent whole.
_MIN_CHUNK_BYTES = 5 * 1024 * 1024
_MAX_CHUNK_BYTES = 64 * 1024 * 1024
_MAX_CHUNKS = 1000

# α9.6 ruling 4 — deterministic visibility negotiation. TikTok requires privacy_level to be one
# of the creator's own privacy_level_options; UNLISTED has no TikTok equivalent. We NEVER
# silently downgrade a creator's requested visibility — an unsupported request fails loudly.
_VISIBILITY_TO_PRIVACY: dict[Visibility, str] = {
    Visibility.PUBLIC: "PUBLIC_TO_EVERYONE",
    Visibility.PRIVATE: "SELF_ONLY",
}

# Definitive rejections at creator-query / init time. No bytes have been sent, so these are
# unambiguous: permanent, mapped onto a stable neutral code.
_PERMANENT_INIT_CODES: dict[str, str] = {
    "access_token_invalid": "unauthorized",
    "scope_not_authorized": "unauthorized",
    "unaudited_client_can_only_post_to_private_accounts": "unaudited_client",
    "spam_risk_too_many_posts": "spam_risk",
    "spam_risk_user_banned_from_posting": "spam_risk",
    "privacy_level_option_mismatch": "invalid_metadata",
    "url_ownership_unverified": "invalid_metadata",
    "invalid_param": "invalid_metadata",
}

# Pre-upload conditions that may clear on their own. Safe to retry: nothing was transmitted.
_RETRYABLE_INIT_CODES: dict[str, str] = {
    "reached_active_user_cap": "quota_exceeded",
    "rate_limit_exceeded": "tiktok_transient",
    "internal_error": "tiktok_transient",
}

# Terminal poll outcomes. EVERY entry is permanent because polling only happens after bytes were
# accepted — see the module docstring on `internal`.
_FAIL_REASON_CODES: dict[str, str] = {
    "file_format_check_failed": "invalid_media",
    "duration_check_failed": "invalid_media",
    "frame_rate_check_failed": "invalid_media",
    "picture_size_check_failed": "invalid_media",
    "video_pull_failed": "invalid_media",
    "photo_pull_failed": "invalid_media",
    "publish_cancelled": "publish_cancelled",
    "auth_removed": "auth_removed",
    "spam_risk_too_many_posts": "spam_risk",
    "spam_risk_user_banned_from_posting": "spam_risk",
    "spam_risk_text": "spam_risk",
    "spam_risk": "spam_risk",
}

_STATUS_COMPLETE = "PUBLISH_COMPLETE"
_STATUS_FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class _ChunkPlan:
    """Resolved TikTok chunking parameters for one artifact."""

    chunk_size: int
    total_chunks: int
    size_bytes: int


class TikTokDestination(IDestinationPublisher):
    """Publish one finished video to TikTok via the Content Posting API (Direct Post)."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_base_url: str,
        chunk_size_bytes: int,
        status_poll_interval_seconds: float,
        status_poll_budget_seconds: float,
    ) -> None:
        self._http = http
        self._api_base_url = api_base_url.rstrip("/")
        self._chunk_size_bytes = chunk_size_bytes
        self._poll_interval = status_poll_interval_seconds
        self._poll_budget = status_poll_budget_seconds

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

        # α9.6 ruling 5 — scheduling is rejected, never silently ignored. TikTok's Direct Post
        # has no scheduled-publish field, so honouring `publish_at` is impossible; publishing
        # immediately would silently violate the creator's stated intent.
        if package.publish_at is not None:
            raise DestinationError(
                "tiktok does not support scheduled publishing (publish_at)",
                retryable=False,
                code="invalid_metadata",
            )
        # α9.3/ADR-0050 — thumbnails are optional + best-effort. TikTok covers are chosen by
        # frame timestamp, not an uploaded image, so a supplied thumbnail is accepted and
        # ignored (never fatal), exactly as MockDestination does.
        if media.thumbnail is not None:
            _LOGGER.info("publish.thumbnail_unsupported", platform=_PLATFORM)

        caption = self._build_caption(package)
        privacy_level = await self._negotiate_privacy(package.visibility, auth.access_token)
        plan = self._plan_chunks(media.size_bytes)
        publish_id, upload_url = await self._initiate(
            access_token=auth.access_token,
            caption=caption,
            privacy_level=privacy_level,
            media=media,
            plan=plan,
        )
        await self._transmit(upload_url=upload_url, media=media, plan=plan, publish_id=publish_id)
        return await self._await_completion(publish_id=publish_id, access_token=auth.access_token)

    # ---- metadata ---------------------------------------------------------------

    def _build_caption(self, package: ContentPackage) -> str:
        """Compose title + inline hashtags into TikTok's single caption field.

        TikTok has no separate tag field — hashtags are matched inline in the caption — and no
        separate description field, so ``ContentPackage.description`` is intentionally unused.
        """
        title = package.title.strip()
        if not title:
            raise DestinationError("title is empty", retryable=False, code="invalid_metadata")
        hashtags = [f"#{tag.lstrip('#').strip()}" for tag in package.tags if tag.strip()]
        caption = " ".join([title, *hashtags]) if hashtags else title
        if _utf16_length(caption) > _MAX_CAPTION_UTF16:
            raise DestinationError(
                f"caption exceeds {_MAX_CAPTION_UTF16} UTF-16 runes",
                retryable=False,
                code="invalid_metadata",
            )
        return caption

    async def _negotiate_privacy(self, visibility: Visibility, access_token: str) -> str:
        """Resolve ``visibility`` against the creator's own allowed options (deterministic).

        TikTok rejects any ``privacy_level`` not offered by ``creator_info/query`` for this
        creator, so the mapping must be negotiated rather than assumed. The result is
        deterministic: a single preferred value per visibility, and a loud permanent failure
        when it is unavailable. We never substitute a different visibility.
        """
        preferred = _VISIBILITY_TO_PRIVACY.get(visibility)
        if preferred is None:
            raise DestinationError(
                f"tiktok has no equivalent for visibility {visibility.value!r}",
                retryable=False,
                code="invalid_metadata",
            )
        body = await self._post(
            "/v2/post/publish/creator_info/query/", access_token=access_token, phase="creator_info"
        )
        data = body.get("data") if isinstance(body, dict) else None
        options = data.get("privacy_level_options") if isinstance(data, dict) else None
        if not isinstance(options, list) or not options:
            # Pre-upload protocol anomaly — nothing sent yet, so a retry is safe.
            raise DestinationError(
                "creator info query returned no privacy_level_options",
                retryable=True,
                code="tiktok_transient",
            )
        if preferred not in options:
            raise DestinationError(
                f"creator does not allow privacy level {preferred!r} "
                f"(offered: {', '.join(str(o) for o in options)})",
                retryable=False,
                code="visibility_unavailable",
            )
        return preferred

    # ---- chunk planning ---------------------------------------------------------

    def _plan_chunks(self, size_bytes: int) -> _ChunkPlan:
        """Compute TikTok's chunk_size / total_chunk_count (TikTok specifies FLOOR division)."""
        if size_bytes < _MIN_CHUNK_BYTES:
            # A sub-5MB file must be uploaded whole.
            return _ChunkPlan(chunk_size=size_bytes, total_chunks=1, size_bytes=size_bytes)
        # Widen the chunk before giving up, so a large artifact stays within the 1000-chunk cap
        # rather than failing on a configuration default.
        minimum_for_cap = math.ceil(size_bytes / _MAX_CHUNKS)
        chunk_size = max(_MIN_CHUNK_BYTES, self._chunk_size_bytes, minimum_for_cap)
        chunk_size = min(chunk_size, _MAX_CHUNK_BYTES)
        total_chunks = size_bytes // chunk_size
        if total_chunks > _MAX_CHUNKS:
            raise DestinationError(
                f"media requires {total_chunks} chunks, exceeding TikTok's {_MAX_CHUNKS} limit",
                retryable=False,
                code="invalid_metadata",
            )
        return _ChunkPlan(chunk_size=chunk_size, total_chunks=total_chunks, size_bytes=size_bytes)

    # ---- phases -----------------------------------------------------------------

    async def _initiate(
        self,
        *,
        access_token: str,
        caption: str,
        privacy_level: str,
        media: UploadMedia,
        plan: _ChunkPlan,
    ) -> tuple[str, str]:
        """Open the publish + upload session. Pre-upload failures are safely retryable."""
        body = await self._post(
            "/v2/post/publish/video/init/",
            access_token=access_token,
            phase="init",
            json={
                "post_info": {"title": caption, "privacy_level": privacy_level},
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": plan.size_bytes,
                    "chunk_size": plan.chunk_size,
                    "total_chunk_count": plan.total_chunks,
                },
            },
        )
        data = body.get("data") if isinstance(body, dict) else None
        publish_id = str(data.get("publish_id", "")).strip() if isinstance(data, dict) else ""
        upload_url = str(data.get("upload_url", "")).strip() if isinstance(data, dict) else ""
        if not publish_id or not upload_url:
            raise DestinationError(
                "init returned no publish_id/upload_url",
                retryable=True,
                code="tiktok_transient",
            )
        return publish_id, upload_url

    async def _transmit(
        self, *, upload_url: str, media: UploadMedia, plan: _ChunkPlan, publish_id: str
    ) -> None:
        """Stream the artifact in sequential chunks. Past the first byte, ambiguity ⇒ permanent."""
        path = Path(media.path)
        for index in range(plan.total_chunks):
            first = index * plan.chunk_size
            is_final = index == plan.total_chunks - 1
            # The final chunk absorbs the trailing bytes left by TikTok's floor division.
            length = plan.size_bytes - first if is_final else plan.chunk_size
            last = first + length - 1
            try:
                data = await asyncio.to_thread(_read_chunk, path, first, length)
            except OSError as exc:
                # Local I/O failure. Before the first chunk nothing was sent (safe to retry);
                # afterwards the platform already holds bytes, so PUB-11 applies.
                if index == 0:
                    raise DestinationError(
                        f"could not read media artifact: {exc}",
                        retryable=True,
                        code="tiktok_transient",
                    ) from exc
                raise DestinationError(
                    f"ambiguous upload outcome: media read failed mid-transmission: {exc}",
                    retryable=False,
                    code="ambiguous_upload_outcome",
                ) from exc

            try:
                response = await self._http.put(
                    upload_url,
                    headers={
                        "Content-Type": media.mime_type,
                        "Content-Length": str(length),
                        "Content-Range": f"bytes {first}-{last}/{plan.size_bytes}",
                    },
                    content=data,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if index == 0:
                    # The connection never established for the very first chunk — nothing was
                    # accepted, so a retry cannot duplicate anything.
                    raise DestinationError(
                        f"upload connection error before transmission: {exc}",
                        retryable=True,
                        code="tiktok_transient",
                    ) from exc
                raise DestinationError(
                    f"ambiguous upload outcome after transmission: {exc}",
                    retryable=False,
                    code="ambiguous_upload_outcome",
                ) from exc
            except httpx.HTTPError as exc:
                # Any other transport failure happens after bytes began flowing (PUB-11).
                raise DestinationError(
                    f"ambiguous upload outcome after transmission: {exc}",
                    retryable=False,
                    code="ambiguous_upload_outcome",
                ) from exc

            expected = (
                (httpx.codes.CREATED, httpx.codes.OK)
                if is_final
                else (httpx.codes.PARTIAL_CONTENT,)
            )
            if response.status_code not in expected:
                raise DestinationError(
                    f"ambiguous upload outcome "
                    f"(HTTP {response.status_code} on chunk {index + 1}/{plan.total_chunks})",
                    retryable=False,
                    code="ambiguous_upload_outcome",
                )
        _LOGGER.info(
            "publish.upload_transmitted",
            platform=_PLATFORM,
            publish_id=publish_id,
            chunks=plan.total_chunks,
            size_bytes=plan.size_bytes,
        )

    async def _await_completion(self, *, publish_id: str, access_token: str) -> PublishResult:
        """Poll until terminal or the budget expires — the three-outcome decision point."""
        started = time.monotonic()
        polls = 0
        last_status = "UNKNOWN"
        uploaded_bytes: Any = None
        while True:
            # PUB-11: we are PAST transmission, so a transient status-poll failure must never
            # escape as retryable — that would re-run the whole upload and risk a duplicate post.
            # Instead we absorb it and keep polling until the budget decides the outcome.
            try:
                body = await self._post(
                    "/v2/post/publish/status/fetch/",
                    access_token=access_token,
                    phase="status",
                    json={"publish_id": publish_id},
                )
            except DestinationError as exc:
                if not exc.retryable:
                    raise
                _LOGGER.info(
                    "publish.status_poll_failed",
                    platform=_PLATFORM,
                    publish_id=publish_id,
                    code=exc.code,
                )
                body = {}
            polls += 1
            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, dict):
                last_status = str(data.get("status") or last_status)
                uploaded_bytes = data.get("uploaded_bytes", uploaded_bytes)
                if last_status == _STATUS_COMPLETE:
                    return self._succeed(publish_id=publish_id, data=data)
                if last_status == _STATUS_FAILED:
                    raise self._fail_reason_error(publish_id, data)

            if time.monotonic() - started >= self._poll_budget:
                # OUTCOME 3 — INDETERMINATE TIMEOUT. The bytes were accepted and TikTok may yet
                # publish, so we must not retry, must not re-upload, and must not attempt any
                # duplicate-risk recovery (PUB-11). We record enough to reconcile later by hand.
                _LOGGER.warning(
                    "publish.indeterminate_timeout",
                    platform=_PLATFORM,
                    publish_id=publish_id,
                    last_status=last_status,
                    uploaded_bytes=uploaded_bytes,
                    polls=polls,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    budget_seconds=self._poll_budget,
                )
                raise DestinationError(
                    f"ambiguous upload outcome: publish {publish_id} still {last_status} after "
                    f"{polls} polls / {self._poll_budget}s budget",
                    retryable=False,
                    code="ambiguous_upload_outcome",
                )
            await asyncio.sleep(self._poll_interval)

    def _succeed(self, *, publish_id: str, data: dict[str, Any]) -> PublishResult:
        """Build the success result. **The identifier is the publish_id — always.**

        α9.6 ruling 3: ``publish_id`` is minted at init, is durable, and is available for every
        post regardless of visibility or moderation. A later public post id must therefore
        **never** replace it — a stable identifier keeps logs, audits and any future
        reconciliation coherent. TikTok only ever exposes ``publicaly_available_post_id`` for
        public, moderation-approved posts, so it is not a dependable identity.

        The public post id is captured as **diagnostic metadata only**. It is deliberately not
        persisted: the sole spare nullable field is ``platform_post_url``, and TikTok's canonical
        URL requires the creator's username (``/@{username}/video/{id}``), which a
        credential-blind adapter never sees — so no valid URL can be derived and none is invented.
        """
        post_ids = data.get("publicaly_available_post_id")
        if isinstance(post_ids, list) and post_ids:
            _LOGGER.info(
                "publish.public_post_id_observed",
                platform=_PLATFORM,
                publish_id=publish_id,
                public_post_id=str(post_ids[0]),
            )
        return PublishResult(external_post_id=publish_id, post_url=None)

    @staticmethod
    def _fail_reason_error(publish_id: str, data: dict[str, Any]) -> DestinationError:
        """Map a terminal ``FAILED`` onto a permanent :class:`DestinationError`.

        **Every** terminal failure here is permanent, including ``fail_reason="internal"``, which
        TikTok documents as retryable. Polling only occurs once our bytes have been accepted, so
        a retry would re-upload and risk a second public post. Our invariant against duplicate
        external publications (PUB-11) takes precedence over the provider's recommendation.
        """
        reason = str(data.get("fail_reason") or "unknown")
        code = _FAIL_REASON_CODES.get(reason)
        if code is None:
            # Unknown or `internal` — the post may or may not exist. Never retry (PUB-11).
            code = "ambiguous_upload_outcome"
        return DestinationError(
            f"tiktok publish {publish_id} failed: {reason}",
            retryable=False,
            code=code,
        )

    # ---- transport --------------------------------------------------------------

    async def _post(
        self,
        path: str,
        *,
        access_token: str,
        phase: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST a TikTok JSON endpoint and normalise its error envelope.

        Used only for the *pre-upload* (creator info, init) and *post-upload* (status) phases —
        never for byte transmission — so transport failures here are classified as retryable.
        The status phase is exempt: its caller owns the budget and PUB-11 handling.
        """
        url = f"{self._api_base_url}{path}"
        try:
            response = await self._http.post(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json=json if json is not None else {},
            )
        except httpx.HTTPError as exc:
            raise DestinationError(
                f"{phase} transport error: {exc}", retryable=True, code="tiktok_transient"
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise DestinationError(
                f"{phase} returned a non-JSON body",
                retryable=True,
                code="tiktok_transient",
            ) from exc
        if not isinstance(body, dict):
            raise DestinationError(
                f"{phase} returned an unexpected body", retryable=True, code="tiktok_transient"
            )

        error = body.get("error")
        error_code = str(error.get("code")) if isinstance(error, dict) else None
        if error_code and error_code != "ok":
            raise _error_for_code(error_code, phase=phase)
        if response.status_code != httpx.codes.OK:
            raise DestinationError(
                f"{phase} returned HTTP {response.status_code}",
                retryable=True,
                code="tiktok_transient",
            )
        return body


def _error_for_code(error_code: str, *, phase: str) -> DestinationError:
    """Translate a TikTok ``error.code`` into a neutral :class:`DestinationError`."""
    permanent = _PERMANENT_INIT_CODES.get(error_code)
    if permanent is not None:
        return DestinationError(
            f"tiktok rejected the {phase} request: {error_code}",
            retryable=False,
            code=permanent,
        )
    retryable = _RETRYABLE_INIT_CODES.get(error_code)
    if retryable is not None:
        return DestinationError(
            f"tiktok {phase} temporarily unavailable: {error_code}",
            retryable=True,
            code=retryable,
        )
    return DestinationError(
        f"tiktok {phase} returned error {error_code}",
        retryable=True,
        code="tiktok_transient",
    )


def _read_chunk(path: Path, offset: int, length: int) -> bytes:
    """Read one chunk off disk without materialising the whole artifact in memory."""
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(length)


def _utf16_length(text: str) -> int:
    """TikTok expresses the caption limit in UTF-16 runes, not Python characters."""
    return len(text.encode("utf-16-le")) // 2


__all__ = ["TikTokDestination"]
