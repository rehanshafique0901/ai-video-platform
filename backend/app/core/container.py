"""Application DI container — composition root.

This module is the *only* one under ``app.core`` that imports from
``app.infrastructure``. The API layer reaches infrastructure through
the container's accessors and therefore satisfies the import-linter
contract that forbids ``app.api`` from importing infrastructure
directly (see ``pyproject.toml`` ``[[tool.importlinter.contracts]]``).

Lifecycle:

1. ``init(settings)`` is called by ``app.main.create_app`` — builds
   the async engine, session factory, password hasher, JWT service,
   token issuer, and (α2a) pre-computes the anti-enumeration dummy
   Argon2 hash so the ``LoginUser`` use case doesn't pay a ~300 ms
   Argon2 cost on the first request per process.
2. ``get_session`` / ``get_unit_of_work`` and the various
   use-case factories are imported by ``app.api.v1.deps`` and used
   as FastAPI dependencies.
3. ``shutdown()`` is called by the lifespan handler on process exit
   to dispose the engine cleanly.
4. ``reset()`` is provided for tests; it clears all singletons so the
   next ``init`` rebuilds them against test settings.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.interfaces.clock import IClock
from app.application.interfaces.gif_previewer import IGifPreviewer
from app.application.interfaces.media_downloader import IMediaDownloader
from app.application.interfaces.object_storage import IObjectStorage
from app.application.interfaces.preview_clipper import IPreviewClipper
from app.application.interfaces.provider_dispatcher import ProviderDispatcherPort
from app.application.interfaces.providers import Capability
from app.application.interfaces.publisher import PublisherPort
from app.application.interfaces.renderer import IRenderer
from app.application.interfaces.repositories import IUserRepository
from app.application.interfaces.security import IPasswordHasher, ITokenIssuer
from app.application.interfaces.thumbnailer import IThumbnailer
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.interfaces.waveform_renderer import IWaveformRenderer
from app.application.interfaces.webhook_verifier import IWebhookVerifier
from app.application.use_cases.auth.login_user import LoginUser
from app.application.use_cases.auth.logout_session import LogoutSession
from app.application.use_cases.auth.refresh_session import RefreshSession
from app.application.use_cases.auth.register_user import RegisterUser
from app.application.use_cases.media.delete_media import DeleteMedia
from app.application.use_cases.media.enrich_generated_media import EnrichGeneratedMedia
from app.application.use_cases.media.enrichers import (
    Enricher,
    GifEnricher,
    PreviewEnricher,
    ThumbnailEnricher,
    WaveformEnricher,
)
from app.application.use_cases.media.generated_media_subscriber import (
    GeneratedMediaIngestionSubscriber,
)
from app.application.use_cases.media.get_media import GetMedia
from app.application.use_cases.media.ingest_generated_media import IngestGeneratedMedia
from app.application.use_cases.media.list_media import ListMedia
from app.application.use_cases.media.media_enrichment_worker import MediaEnrichmentWorker
from app.application.use_cases.media.register_media import RegisterMedia
from app.application.use_cases.media.update_media import UpdateMedia
from app.application.use_cases.projects.create_project import CreateProject
from app.application.use_cases.projects.delete_project import DeleteProject
from app.application.use_cases.projects.get_project import GetProject
from app.application.use_cases.projects.list_projects import ListProjects
from app.application.use_cases.projects.update_project import UpdateProject
from app.application.use_cases.prompts.create_prompt import CreatePrompt
from app.application.use_cases.prompts.delete_prompt import DeletePrompt
from app.application.use_cases.prompts.get_prompt import GetPrompt
from app.application.use_cases.prompts.list_prompts import ListPrompts
from app.application.use_cases.prompts.update_prompt import UpdatePrompt
from app.application.use_cases.relay.relay_service import RelayService
from app.application.use_cases.render.cancel_render_job import CancelRenderJob
from app.application.use_cases.render.create_render_job import CreateRenderJob
from app.application.use_cases.render.get_render_job import GetRenderJob
from app.application.use_cases.render.list_render_jobs import ListRenderJobs
from app.application.use_cases.render.process_render_job import ProcessRenderJob
from app.application.use_cases.render.render_worker import RenderWorker
from app.application.use_cases.scenes.create_scene import CreateScene
from app.application.use_cases.scenes.delete_scene import DeleteScene
from app.application.use_cases.scenes.get_scene import GetScene
from app.application.use_cases.scenes.list_scenes import ListScenes
from app.application.use_cases.scenes.move_scene import MoveScene
from app.application.use_cases.scenes.update_scene import UpdateScene
from app.application.use_cases.timeline.create_clip import CreateClip
from app.application.use_cases.timeline.create_track import CreateTrack
from app.application.use_cases.timeline.delete_clip import DeleteClip
from app.application.use_cases.timeline.delete_track import DeleteTrack
from app.application.use_cases.timeline.get_clip import GetClip
from app.application.use_cases.timeline.get_timeline import GetTimeline
from app.application.use_cases.timeline.list_clips import ListClips
from app.application.use_cases.timeline.list_tracks import ListTracks
from app.application.use_cases.timeline.provision_timeline import ProvisionTimeline
from app.application.use_cases.timeline.update_clip import UpdateClip
from app.application.use_cases.timeline.update_timeline import UpdateTimeline
from app.application.use_cases.timeline.update_track import UpdateTrack
from app.application.use_cases.usage.usage_recorder_service import UsageRecorderService
from app.application.use_cases.users.update_profile import UpdateUserProfile
from app.application.use_cases.versions.branch_version import BranchProjectVersion
from app.application.use_cases.versions.create_version import CreateProjectVersion
from app.application.use_cases.versions.diff_versions import DiffProjectVersions
from app.application.use_cases.versions.get_version import GetProjectVersion
from app.application.use_cases.versions.list_versions import ListProjectVersions
from app.application.use_cases.versions.restore_version import RestoreProjectVersion
from app.application.use_cases.workflow.advance_workflow_run import AdvanceWorkflowRun
from app.application.use_cases.workflow.cancel_workflow_run import CancelWorkflowRun
from app.application.use_cases.workflow.completion_engine import CompletionEngine
from app.application.use_cases.workflow.create_workflow_run import CreateWorkflowRun
from app.application.use_cases.workflow.get_workflow_run import GetWorkflowRun
from app.application.use_cases.workflow.list_workflow_runs import ListWorkflowRuns
from app.application.use_cases.workflow.receive_provider_webhook import ReceiveProviderWebhook
from app.application.use_cases.workflow.resume_workflow_run import ResumeWorkflowRun
from app.core.config import Settings
from app.infrastructure.ai.dispatcher import StepCommandDispatcher
from app.infrastructure.ai.providers.fal import FalVideoProvider, FalWebhookVerifier
from app.infrastructure.ai.providers.mocks import (
    MockImageProvider,
    MockLLMProvider,
    MockVideoProvider,
    MockVoiceProvider,
)
from app.infrastructure.ai.providers.openai import OpenAIImageProvider
from app.infrastructure.ai.providers.ports import Provider
from app.infrastructure.ai.providers.registry import ProviderRegistry
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.session import make_engine, make_session_factory
from app.infrastructure.media import HttpMediaDownloader
from app.infrastructure.publisher.in_process_publisher import InProcessPublisher
from app.infrastructure.render import (
    FfmpegGifPreviewer,
    FfmpegPreviewClipper,
    FfmpegRenderer,
    FfmpegThumbnailer,
    FfmpegWaveformRenderer,
)
from app.infrastructure.repositories.user_repository import UserRepository
from app.infrastructure.security.jwt import JWTService
from app.infrastructure.security.password_hasher import PasswordHasher
from app.infrastructure.security.token_issuer import AuthTokenIssuer
from app.infrastructure.storage import LocalObjectStorage
from app.infrastructure.uow.sqlalchemy_unit_of_work import SqlAlchemyUnitOfWork

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_password_hasher: PasswordHasher | None = None
_jwt_service: JWTService | None = None
_token_issuer: AuthTokenIssuer | None = None
_dummy_password_hash: str | None = None
_clock: SystemClock | None = None
_publisher: PublisherPort | None = None
# α8.1/α8.2: the process-wide provider registry + the shared real-provider HTTP
# clients. The registry is settings-dependent — it wires the real IMAGE provider
# iff an OpenAI key is configured, and the real (async) VIDEO provider iff a Fal
# key is configured — so it joins the init/shutdown/reset lifecycle.
_provider_registry: ProviderRegistry | None = None
_openai_client: httpx.AsyncClient | None = None
_fal_client: httpx.AsyncClient | None = None
# α8.3b: the inbound Fal webhook verifier + its dedicated JWKS HTTP client. Built
# unconditionally (verification uses Fal's PUBLIC JWKS keys, no API key needed).
_fal_webhook_verifier: IWebhookVerifier | None = None
_fal_webhook_client: httpx.AsyncClient | None = None
# α8.4a: the generated-media ingestion pieces — object storage (local FS adapter),
# the artifact downloader + its dedicated httpx client. The downstream subscriber
# is registered on the in-process publisher at init.
_object_storage: IObjectStorage | None = None
_media_downloader: IMediaDownloader | None = None
_renderer: IRenderer | None = None
_thumbnailer: IThumbnailer | None = None
_preview_clipper: IPreviewClipper | None = None
_gif_previewer: IGifPreviewer | None = None
_waveform_renderer: IWaveformRenderer | None = None
_media_download_client: httpx.AsyncClient | None = None
# α8.3: settings retained for the completion engine's lease owner + duration.
_settings: Settings | None = None


def init(settings: Settings) -> None:
    """Build the process-wide singletons.

    Idempotent: a second call with the container already initialised is
    a no-op. Tests that want to rebuild with different settings should
    call ``reset()`` first.
    """
    global _engine, _session_factory, _password_hasher, _jwt_service
    global _token_issuer, _dummy_password_hash, _clock, _publisher
    global _provider_registry, _openai_client, _fal_client, _settings
    if _engine is not None:
        return
    _settings = settings
    _engine = make_engine(settings.database_url)
    _session_factory = make_session_factory(_engine)
    _password_hasher = PasswordHasher()
    _jwt_service = JWTService(
        secret=settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
        access_ttl_seconds=settings.jwt_access_ttl_seconds,
        refresh_ttl_seconds=settings.jwt_refresh_ttl_seconds,
    )
    _token_issuer = AuthTokenIssuer(
        jwt_service=_jwt_service,
        refresh_ttl_seconds=settings.jwt_refresh_ttl_seconds,
    )
    # Anti-enumeration dummy hash: computed once at startup so
    # ``LoginUser`` never pays the ~300 ms Argon2 cost per request.
    # The plaintext is a discarded random 32-byte secret — even if an
    # attacker learned it, it would not help them since the hash is
    # never a real user's password.
    _dummy_password_hash = _password_hasher.hash(secrets.token_urlsafe(32))
    _clock = SystemClock()
    # α7.3: the outbox relay's publish target. In-process; α8.4a registers the FIRST
    # real consumer — the generated-media ingestion subscriber for
    # ``WorkflowRunSucceeded`` — behind the same ``PublisherPort`` (the relay is
    # untouched). A broker-backed publisher can replace this later identically. The
    # subscriber holds a *factory* (not an instance); the object storage + artifact
    # downloader it needs are built lazily on the first ingestion (see
    # ``get_ingest_generated_media_use_case``) so the common test path opens no
    # HTTP client it must close.
    _publisher = InProcessPublisher(
        [GeneratedMediaIngestionSubscriber(get_ingest_generated_media_use_case)]
    )
    # α8.1/α8.2: wire the provider registry. When a provider's key is configured,
    # build a single shared, pre-authenticated httpx client and register the real
    # provider for that capability; otherwise the capability stays on its
    # deterministic mock. IMAGE ← OpenAI (α8.1); VIDEO ← Fal (α8.2); LLM / VOICE
    # always mock. Secrets are injected into the clients here and never read by
    # the providers (W8.1.1 — adapters are configuration-blind; receive, never
    # retrieve).
    _openai_client = _build_openai_client(settings)
    _fal_client = _build_fal_client(settings)
    _provider_registry = _build_provider_registry(_openai_client, _fal_client)
    # α8.3b: the Fal webhook verifier is built *lazily* (see
    # ``_get_fal_webhook_verifier``) so the common test path (init without ever
    # hitting the webhook route) never opens an HTTP client it must close.


def _build_openai_client(settings: Settings) -> httpx.AsyncClient | None:
    """A shared, pre-authenticated OpenAI client — or ``None`` when no key is set."""
    key = settings.openai_api_key
    if key is None:
        return None
    return httpx.AsyncClient(
        base_url=settings.openai_base_url,
        timeout=settings.openai_timeout_seconds,
        headers={"Authorization": f"Bearer {key.get_secret_value()}"},
    )


def _build_fal_client(settings: Settings) -> httpx.AsyncClient | None:
    """A shared, pre-authenticated Fal.ai client — or ``None`` when no key is set.

    Fal uses the ``Key`` auth scheme (not ``Bearer``). The header + base URL +
    per-attempt timeout are baked in here; ``FalVideoProvider`` is
    configuration-blind and never sees the raw key (W8.1.1).
    """
    key = settings.fal_api_key
    if key is None:
        return None
    return httpx.AsyncClient(
        base_url=settings.fal_base_url,
        timeout=settings.fal_timeout_seconds,
        headers={"Authorization": f"Key {key.get_secret_value()}"},
    )


def _build_provider_registry(
    openai_client: httpx.AsyncClient | None,
    fal_client: httpx.AsyncClient | None,
) -> ProviderRegistry:
    """Compose the registry: exactly one provider per capability (no selection).

    IMAGE resolves to the real ``OpenAIImageProvider`` iff an OpenAI client was
    built, else the mock; VIDEO resolves to the real ``FalVideoProvider`` iff a
    Fal client was built, else the mock (composed independently of IMAGE). LLM /
    VOICE are always mock. ``resolve`` stays a direct lookup — there is no
    fallback, priority, weighting, or health ordering.
    """
    registry = ProviderRegistry()
    registry.register(provider=MockLLMProvider(), capabilities=[Capability.LLM])
    registry.register(provider=MockVoiceProvider(), capabilities=[Capability.VOICE])
    image_provider: Provider = (
        OpenAIImageProvider(client=openai_client)
        if openai_client is not None
        else MockImageProvider()
    )
    registry.register(provider=image_provider, capabilities=[Capability.IMAGE])
    video_provider: Provider = (
        FalVideoProvider(client=fal_client) if fal_client is not None else MockVideoProvider()
    )
    registry.register(provider=video_provider, capabilities=[Capability.VIDEO])
    return registry


async def shutdown() -> None:
    """Dispose the engine + the shared provider clients. Called by the lifespan handler."""
    global _engine, _session_factory, _provider_registry, _openai_client, _fal_client, _settings
    global _fal_webhook_verifier, _fal_webhook_client
    global _object_storage, _media_downloader, _media_download_client, _renderer, _thumbnailer
    global _preview_clipper, _gif_previewer, _waveform_renderer
    if _engine is not None:
        await _engine.dispose()
    if _openai_client is not None:
        await _openai_client.aclose()
    if _fal_client is not None:
        await _fal_client.aclose()
    if _fal_webhook_client is not None:
        await _fal_webhook_client.aclose()
    if _media_download_client is not None:
        await _media_download_client.aclose()
    _engine = None
    _session_factory = None
    _provider_registry = None
    _openai_client = None
    _fal_client = None
    _fal_webhook_verifier = None
    _fal_webhook_client = None
    _object_storage = None
    _media_downloader = None
    _media_download_client = None
    _renderer = None
    _thumbnailer = None
    _preview_clipper = None
    _gif_previewer = None
    _waveform_renderer = None
    _settings = None


def reset() -> None:
    """Test-only: clear all singletons so the next ``init`` rebuilds them."""
    global _engine, _session_factory, _password_hasher, _jwt_service
    global _token_issuer, _dummy_password_hash, _clock, _publisher
    global _provider_registry, _openai_client, _fal_client, _settings
    global _fal_webhook_verifier, _fal_webhook_client
    global _object_storage, _media_downloader, _media_download_client, _renderer, _thumbnailer
    global _preview_clipper, _gif_previewer, _waveform_renderer
    _settings = None
    _engine = None
    _session_factory = None
    _password_hasher = None
    _jwt_service = None
    _token_issuer = None
    _dummy_password_hash = None
    _clock = None
    _publisher = None
    # Test-only: drop the registry + client refs. Tests run without provider keys,
    # so the clients are ``None`` here (no un-awaited client to close); the real
    # clients are closed in ``shutdown()`` on the production lifespan path.
    _provider_registry = None
    _openai_client = None
    _fal_client = None
    _fal_webhook_verifier = None
    _fal_webhook_client = None
    _object_storage = None
    _media_downloader = None
    _media_download_client = None
    _renderer = None
    _thumbnailer = None
    _preview_clipper = None
    _gif_previewer = None
    _waveform_renderer = None


def _require_init() -> None:
    if _engine is None:
        raise RuntimeError(
            "container not initialised — call init(settings) first "
            "(usually done by app.main.create_app)"
        )


def _get_settings() -> Settings:
    _require_init()
    assert _settings is not None
    return _settings


def get_engine() -> AsyncEngine:
    _require_init()
    assert _engine is not None
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    _require_init()
    assert _session_factory is not None
    return _session_factory


def get_password_hasher() -> IPasswordHasher:
    _require_init()
    assert _password_hasher is not None
    return _password_hasher


def get_jwt_service() -> JWTService:
    _require_init()
    assert _jwt_service is not None
    return _jwt_service


def get_token_issuer() -> ITokenIssuer:
    _require_init()
    assert _token_issuer is not None
    return _token_issuer


def get_dummy_password_hash() -> str:
    _require_init()
    assert _dummy_password_hash is not None
    return _dummy_password_hash


def get_clock() -> IClock:
    _require_init()
    assert _clock is not None
    return _clock


def get_publisher() -> PublisherPort:
    """The process-wide outbox publisher (α7.3). In-process, no broker."""
    _require_init()
    assert _publisher is not None
    return _publisher


def get_relay_service() -> RelayService:
    """Factory: a fresh outbox ``RelayService`` bound to a new UoW + the publisher.

    Library-only (α7.3): no daemon, no endpoint. The α8.1 worker calls
    ``relay_once`` on a cadence; α7.3 exposes the primitive for tests + wiring.
    """
    return RelayService(uow=get_unit_of_work(), publisher=get_publisher())


def get_provider_registry() -> ProviderRegistry:
    """The process-wide provider registry (α7.4, settings-composed in α8.1/α8.2).

    Built by :func:`init` with exactly one provider per capability: the real
    ``OpenAIImageProvider`` for IMAGE when an OpenAI key is configured (else mock),
    the real ``FalVideoProvider`` for VIDEO when a Fal key is configured (else
    mock); LLM / VOICE always mock. Callers (dispatcher → runner) are unchanged —
    they still ``resolve`` a capability and never learn which concrete provider
    served it (W8.1.3 / W8.2.1).
    """
    _require_init()
    assert _provider_registry is not None
    return _provider_registry


def get_step_command_dispatcher() -> ProviderDispatcherPort:
    """Factory: a ``StepCommandDispatcher`` over the process-wide registry (α7.4).

    Library-only: the α7.2 runner is **not** wired to it in this slice (D3.3); the
    α7.6 pipeline depends on this ``ProviderDispatcherPort`` to turn ``StepCommand``s
    into provider calls.
    """
    return StepCommandDispatcher(get_provider_registry())


def get_usage_recorder_service() -> UsageRecorderService:
    """Factory: a ``UsageRecorderService`` bound to a fresh UoW (α7.5).

    Seam-only (α7.5 sign-off Q2): the recorder is **not** wired into the
    runner/dispatcher this slice; the α7.6 pipeline calls
    :meth:`UsageRecorderPort.record` around each terminal dispatch. Purely
    observational (W7.5.1) — its only write is ``usage_records``.
    """
    return UsageRecorderService(uow=get_unit_of_work())


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield one ``AsyncSession`` per request.

    The session is closed automatically. Commits are the caller's
    responsibility — for transactional mutations, acquire a UnitOfWork
    via ``get_unit_of_work`` instead.
    """
    factory = get_session_factory()
    async with factory() as session:
        yield session


