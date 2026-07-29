"""Unit tests for ``TikTokDestination`` (α9.6) — network-free via MockTransport.

Covers caption/privacy mapping, the chunked FILE_UPLOAD protocol, the bounded status-poll loop,
and the full error-classification table — including the two deliberate α9.6 rulings:

* the identifier is **always** the ``publish_id`` and is never replaced by a public post id;
* ``fail_reason="internal"`` is **permanent** even though TikTok documents it as retryable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.application.interfaces.destination_publisher import (
    DestinationError,
    UploadMedia,
    UploadThumbnail,
)
from app.application.interfaces.social_credential_store import AuthorizedContext
from app.domain.publishing.content_package import Visibility, build_content_package
from app.infrastructure.publishing.destinations.tiktok import TikTokDestination

_API = "https://open.tiktokapis.com"
_CREATOR_PATH = "/v2/post/publish/creator_info/query/"
_INIT_PATH = "/v2/post/publish/video/init/"
_STATUS_PATH = "/v2/post/publish/status/fetch/"
_UPLOAD_URL = "https://open-upload.tiktokapis.com/video/?upload_id=1&upload_token=t"
_PUBLISH_ID = "v_pub_file~v2-1.123456789"

_OK = {"code": "ok", "message": ""}


def _dest(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    chunk_size: int = 5 * 1024 * 1024,
    budget: float = 30.0,
) -> TikTokDestination:
    return TikTokDestination(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        api_base_url=_API,
        chunk_size_bytes=chunk_size,
        status_poll_interval_seconds=0.0,
        status_poll_budget_seconds=budget,
    )


def _auth(token: str = "act.bearer") -> AuthorizedContext:
    return AuthorizedContext(
        access_token=token, expires_at=datetime(2026, 7, 29, tzinfo=UTC), scopes=("video.publish",)
    )


def _media(tmp_path: Path, *, size: int = 2048) -> UploadMedia:
    artifact = tmp_path / "artifact.mp4"
    artifact.write_bytes(b"x" * size)
    return UploadMedia(path=str(artifact), mime_type="video/mp4", size_bytes=size)


def _pkg(**kwargs: object):
    return build_content_package(media_asset_id=uuid4(), project_title="My Project", **kwargs)


def _flow(
    *,
    options: list[str] | None = None,
    statuses: list[dict[str, object]] | None = None,
    put_responses: list[httpx.Response] | None = None,
    captured: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """A complete happy-ish TikTok flow whose phases can be individually overridden."""
    remaining_status = list(statuses if statuses is not None else [{"status": "PUBLISH_COMPLETE"}])
    remaining_put = list(put_responses) if put_responses is not None else None

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        path = request.url.path
        if request.method == "POST" and path == _CREATOR_PATH:
            levels = options if options is not None else ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]
            return httpx.Response(
                200, json={"data": {"privacy_level_options": levels}, "error": _OK}
            )
        if request.method == "POST" and path == _INIT_PATH:
            return httpx.Response(
                200,
                json={
                    "data": {"publish_id": _PUBLISH_ID, "upload_url": _UPLOAD_URL},
                    "error": _OK,
                },
            )
        if request.method == "PUT":
            if remaining_put is not None:
                return remaining_put.pop(0)
            return httpx.Response(201)
        if request.method == "POST" and path == _STATUS_PATH:
            data = remaining_status[0] if len(remaining_status) == 1 else remaining_status.pop(0)
            return httpx.Response(200, json={"data": data, "error": _OK})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    return handler


# ---- happy path -----------------------------------------------------------------


@pytest.mark.unit
async def test_publish_returns_publish_id_and_no_url(tmp_path: Path) -> None:
    dest = _dest(_flow())
    result = await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path))

    assert dest.platform == "tiktok"
    assert result.external_post_id == _PUBLISH_ID
    # TikTok's canonical URL needs the creator's username, which a credential-blind adapter
    # never sees, so no URL is invented.
    assert result.post_url is None


@pytest.mark.unit
async def test_caption_composes_title_and_inline_hashtags(tmp_path: Path) -> None:
    captured: list[httpx.Request] = []
    dest = _dest(_flow(captured=captured))
    await dest.publish(
        package=_pkg(title="Launch day", tags=("ai", "#video")),
        auth=_auth(),
        media=_media(tmp_path),
    )

    init = next(r for r in captured if r.url.path == _INIT_PATH)
    import json as _json

    body = _json.loads(init.content)
    assert body["post_info"]["title"] == "Launch day #ai #video"
    assert body["source_info"]["source"] == "FILE_UPLOAD"


@pytest.mark.unit
async def test_thumbnail_is_accepted_and_ignored(tmp_path: Path) -> None:
    """TikTok covers are frame timestamps; a supplied thumbnail is never fatal (ADR-0050)."""
    thumb = tmp_path / "cover.jpg"
    thumb.write_bytes(b"jpeg")
    media = _media(tmp_path)
    media = UploadMedia(
        path=media.path,
        mime_type=media.mime_type,
        size_bytes=media.size_bytes,
        thumbnail=UploadThumbnail(path=str(thumb), mime_type="image/jpeg", size_bytes=4),
    )
    dest = _dest(_flow())
    result = await dest.publish(package=_pkg(), auth=_auth(), media=media)
    assert result.external_post_id == _PUBLISH_ID


# ---- identifier stability (α9.6 ruling 3) ---------------------------------------


@pytest.mark.unit
async def test_public_post_id_never_replaces_the_publish_id(tmp_path: Path) -> None:
    """The identifier captured at publish time must stay stable for audit/reconciliation."""
    dest = _dest(
        _flow(
            statuses=[
                {
                    "status": "PUBLISH_COMPLETE",
                    "publicaly_available_post_id": ["7234567890123456789"],
                }
            ]
        )
    )
    result = await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path))
    assert result.external_post_id == _PUBLISH_ID
    assert "7234567890123456789" not in (result.external_post_id or "")
    assert result.post_url is None


# ---- metadata validation --------------------------------------------------------


@pytest.mark.unit
async def test_publish_at_is_rejected_not_ignored(tmp_path: Path) -> None:
    """α9.6 ruling 5 — TikTok cannot schedule, so a schedule must fail loudly."""

    def no_network(_: httpx.Request) -> httpx.Response:  # pragma: no cover - safety net
        raise AssertionError("no HTTP call expected")

    dest = _dest(no_network)
    when = datetime.now(UTC) + timedelta(hours=2)
    with pytest.raises(DestinationError) as err:
        await dest.publish(package=_pkg(publish_at=when), auth=_auth(), media=_media(tmp_path))
    assert err.value.code == "invalid_metadata"
    assert err.value.retryable is False


@pytest.mark.unit
async def test_unlisted_has_no_tiktok_equivalent(tmp_path: Path) -> None:
    dest = _dest(_flow())
    with pytest.raises(DestinationError) as err:
        await dest.publish(
            package=_pkg(visibility=Visibility.UNLISTED), auth=_auth(), media=_media(tmp_path)
        )
    assert err.value.code == "invalid_metadata"
    assert err.value.retryable is False


@pytest.mark.unit
async def test_visibility_unavailable_is_never_silently_downgraded(tmp_path: Path) -> None:
    """α9.6 ruling 4 — if the creator does not offer the requested level, fail explicitly."""
    dest = _dest(_flow(options=["SELF_ONLY"]))
    with pytest.raises(DestinationError) as err:
        await dest.publish(
            package=_pkg(visibility=Visibility.PUBLIC), auth=_auth(), media=_media(tmp_path)
        )
    assert err.value.code == "visibility_unavailable"
    assert err.value.retryable is False


@pytest.mark.unit
async def test_over_long_caption_is_permanent(tmp_path: Path) -> None:
    dest = _dest(_flow())
    with pytest.raises(DestinationError) as err:
        await dest.publish(package=_pkg(title="x" * 2500), auth=_auth(), media=_media(tmp_path))
    assert err.value.code == "invalid_metadata"


@pytest.mark.unit
async def test_guards_reject_missing_bearer_and_empty_media(tmp_path: Path) -> None:
    def no_network(_: httpx.Request) -> httpx.Response:  # pragma: no cover - safety net
        raise AssertionError("no HTTP call expected")

    dest = _dest(no_network)
    with pytest.raises(DestinationError) as err:
        await dest.publish(package=_pkg(), auth=_auth(token=""), media=_media(tmp_path))
    assert err.value.code == "missing_bearer"

    empty = UploadMedia(path=str(tmp_path / "none.mp4"), mime_type="video/mp4", size_bytes=0)
    with pytest.raises(DestinationError) as err:
        await dest.publish(package=_pkg(), auth=_auth(), media=empty)
    assert err.value.code == "empty_media"


# ---- chunking -------------------------------------------------------------------


@pytest.mark.unit
async def test_small_file_is_sent_as_one_chunk(tmp_path: Path) -> None:
    captured: list[httpx.Request] = []
    dest = _dest(_flow(captured=captured))
    await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path, size=2048))

    puts = [r for r in captured if r.method == "PUT"]
    assert len(puts) == 1
    assert puts[0].headers["content-range"] == "bytes 0-2047/2048"
    assert puts[0].headers["content-length"] == "2048"


@pytest.mark.unit
async def test_multi_chunk_uses_floor_count_and_trailing_final_chunk(tmp_path: Path) -> None:
    """TikTok specifies FLOOR division, so the last chunk absorbs the remainder."""
    size = 12 * 1024 * 1024  # 12MB with a 5MB chunk ⇒ floor(12/5) = 2 chunks
    captured: list[httpx.Request] = []
    dest = _dest(
        _flow(
            put_responses=[httpx.Response(206), httpx.Response(201)],
            captured=captured,
        ),
        chunk_size=5 * 1024 * 1024,
    )
    await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path, size=size))

    puts = [r for r in captured if r.method == "PUT"]
    assert len(puts) == 2
    assert puts[0].headers["content-range"] == f"bytes 0-5242879/{size}"
    assert puts[1].headers["content-range"] == f"bytes 5242880-{size - 1}/{size}"
    # The final chunk carries 7MB — chunk_size plus the trailing bytes.
    assert puts[1].headers["content-length"] == str(size - 5242880)

    init = next(r for r in captured if r.url.path == _INIT_PATH)
    import json as _json

    source = _json.loads(init.content)["source_info"]
    assert source["total_chunk_count"] == 2
    assert source["chunk_size"] == 5242880
    assert source["video_size"] == size


# ---- init-phase classification --------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error_code", "expected_code", "retryable"),
    [
        ("unaudited_client_can_only_post_to_private_accounts", "unaudited_client", False),
        ("spam_risk_too_many_posts", "spam_risk", False),
        ("privacy_level_option_mismatch", "invalid_metadata", False),
        ("access_token_invalid", "unauthorized", False),
        ("scope_not_authorized", "unauthorized", False),
        ("reached_active_user_cap", "quota_exceeded", True),
        ("rate_limit_exceeded", "tiktok_transient", True),
    ],
)
async def test_init_error_codes_are_classified(
    tmp_path: Path, error_code: str, expected_code: str, retryable: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _CREATOR_PATH:
            return httpx.Response(
                200,
                json={"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": _OK},
            )
        return httpx.Response(403, json={"data": {}, "error": {"code": error_code}})

    dest = _dest(handler)
    with pytest.raises(DestinationError) as err:
        await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path))
    assert err.value.code == expected_code
    assert err.value.retryable is retryable


@pytest.mark.unit
async def test_pre_upload_transport_error_is_retryable(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    dest = _dest(handler)
    with pytest.raises(DestinationError) as err:
        await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path))
    assert err.value.retryable is True
    assert err.value.code == "tiktok_transient"


# ---- PUB-11 -----------------------------------------------------------------------


@pytest.mark.unit
async def test_first_chunk_connect_error_is_retryable(tmp_path: Path) -> None:
    """Nothing was accepted yet, so a retry cannot duplicate the post."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            raise httpx.ConnectError("refused")
        return _flow()(request)

    dest = _dest(handler)
    with pytest.raises(DestinationError) as err:
        await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path))
    assert err.value.retryable is True