def get_unit_of_work() -> IUnitOfWork:
    """FastAPI dependency: a fresh UnitOfWork bound to the session factory."""
    return SqlAlchemyUnitOfWork(get_session_factory())


def get_user_repository(session: AsyncSession) -> IUserRepository:
    """Factory: a ``UserRepository`` over the supplied session."""
    return UserRepository(session)


# ---------------------------------------------------------------------
# Use-case factories (Slice α2a)
# ---------------------------------------------------------------------
#
# Each factory constructs one use case with a fresh UoW + the shared
# security singletons. FastAPI-facing wrappers live in
# ``app.api.v1.deps`` so the router never imports this module.
# ---------------------------------------------------------------------


def get_register_user_use_case() -> RegisterUser:
    return RegisterUser(
        uow=get_unit_of_work(),
        hasher=get_password_hasher(),
        token_issuer=get_token_issuer(),
        clock=get_clock(),
    )


def get_login_user_use_case() -> LoginUser:
    return LoginUser(
        uow=get_unit_of_work(),
        hasher=get_password_hasher(),
        token_issuer=get_token_issuer(),
        dummy_password_hash=get_dummy_password_hash(),
        clock=get_clock(),
    )


def get_refresh_session_use_case() -> RefreshSession:
    return RefreshSession(
        uow=get_unit_of_work(),
        token_issuer=get_token_issuer(),
        clock=get_clock(),
    )