@pytest.mark.unit
async def test_transport_error_after_transmission_is_permanent(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            raise httpx.ReadTimeout("mid-flight")
        return _flow()(request)

    dest = _dest(handler)
    with pytest.raises(DestinationError) as err:
        await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path))
    assert err.value.code == "ambiguous_upload_outcome"
    assert err.value.retryable is False


@pytest.mark.unit
async def test_unexpected_upload_status_is_permanent(tmp_path: Path) -> None:
    dest = _dest(_flow(put_responses=[httpx.Response(500)]))
    with pytest.raises(DestinationError) as err:
        await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path))
    assert err.value.code == "ambiguous_upload_outcome"
    assert err.value.retryable is False


# ---- polling ---------------------------------------------------------------------


@pytest.mark.unit
async def test_polling_continues_until_terminal_success(tmp_path: Path) -> None:
    dest = _dest(
        _flow(
            statuses=[
                {"status": "PROCESSING_UPLOAD"},
                {"status": "PROCESSING_UPLOAD"},
                {"status": "PUBLISH_COMPLETE"},
            ]
        )
    )
    result = await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path))
    assert result.external_post_id == _PUBLISH_ID


@pytest.mark.unit
async def test_indeterminate_timeout_is_permanent(tmp_path: Path) -> None:
    """OUTCOME 3 — budget exhausted with no terminal state: never retried (PUB-11)."""
    dest = _dest(_flow(statuses=[{"status": "PROCESSING_UPLOAD"}]), budget=0.0)
    with pytest.raises(DestinationError) as err:
        await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path))
    assert err.value.code == "ambiguous_upload_outcome"
    assert err.value.retryable is False
    assert "PROCESSING_UPLOAD" in str(err.value)