def get_logout_session_use_case() -> LogoutSession:
    return LogoutSession(
        uow=get_unit_of_work(),
        token_issuer=get_token_issuer(),
        clock=get_clock(),
    )


# ---------------------------------------------------------------------
# Use-case factories (Slice α4)
# ---------------------------------------------------------------------


def get_update_user_profile_use_case() -> UpdateUserProfile:
    """Factory: a fresh ``UpdateUserProfile`` use case bound to a new UoW.

    See pre-flight §4.1 (composition root wiring) and §12 step 8.
    """
    return UpdateUserProfile(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α5a — Projects create + read)
# ---------------------------------------------------------------------


def get_create_project_use_case() -> CreateProject:
    return CreateProject(uow=get_unit_of_work())


def get_get_project_use_case() -> GetProject:
    return GetProject(uow=get_unit_of_work())


def get_list_projects_use_case() -> ListProjects:
    return ListProjects(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α5b — Projects update + soft-delete)
# ---------------------------------------------------------------------


def get_update_project_use_case() -> UpdateProject:
    return UpdateProject(uow=get_unit_of_work())


def get_delete_project_use_case() -> DeleteProject:
    return DeleteProject(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α5c — Scenes CRUD + reorder)
# ---------------------------------------------------------------------


def get_create_scene_use_case() -> CreateScene:
    return CreateScene(uow=get_unit_of_work())