@pytest.mark.unit
async def test_transient_status_poll_failure_does_not_escape_as_retryable(
    tmp_path: Path,
) -> None:
    """A flaky status poll must never surface retryable — that would re-upload (PUB-11)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == _STATUS_PATH:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("flaky")
            return httpx.Response(200, json={"data": {"status": "PUBLISH_COMPLETE"}, "error": _OK})
        return _flow()(request)

    dest = _dest(handler)
    result = await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path))
    assert result.external_post_id == _PUBLISH_ID
    assert calls["n"] == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fail_reason", "expected_code"),
    [
        ("spam_risk_text", "spam_risk"),
        ("auth_removed", "auth_removed"),
        ("duration_check_failed", "invalid_media"),
        ("file_format_check_failed", "invalid_media"),
        ("publish_cancelled", "publish_cancelled"),
    ],
)
async def test_terminal_failures_are_permanent(
    tmp_path: Path, fail_reason: str, expected_code: str
) -> None:
    dest = _dest(_flow(statuses=[{"status": "FAILED", "fail_reason": fail_reason}]))
    with pytest.raises(DestinationError) as err:
        await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path))
    assert err.value.code == expected_code
    assert err.value.retryable is False


@pytest.mark.unit
async def test_internal_fail_reason_is_permanent_despite_provider_guidance(
    tmp_path: Path,
) -> None:
    """α9.6 ruling 7 — TikTok calls ``internal`` retryable; our no-duplicate invariant wins.

    The failure can only surface after our bytes were accepted, so retrying would re-upload and
    risk a second public post. PUB-11 outranks the provider's recommendation.
    """
    dest = _dest(_flow(statuses=[{"status": "FAILED", "fail_reason": "internal"}]))
    with pytest.raises(DestinationError) as err:
        await dest.publish(package=_pkg(), auth=_auth(), media=_media(tmp_path))
    assert err.value.retryable is False
    assert err.value.code == "ambiguous_upload_outcome"