def get_list_scenes_use_case() -> ListScenes:
    return ListScenes(uow=get_unit_of_work())


def get_get_scene_use_case() -> GetScene:
    return GetScene(uow=get_unit_of_work())


def get_update_scene_use_case() -> UpdateScene:
    return UpdateScene(uow=get_unit_of_work())


def get_move_scene_use_case() -> MoveScene:
    return MoveScene(uow=get_unit_of_work())


def get_delete_scene_use_case() -> DeleteScene:
    return DeleteScene(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α5d.1 — Project Versions create + read)
# ---------------------------------------------------------------------


def get_create_project_version_use_case() -> CreateProjectVersion:
    return CreateProjectVersion(uow=get_unit_of_work())


def get_list_project_versions_use_case() -> ListProjectVersions:
    return ListProjectVersions(uow=get_unit_of_work())


def get_get_project_version_use_case() -> GetProjectVersion:
    return GetProjectVersion(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α5d.2 — Project Version restore + diff)
# ---------------------------------------------------------------------


def get_restore_project_version_use_case() -> RestoreProjectVersion:
    return RestoreProjectVersion(uow=get_unit_of_work())


def get_diff_project_versions_use_case() -> DiffProjectVersions:
    return DiffProjectVersions(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α5d.3 — Project Version branch / fork)
# ---------------------------------------------------------------------


def get_branch_project_version_use_case() -> BranchProjectVersion:
    return BranchProjectVersion(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α6.1 — Prompts CRUD)
# ---------------------------------------------------------------------


def get_create_prompt_use_case() -> CreatePrompt:
    return CreatePrompt(uow=get_unit_of_work())


def get_list_prompts_use_case() -> ListPrompts:
    return ListPrompts(uow=get_unit_of_work())


def get_get_prompt_use_case() -> GetPrompt:
    return GetPrompt(uow=get_unit_of_work())


def get_update_prompt_use_case() -> UpdatePrompt:
    return UpdatePrompt(uow=get_unit_of_work())


def get_delete_prompt_use_case() -> DeletePrompt:
    return DeletePrompt(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α6.2 — Media register/CRUD)
# ---------------------------------------------------------------------


def get_register_media_use_case() -> RegisterMedia:
    return RegisterMedia(uow=get_unit_of_work())


def get_list_media_use_case() -> ListMedia:
    return ListMedia(uow=get_unit_of_work())


def get_get_media_use_case() -> GetMedia:
    return GetMedia(uow=get_unit_of_work())


def get_update_media_use_case() -> UpdateMedia:
    return UpdateMedia(uow=get_unit_of_work())


def get_delete_media_use_case() -> DeleteMedia:
    return DeleteMedia(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α6.3a — Timeline + Tracks)
# ---------------------------------------------------------------------


def get_provision_timeline_use_case() -> ProvisionTimeline:
    return ProvisionTimeline(uow=get_unit_of_work())


def get_get_timeline_use_case() -> GetTimeline:
    return GetTimeline(uow=get_unit_of_work())


def get_update_timeline_use_case() -> UpdateTimeline:
    return UpdateTimeline(uow=get_unit_of_work())


def get_create_track_use_case() -> CreateTrack:
    return CreateTrack(uow=get_unit_of_work())


def get_list_tracks_use_case() -> ListTracks:
    return ListTracks(uow=get_unit_of_work())


def get_update_track_use_case() -> UpdateTrack:
    return UpdateTrack(uow=get_unit_of_work())


def get_delete_track_use_case() -> DeleteTrack:
    return DeleteTrack(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α6.3b — Clips)
# ---------------------------------------------------------------------


def get_create_clip_use_case() -> CreateClip:
    return CreateClip(uow=get_unit_of_work())


def get_list_clips_use_case() -> ListClips:
    return ListClips(uow=get_unit_of_work())


def get_get_clip_use_case() -> GetClip:
    return GetClip(uow=get_unit_of_work())


def get_update_clip_use_case() -> UpdateClip:
    return UpdateClip(uow=get_unit_of_work())


def get_delete_clip_use_case() -> DeleteClip:
    return DeleteClip(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α7.1 — Render jobs)
# ---------------------------------------------------------------------


def get_create_render_job_use_case() -> CreateRenderJob:
    return CreateRenderJob(uow=get_unit_of_work())


def get_list_render_jobs_use_case() -> ListRenderJobs:
    return ListRenderJobs(uow=get_unit_of_work())


def get_get_render_job_use_case() -> GetRenderJob:
    return GetRenderJob(uow=get_unit_of_work())


def get_cancel_render_job_use_case() -> CancelRenderJob:
    return CancelRenderJob(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α7.2 — Workflow runs)
# ---------------------------------------------------------------------
# The runner-bearing use cases (create/advance) default to the module-level
# ``WORKFLOW_REGISTRY``; tests inject their own registry directly.


def get_create_workflow_run_use_case() -> CreateWorkflowRun:
    return CreateWorkflowRun(uow=get_unit_of_work())


def get_list_workflow_runs_use_case() -> ListWorkflowRuns:
    return ListWorkflowRuns(uow=get_unit_of_work())


def get_get_workflow_run_use_case() -> GetWorkflowRun:
    return GetWorkflowRun(uow=get_unit_of_work())


def get_advance_workflow_run_use_case() -> AdvanceWorkflowRun:
    # α7.6: the runner is now wired to the α7.4 dispatcher so it can interpret a
    # pure step's StepCommands (dispatch → mock provider → record usage → checkpoint).
    # Deterministic α7.2 workflows emit no commands, so they never touch it.
    return AdvanceWorkflowRun(
        uow=get_unit_of_work(),
        dispatcher=get_step_command_dispatcher(),
    )


def get_cancel_workflow_run_use_case() -> CancelWorkflowRun:
    return CancelWorkflowRun(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α8.3 — Completion engine)
# ---------------------------------------------------------------------


def get_resume_workflow_run_use_case() -> ResumeWorkflowRun:
    """Factory: the public resume seam, with its runner sharing ONE UoW.

    The runner is constructed over the *same* UoW as the use case so the runner's
    continuation participates in the resume's single transaction (resume + terminal
    usage + step-succeeded + continue + settle commit atomically — α8.3 Fork 2).
    """
    uow = get_unit_of_work()
    runner = AdvanceWorkflowRun(uow=uow, dispatcher=get_step_command_dispatcher())
    return ResumeWorkflowRun(uow=uow, runner=runner)


def get_completion_engine() -> CompletionEngine:
    """Factory: the α8.3 completion engine (polling ingress + provider resolve).

    Library-only (no daemon): a test loop / trigger drives ``poll_once`` /
    ``complete``. Its read/lease UoW is independent of the resume use case's UoW, so
    provider I/O never holds a DB transaction open. Lease owner + duration come from
    settings.
    """
    settings = _get_settings()
    return CompletionEngine(
        uow=get_unit_of_work(),
        resume=get_resume_workflow_run_use_case(),
        dispatcher=get_step_command_dispatcher(),
        owner=settings.completion_lock_owner,
        lease=timedelta(seconds=settings.completion_lease_seconds),
    )


# ---------------------------------------------------------------------
# Use-case factories (Slice α8.3b — Webhook completion ingress)
# ---------------------------------------------------------------------


def _get_fal_webhook_verifier() -> IWebhookVerifier:
    """Lazily build + memoise the Fal webhook verifier and its JWKS client.

    Built on first use (not at ``init``) so the common test path never opens an
    HTTP client it must close. The client has no base URL / auth header — Fal's
    JWKS holds **public** verification keys (W8.1.1 governs credentials, not
    public trust anchors), and the JWKS URL is absolute.
    """
    global _fal_webhook_verifier, _fal_webhook_client
    _require_init()
    if _fal_webhook_verifier is None:
        settings = _get_settings()
        assert _clock is not None
        _fal_webhook_client = httpx.AsyncClient(timeout=settings.fal_timeout_seconds)
        _fal_webhook_verifier = FalWebhookVerifier(
            client=_fal_webhook_client,
            jwks_url=settings.fal_webhook_jwks_url,
            clock=_clock,
            timestamp_tolerance_seconds=settings.fal_webhook_timestamp_tolerance_seconds,
            jwks_cache_seconds=settings.fal_webhook_jwks_cache_seconds,
        )
    return _fal_webhook_verifier


def get_receive_provider_webhook_use_case() -> ReceiveProviderWebhook:
    """Factory: the α8.3b webhook ingress (verify → find paused run → complete()).

    Provider-agnostic: ``verifiers`` is a per-provider registry (α8.3b ships only
    Fal). The ingress reads with its own UoW and delegates all state changes to the
    frozen completion engine (W8.3b.1).
    """
    return ReceiveProviderWebhook(
        uow=get_unit_of_work(),
        completion_engine=get_completion_engine(),
        verifiers={"fal": _get_fal_webhook_verifier()},
    )


# ---------------------------------------------------------------------
# Use-case factories (Slice α8.4a — Generated media ingestion)
# ---------------------------------------------------------------------


def _get_object_storage() -> IObjectStorage:
    """Lazily build + memoise the object-storage adapter (local FS in α8.4a)."""
    global _object_storage
    _require_init()
    if _object_storage is None:
        settings = _get_settings()
        _object_storage = LocalObjectStorage(
            root=settings.media_storage_root, bucket=settings.media_storage_bucket
        )
    return _object_storage


def _get_media_downloader() -> IMediaDownloader:
    """Lazily build + memoise the artifact downloader + its dedicated HTTP client.

    Built on first ingestion (not at ``init``) so the common test path opens no HTTP
    client it must close; the client is disposed in ``shutdown()``.
    """
    global _media_downloader, _media_download_client
    _require_init()
    if _media_downloader is None:
        settings = _get_settings()
        _media_download_client = httpx.AsyncClient(
            timeout=settings.media_download_timeout_seconds, follow_redirects=True
        )
        _media_downloader = HttpMediaDownloader(
            client=_media_download_client, max_bytes=settings.media_download_max_bytes
        )
    return _media_downloader


def get_ingest_generated_media_use_case() -> IngestGeneratedMedia:
    """Factory: the α8.4a generated-media ingestion use case (fresh UoW per call).

    Invoked by the ``WorkflowRunSucceeded`` subscriber once per delivered event, so
    each ingestion runs in its own Unit of Work. Object storage + downloader are
    process-wide (memoised); the use case is strictly downstream of — and never
    mutates — the frozen orchestration pipeline (W8.4.1 / W8.4.2).
    """
    return IngestGeneratedMedia(
        uow=get_unit_of_work(),
        storage=_get_object_storage(),
        downloader=_get_media_downloader(),
    )


# ---------------------------------------------------------------------
# Use-case factories (Slice α8.4b — Render engine)
# ---------------------------------------------------------------------


def _get_renderer() -> IRenderer:
    """Lazily build + memoise the FFmpeg renderer (configuration-blind, W8.1.1)."""
    global _renderer
    _require_init()
    if _renderer is None:
        settings = _get_settings()
        _renderer = FfmpegRenderer(
            ffmpeg_path=settings.render_ffmpeg_path,
            ffprobe_path=settings.render_ffprobe_path,
            timeout_seconds=settings.render_timeout_seconds,
        )
    return _renderer


def get_process_render_job_use_case() -> ProcessRenderJob:
    """Factory: the α8.4b single-job render use case (fresh UoW per call)."""
    settings = _get_settings()
    return ProcessRenderJob(
        uow=get_unit_of_work(),
        storage=_get_object_storage(),
        renderer=_get_renderer(),
        workspace_dir=settings.render_workspace_dir,
        lease=timedelta(seconds=settings.render_timeout_seconds),
    )


def get_render_worker() -> RenderWorker:
    """Factory: the α8.4b render poll ingress (``run_once`` drains queued jobs).

    Mirrors ``CompletionEngine`` wiring — the worker is the render-side poller. It
    is a pure Timeline → Media transform and never touches the frozen orchestration
    core (W8.4b.1 / W8.4b.2).
    """
    settings = _get_settings()
    return RenderWorker(
        uow=get_unit_of_work(),
        process=get_process_render_job_use_case(),
        batch_size=settings.render_batch_size,
    )


# ---------------------------------------------------------------------
# Use-case factories (Slice α8.4c — Media enrichment)
# ---------------------------------------------------------------------


def _get_thumbnailer() -> IThumbnailer:
    """Lazily build + memoise the FFmpeg thumbnailer (configuration-blind, W8.1.1)."""
    global _thumbnailer
    _require_init()
    if _thumbnailer is None:
        settings = _get_settings()
        _thumbnailer = FfmpegThumbnailer(
            ffmpeg_path=settings.render_ffmpeg_path,
            ffprobe_path=settings.render_ffprobe_path,
            timeout_seconds=settings.render_timeout_seconds,
        )
    return _thumbnailer


def _get_preview_clipper() -> IPreviewClipper:
    """Lazily build + memoise the FFmpeg preview clipper (configuration-blind, W8.1.1)."""
    global _preview_clipper
    _require_init()
    if _preview_clipper is None:
        settings = _get_settings()
        _preview_clipper = FfmpegPreviewClipper(
            ffmpeg_path=settings.render_ffmpeg_path,
            ffprobe_path=settings.render_ffprobe_path,
            timeout_seconds=settings.render_timeout_seconds,
        )
    return _preview_clipper


def _get_gif_previewer() -> IGifPreviewer:
    """Lazily build + memoise the FFmpeg GIF previewer (configuration-blind, W8.1.1)."""
    global _gif_previewer
    _require_init()
    if _gif_previewer is None:
        settings = _get_settings()
        _gif_previewer = FfmpegGifPreviewer(
            ffmpeg_path=settings.render_ffmpeg_path,
            ffprobe_path=settings.render_ffprobe_path,
            timeout_seconds=settings.render_timeout_seconds,
        )
    return _gif_previewer


def _get_waveform_renderer() -> IWaveformRenderer:
    """Lazily build + memoise the FFmpeg waveform renderer (configuration-blind, W8.1.1)."""
    global _waveform_renderer
    _require_init()
    if _waveform_renderer is None:
        settings = _get_settings()
        _waveform_renderer = FfmpegWaveformRenderer(
            ffmpeg_path=settings.render_ffmpeg_path,
            ffprobe_path=settings.render_ffprobe_path,
            timeout_seconds=settings.render_timeout_seconds,
        )
    return _waveform_renderer


def _build_enrichers() -> list[Enricher]:
    """The α8.4d derived-preview pipeline (order = thumbnail, preview, gif, waveform)."""
    settings = _get_settings()
    return [
        ThumbnailEnricher(_get_thumbnailer(), at_seconds=settings.enrichment_thumbnail_at_seconds),
        PreviewEnricher(
            _get_preview_clipper(),
            max_seconds=settings.enrichment_preview_max_seconds,
            max_width=settings.enrichment_preview_max_width,
        ),
        GifEnricher(
            _get_gif_previewer(),
            max_seconds=settings.enrichment_gif_max_seconds,
            fps=settings.enrichment_gif_fps,
            max_width=settings.enrichment_gif_max_width,
        ),
        WaveformEnricher(
            _get_waveform_renderer(),
            width=settings.enrichment_waveform_width,
            height=settings.enrichment_waveform_height,
        ),
    ]


def get_enrich_generated_media_use_case() -> EnrichGeneratedMedia:
    """Factory: the α8.4c/d derived-preview enrichment pipeline (fresh UoW per call)."""
    settings = _get_settings()
    return EnrichGeneratedMedia(
        get_unit_of_work(),
        _get_object_storage(),
        _build_enrichers(),
        workspace_dir=settings.render_workspace_dir,
        lease=timedelta(seconds=settings.render_timeout_seconds),
    )


def get_media_enrichment_worker() -> MediaEnrichmentWorker:
    """Factory: the α8.4c enrichment poll ingress (``run_once`` enriches videos).

    Symmetric with ``get_render_worker`` — a dedicated poller (Fork B → B2) so FFmpeg
    never runs on the relay path. Pure function of the parent MediaAsset; never reads
    orchestration/render-job history (W8.4c.1 / W8.4c.2 / W8.4c.3).
    """
    settings = _get_settings()
    return MediaEnrichmentWorker(
        uow=get_unit_of_work(),
        enrich=get_enrich_generated_media_use_case(),
        batch_size=settings.enrichment_batch_size,
    )
