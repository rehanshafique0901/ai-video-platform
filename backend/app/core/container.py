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

from app.application.interfaces.catalogue_reader import ICatalogueReader
from app.application.interfaces.clock import IClock
from app.application.interfaces.destination_publisher import IDestinationPublisher
from app.application.interfaces.download_delivery import IDownloadDelivery
from app.application.interfaces.exporter import IExporter
from app.application.interfaces.gif_previewer import IGifPreviewer
from app.application.interfaces.image_feature_extractor import IImageFeatureExtractor
from app.application.interfaces.image_generator import IImageGenerator
from app.application.interfaces.media_downloader import IMediaDownloader
from app.application.interfaces.oauth_state_signer import IOAuthStateSigner
from app.application.interfaces.object_storage import IObjectStorage
from app.application.interfaces.preview_clipper import IPreviewClipper
from app.application.interfaces.provider_dispatcher import ProviderDispatcherPort
from app.application.interfaces.providers import Capability
from app.application.interfaces.publisher import PublisherPort
from app.application.interfaces.renderer import IRenderer
from app.application.interfaces.repositories import IUserRepository
from app.application.interfaces.resolution_ledger import IResolutionLedger
from app.application.interfaces.runtime_state_reader import IRuntimeStateReader
from app.application.interfaces.security import IPasswordHasher, ITokenIssuer
from app.application.interfaces.slideshow_renderer import ISlideshowRenderer
from app.application.interfaces.social_credential_store import ISocialCredentialStore
from app.application.interfaces.social_oauth_client import ISocialOAuthClient
from app.application.interfaces.storage_resolver import IStorageResolver
from app.application.interfaces.thumbnailer import IThumbnailer
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.interfaces.video_probe import IVideoProbe
from app.application.interfaces.waveform_renderer import IWaveformRenderer
from app.application.interfaces.webhook_verifier import IWebhookVerifier
from app.application.use_cases.auth.login_user import LoginUser
from app.application.use_cases.auth.logout_session import LogoutSession
from app.application.use_cases.auth.refresh_session import RefreshSession
from app.application.use_cases.auth.register_user import RegisterUser
from app.application.use_cases.export.create_export_job import CreateExportJob
from app.application.use_cases.export.download_export import DownloadExport
from app.application.use_cases.export.export_worker import ExportWorker
from app.application.use_cases.export.get_export_job import GetExportJob
from app.application.use_cases.export.process_export_job import ProcessExportJob
from app.application.use_cases.generation.capability_resolver import ResolverCapabilityResolver
from app.application.use_cases.generation.generate_video import GenerateVideo
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
from app.application.use_cases.media.promote_generation_assets import PromoteGenerationAssets
from app.application.use_cases.media.register_media import RegisterMedia
from app.application.use_cases.media.update_media import UpdateMedia
from app.application.use_cases.notifications.count_unread_notifications import (
    CountUnreadNotifications,
)
from app.application.use_cases.notifications.create_notification import CreateNotification
from app.application.use_cases.notifications.list_notifications import ListNotifications
from app.application.use_cases.notifications.mark_all_notifications_read import (
    MarkAllNotificationsRead,
)
from app.application.use_cases.notifications.mark_notification_read import (
    MarkNotificationRead,
)
from app.application.use_cases.notifications.notification_projection import (
    NotificationProjection,
)
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
from app.application.use_cases.publishing.complete_social_connection import (
    CompleteSocialConnection,
)
from app.application.use_cases.publishing.create_publish_job import CreatePublishJob
from app.application.use_cases.publishing.get_publish_job import GetPublishJob
from app.application.use_cases.publishing.list_publish_jobs import ListPublishJobs
from app.application.use_cases.publishing.list_social_accounts import ListSocialAccounts
from app.application.use_cases.publishing.process_publish_job import ProcessPublishJob
from app.application.use_cases.publishing.publish_worker import PublishWorker
from app.application.use_cases.publishing.revoke_social_account import RevokeSocialAccount
from app.application.use_cases.publishing.start_social_connection import StartSocialConnection
from app.application.use_cases.relay.relay_service import RelayService
from app.application.use_cases.render.cancel_render_job import CancelRenderJob
from app.application.use_cases.render.create_render_job import CreateRenderJob
from app.application.use_cases.render.get_render_job import GetRenderJob
from app.application.use_cases.render.list_render_jobs import ListRenderJobs
from app.application.use_cases.render.process_render_job import ProcessRenderJob
from app.application.use_cases.render.render_worker import RenderWorker
from app.application.use_cases.resolver.resolver_service import ResolverService
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
from app.infrastructure.delivery import (
    DeliveryResolver,
    LocalStreamDelivery,
    S3RedirectDelivery,
)
from app.infrastructure.export import FfmpegExporter
from app.infrastructure.generation.execution_runtime_store import SqlExecutionRuntimeStore
from app.infrastructure.generation.generation_reader import GenerationReader
from app.infrastructure.generation.model_cache_manager import ModelCacheManager
from app.infrastructure.generation.pillow_feature_extractor import PillowFeatureExtractor
from app.infrastructure.generation.pollinations_image_generator import PollinationsImageGenerator
from app.infrastructure.media import HttpMediaDownloader
from app.infrastructure.publisher.in_process_publisher import InProcessPublisher
from app.infrastructure.publishing.credentials.credential_service import SocialCredentialService
from app.infrastructure.publishing.credentials.envelope import EnvelopeCipher
from app.infrastructure.publishing.credentials.master_key import (
    EnvMasterKeyProvider,
    IMasterKeyProvider,
)
from app.infrastructure.publishing.destinations.mock_destination import MockDestination
from app.infrastructure.publishing.destinations.registry import DestinationRegistry
from app.infrastructure.publishing.destinations.youtube import YouTubeDestination
from app.infrastructure.publishing.oauth.mock_oauth_client import MockSocialOAuthClient
from app.infrastructure.publishing.oauth.youtube_oauth_client import YouTubeOAuthClient
from app.infrastructure.publishing.state_token_signer import JwtOAuthStateSigner
from app.infrastructure.render import (
    FfmpegGifPreviewer,
    FfmpegPreviewClipper,
    FfmpegRenderer,
    FfmpegThumbnailer,
    FfmpegWaveformRenderer,
)
from app.infrastructure.render.ffmpeg_slideshow_renderer import FfmpegSlideshowRenderer
from app.infrastructure.render.ffprobe_video_probe import FfprobeVideoProbe
from app.infrastructure.repositories.catalogue_reader import CatalogueReader
from app.infrastructure.repositories.resolution_ledger_writer import ResolutionLedgerWriter
from app.infrastructure.repositories.runtime_state_reader import RuntimeStateReader
from app.infrastructure.repositories.user_repository import UserRepository
from app.infrastructure.security.jwt import JWTService
from app.infrastructure.security.password_hasher import PasswordHasher
from app.infrastructure.security.token_issuer import AuthTokenIssuer
from app.infrastructure.storage import (
    LocalObjectStorage,
    S3ObjectStorage,
    StorageResolver,
    build_s3_client,
)
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
_exporter: IExporter | None = None
# α8.5b.1: the download-delivery seam (local streaming adapter; cloud/redirect adapters α8.5b.2).
# In α8.5b.2 ``_download_delivery`` holds the backend-dispatching ``DeliveryResolver`` facade.
_download_delivery: IDownloadDelivery | None = None
# α8.5b.2: multi-backend storage. ``_storage_resolver`` selects the write/read adapter per
# backend (E2). When ``storage_active_backend`` is s3/r2, one shared S3-compatible client backs
# both the cloud object-storage adapter and the cloud redirect-delivery adapter; ``_cloud_bundle``
# memoises them as a unit (``_cloud_bundle_built`` disambiguates "not built" from "local-only").
_storage_resolver: IStorageResolver | None = None
_cloud_bundle: tuple[str, IObjectStorage, IDownloadDelivery] | None = None
_cloud_bundle_built: bool = False
_thumbnailer: IThumbnailer | None = None
_preview_clipper: IPreviewClipper | None = None
_gif_previewer: IGifPreviewer | None = None
_waveform_renderer: IWaveformRenderer | None = None
_media_download_client: httpx.AsyncClient | None = None
# α8.6 Increment 3: generation-runtime adapters. The Pollinations image generator
# owns a dedicated httpx client (closed in shutdown()); the Pillow extractor,
# slideshow renderer and ffprobe probe are stateless. All built lazily so the
# common test path opens no HTTP client and shells out to no binary at import.
_image_generator: IImageGenerator | None = None
_image_client: httpx.AsyncClient | None = None
_feature_extractor: IImageFeatureExtractor | None = None
_slideshow_renderer: ISlideshowRenderer | None = None
_video_probe: IVideoProbe | None = None
# α8.6b: the publish-runtime destination registry (Mock in α8.6b; YouTube in α8.6c).
_destination_registry: DestinationRegistry | None = None
# α8.6c: one shared httpx client for the real YouTube leaves (OAuth client + destination),
# built lazily iff YouTube is configured and closed in shutdown(). None on the common
# (unconfigured) path, so tests open no client they must close.
_youtube_client: httpx.AsyncClient | None = None
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
    # α8.6a fail-closed (ADR-0047 C2): production must hold a publishing master key or refuse
    # to boot. There is no auto-generation and no plaintext fallback — a publishing credential
    # is either securely available or the capability is explicitly unavailable.
    if settings.environment == "prod" and settings.publishing_credential_master_key is None:
        raise RuntimeError(
            "publishing_credential_master_key is required in production "
            "(fail-closed — no plaintext fallback, no auto-generation)"
        )
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
    # untouched). α8.5b.3 registers a SECOND, fully independent consumer — the
    # notification projection for ``ExportJobSucceeded`` / ``ExportJobFailed`` — on the
    # same event stream, demonstrating the platform's fan-out seam (the runner knows
    # nothing about either consumer). A broker-backed publisher can replace this later
    # identically. Each consumer holds a *factory* (not an instance) so every delivery
    # runs in its own fresh use case + Unit of Work.
    _publisher = InProcessPublisher(
        [
            GeneratedMediaIngestionSubscriber(get_ingest_generated_media_use_case),
            NotificationProjection(get_create_notification_use_case),
        ]
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
    global _preview_clipper, _gif_previewer, _waveform_renderer, _exporter, _download_delivery
    global _storage_resolver, _cloud_bundle, _cloud_bundle_built
    global _image_generator, _image_client, _feature_extractor, _slideshow_renderer, _video_probe
    global _destination_registry, _youtube_client
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
    if _image_client is not None:
        await _image_client.aclose()
    if _youtube_client is not None:
        await _youtube_client.aclose()
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
    _exporter = None
    _thumbnailer = None
    _preview_clipper = None
    _gif_previewer = None
    _waveform_renderer = None
    _download_delivery = None
    _storage_resolver = None
    _cloud_bundle = None
    _cloud_bundle_built = False
    _image_generator = None
    _image_client = None
    _feature_extractor = None
    _slideshow_renderer = None
    _video_probe = None
    _destination_registry = None
    _youtube_client = None
    _settings = None


def reset() -> None:
    """Test-only: clear all singletons so the next ``init`` rebuilds them."""
    global _engine, _session_factory, _password_hasher, _jwt_service
    global _token_issuer, _dummy_password_hash, _clock, _publisher
    global _provider_registry, _openai_client, _fal_client, _settings
    global _fal_webhook_verifier, _fal_webhook_client
    global _object_storage, _media_downloader, _media_download_client, _renderer, _thumbnailer
    global _preview_clipper, _gif_previewer, _waveform_renderer, _exporter, _download_delivery
    global _storage_resolver, _cloud_bundle, _cloud_bundle_built
    global _image_generator, _image_client, _feature_extractor, _slideshow_renderer, _video_probe
    global _destination_registry, _youtube_client
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
    _exporter = None
    _download_delivery = None
    _storage_resolver = None
    _cloud_bundle = None
    _cloud_bundle_built = False
    _image_generator = None
    _image_client = None
    _feature_extractor = None
    _slideshow_renderer = None
    _video_probe = None
    _destination_registry = None
    _youtube_client = None


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


def get_catalogue_reader(session: AsyncSession) -> ICatalogueReader:
    """Factory: a read-only ``CatalogueReader`` over the supplied session (α8.5e.3)."""
    return CatalogueReader(session)


def get_runtime_state_reader(session: AsyncSession) -> IRuntimeStateReader:
    """Factory: a read-only ``RuntimeStateReader`` over the supplied session (α8.5e.4)."""
    return RuntimeStateReader(session)


def get_resolver_service(session: AsyncSession) -> ResolverService:
    """Factory: a ``ResolverService`` (catalogue + runtime readers) over the session (α8.5e.5)."""
    return ResolverService(CatalogueReader(session), RuntimeStateReader(session))


def get_resolution_ledger(session: AsyncSession) -> IResolutionLedger:
    """Factory: the resolution-ledger writer over the supplied session (α8.5e.5)."""
    return ResolutionLedgerWriter(session)


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


def get_promote_generation_assets_use_case() -> PromoteGenerationAssets:
    """Factory: the α8.8 Asset Promotion Bridge (fresh UoW per call).

    Bridges the execution plane (``generation_assets``) to the media library
    (``media_assets``) — the ADR-0046 X8 seam. Reads the generation through the
    read-only :class:`GenerationReader` (its own short session, like the Execution
    Runtime store), copies the finished bytes via the storage resolver, and registers
    the owned media asset via the UoW. The execution runtime is untouched.
    """
    return PromoteGenerationAssets(
        uow=get_unit_of_work(),
        storage=_get_storage_resolver(),
        reader=GenerationReader(get_session_factory()),
    )


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
# Use-case factories (Slice α8.5a — Export engine, owner-facing)
# ---------------------------------------------------------------------


def get_create_export_job_use_case() -> CreateExportJob:
    return CreateExportJob(uow=get_unit_of_work())


def get_get_export_job_use_case() -> GetExportJob:
    return GetExportJob(uow=get_unit_of_work())


def _get_download_delivery() -> IDownloadDelivery:
    """Lazily build + memoise the download-delivery facade (α8.5b.2 — ``DeliveryResolver``).

    Registers ``local`` → :class:`LocalStreamDelivery` (200 stream) always + the active cloud
    backend → :class:`S3RedirectDelivery` (302 presigned redirect) when configured. The resolver
    *is* an ``IDownloadDelivery``, so :class:`DownloadExport` and the download endpoint are
    unchanged (Ruling A + E); delivery selection is a pure function of the artifact's persisted
    backend (W8.5b.4).
    """
    global _download_delivery
    _require_init()
    if _download_delivery is None:
        adapters: dict[str, IDownloadDelivery] = {
            "local": LocalStreamDelivery(_get_object_storage())
        }
        bundle = _get_cloud_bundle()
        if bundle is not None:
            backend, _, delivery = bundle
            adapters[backend] = delivery
        _download_delivery = DeliveryResolver(adapters=adapters)
    return _download_delivery


def get_download_export_use_case() -> DownloadExport:
    """Factory: the α8.5b.1 owner-facing export download use case (fresh UoW per call)."""
    return DownloadExport(uow=get_unit_of_work(), delivery=_get_download_delivery())


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
    """Lazily build + memoise the *local* object-storage adapter (local FS, α8.4a).

    Always present — the local backend anchors the resolver (α8.5b.2) and backs the local
    streaming delivery adapter — even when ``storage_active_backend`` is a cloud backend.
    """
    global _object_storage
    _require_init()
    if _object_storage is None:
        settings = _get_settings()
        _object_storage = LocalObjectStorage(
            root=settings.media_storage_root, bucket=settings.media_storage_bucket
        )
    return _object_storage


def _get_cloud_bundle() -> tuple[str, IObjectStorage, IDownloadDelivery] | None:
    """Build + memoise the S3/R2 adapter pair when a cloud backend is active (α8.5b.2).

    Returns ``(backend, storage, delivery)`` sharing **one** S3-compatible client, or ``None``
    when ``storage_active_backend`` is ``local`` (local-only deployment). Credentials are
    injected (W8.1.1); a cloud backend with missing bucket/credentials is a hard config error.
    """
    global _cloud_bundle, _cloud_bundle_built
    _require_init()
    if _cloud_bundle_built:
        return _cloud_bundle
    settings = _get_settings()
    backend = settings.storage_active_backend
    if backend not in ("s3", "r2"):
        _cloud_bundle = None
        _cloud_bundle_built = True
        return None
    if (
        not settings.s3_bucket
        or not settings.s3_access_key_id
        or settings.s3_secret_access_key is None
    ):
        raise RuntimeError(
            f"storage_active_backend={backend!r} requires s3_bucket + s3_access_key_id + "
            "s3_secret_access_key to be configured"
        )
    client = build_s3_client(
        region=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key.get_secret_value(),
    )
    storage: IObjectStorage = S3ObjectStorage(
        backend=backend, bucket=settings.s3_bucket, client=client
    )
    delivery: IDownloadDelivery = S3RedirectDelivery(
        backend=backend,
        bucket=settings.s3_bucket,
        client=client,
        ttl_seconds=settings.download_signed_url_ttl_seconds,
    )
    _cloud_bundle = (backend, storage, delivery)
    _cloud_bundle_built = True
    return _cloud_bundle


def _get_storage_resolver() -> IStorageResolver:
    """Lazily build + memoise the multi-backend storage resolver (α8.5b.2 — E2).

    Registers ``local`` always + the active cloud backend when configured. ``active()`` is the
    single configured write backend; reads resolve by an artifact's persisted backend (W8.5b.4 /
    W8.5b.5). No use case is backend-aware — the resolver centralises selection (Ruling A).
    """
    global _storage_resolver
    _require_init()
    if _storage_resolver is None:
        settings = _get_settings()
        adapters: dict[str, IObjectStorage] = {"local": _get_object_storage()}
        bundle = _get_cloud_bundle()
        if bundle is not None:
            backend, storage, _ = bundle
            adapters[backend] = storage
        _storage_resolver = StorageResolver(
            adapters=adapters, active_backend=settings.storage_active_backend
        )
    return _storage_resolver


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
        storage=_get_storage_resolver(),
        downloader=_get_media_downloader(),
    )


# ---------------------------------------------------------------------
# Use-case factories (Slice α8.5b.3 — Notification projection)
# ---------------------------------------------------------------------


def get_create_notification_use_case() -> CreateNotification:
    """Factory: the α8.5b.3 idempotent notification write (fresh UoW per call).

    Invoked by ``NotificationProjection`` once per delivered ``ExportJobSucceeded`` /
    ``ExportJobFailed`` event, so each projection runs in its own Unit of Work. The use
    case is strictly downstream of — and never mutates — the frozen export/orchestration
    pipeline (W8.5b.6); exactly-once is enforced by the DB (W8.5b.7).
    """
    return CreateNotification(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α8.5b.3r — Notification read API)
# ---------------------------------------------------------------------
# The owner-facing read/query half of the notifications context. Each is a query-only
# (or metadata-only mutation) use case over a fresh UoW — never touches the frozen
# export/orchestration pipeline (Gate 1) nor the write projection (Gate 2).


def get_list_notifications_use_case() -> ListNotifications:
    return ListNotifications(uow=get_unit_of_work())


def get_count_unread_notifications_use_case() -> CountUnreadNotifications:
    return CountUnreadNotifications(uow=get_unit_of_work())


def get_mark_notification_read_use_case() -> MarkNotificationRead:
    return MarkNotificationRead(uow=get_unit_of_work())


def get_mark_all_notifications_read_use_case() -> MarkAllNotificationsRead:
    return MarkAllNotificationsRead(uow=get_unit_of_work())


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
        storage=_get_storage_resolver(),
        renderer=_get_renderer(),
        workspace_dir=settings.render_workspace_dir,
        lease=timedelta(seconds=settings.render_timeout_seconds),
    )


# ---------------------------------------------------------------------
# Generation-runtime adapters + use case (Slice α8.6 — Increment 3)
# ---------------------------------------------------------------------
# The image provider + the slideshow renderer + the Pillow extractor + the ffprobe
# probe, composed with the capability resolver into ``GenerateVideo``. All adapters
# are configuration-blind (W8.1.1); the resolver + readers are session-scoped, so
# the use-case factory takes an ``AsyncSession``. No provider-specific branching
# reaches the use case (ADR-0045) — it asks for a capability and executes the best
# eligible candidate.


def _get_image_generator() -> IImageGenerator:
    """Lazily build + memoise the Pollinations image generator + its HTTP client."""
    global _image_generator, _image_client
    _require_init()
    if _image_generator is None:
        settings = _get_settings()
        _image_client = httpx.AsyncClient(
            base_url=settings.pollinations_base_url,
            timeout=settings.pollinations_timeout_seconds,
            follow_redirects=True,
        )
        _image_generator = PollinationsImageGenerator(
            client=_image_client, model=settings.pollinations_model
        )
    return _image_generator


def _get_feature_extractor() -> IImageFeatureExtractor:
    """Lazily build + memoise the Pillow feature extractor (stateless)."""
    global _feature_extractor
    _require_init()
    if _feature_extractor is None:
        _feature_extractor = PillowFeatureExtractor()
    return _feature_extractor


def _get_slideshow_renderer() -> ISlideshowRenderer:
    """Lazily build + memoise the ffmpeg slideshow renderer (configuration-blind)."""
    global _slideshow_renderer
    _require_init()
    if _slideshow_renderer is None:
        settings = _get_settings()
        _slideshow_renderer = FfmpegSlideshowRenderer(
            ffmpeg_path=settings.render_ffmpeg_path,
            timeout_seconds=settings.render_timeout_seconds,
        )
    return _slideshow_renderer


def _get_video_probe() -> IVideoProbe:
    """Lazily build + memoise the ffprobe video probe (configuration-blind)."""
    global _video_probe
    _require_init()
    if _video_probe is None:
        settings = _get_settings()
        _video_probe = FfprobeVideoProbe(ffprobe_path=settings.render_ffprobe_path)
    return _video_probe


def get_generate_video_use_case(session: AsyncSession) -> GenerateVideo:
    """Factory: the α8.6 end-to-end generation use case over the supplied session.

    The capability resolver reads the catalogue + runtime snapshots through the
    request ``session``; the image generator / extractor / renderer / probe are
    process-wide (memoised). The persistent Execution Runtime store + model cache
    manager use their own short-lived sessions (generation is long-running, so no
    single transaction spans the run — Increment 4 / ADR-0046). The Model Cache has
    no downloader until Increment 6; it resolves already-registered local models.
    """
    resolver = ResolverCapabilityResolver(CatalogueReader(session), RuntimeStateReader(session))
    session_factory = get_session_factory()
    return GenerateVideo(
        resolver=resolver,
        image_generator=_get_image_generator(),
        feature_extractor=_get_feature_extractor(),
        renderer=_get_slideshow_renderer(),
        video_probe=_get_video_probe(),
        storage=_get_object_storage(),
        model_manager=ModelCacheManager(session_factory),
        store=SqlExecutionRuntimeStore(session_factory),
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
# Use-case factories (Slice α8.5a — Export engine, worker path)
# ---------------------------------------------------------------------


def _get_exporter() -> IExporter:
    """Lazily build + memoise the FFmpeg exporter (configuration-blind, W8.1.1).

    Reuses the render binary + timeout config (same ffmpeg/ffprobe) — export is a distinct
    domain (Fork C) but shares the platform's subprocess plumbing, mirroring the enrichment
    adapters.
    """
    global _exporter
    _require_init()
    if _exporter is None:
        settings = _get_settings()
        _exporter = FfmpegExporter(
            ffmpeg_path=settings.render_ffmpeg_path,
            ffprobe_path=settings.render_ffprobe_path,
            timeout_seconds=settings.render_timeout_seconds,
        )
    return _exporter


def get_process_export_job_use_case() -> ProcessExportJob:
    """Factory: the α8.5a single-job export use case (fresh UoW per call)."""
    settings = _get_settings()
    return ProcessExportJob(
        uow=get_unit_of_work(),
        storage=_get_storage_resolver(),
        exporter=_get_exporter(),
        workspace_dir=settings.render_workspace_dir,
        lease=timedelta(seconds=settings.render_timeout_seconds),
    )


def get_export_worker() -> ExportWorker:
    """Factory: the α8.5a export poll ingress (``run_once`` drains queued export jobs).

    Mirrors ``get_render_worker`` — a dedicated poller (Fork B) so CPU-bound transcoding
    stays off the relay fan-out. Export is downstream, delivery-only (W8.5.1/W8.5.2).
    """
    settings = _get_settings()
    return ExportWorker(
        uow=get_unit_of_work(),
        process=get_process_export_job_use_case(),
        batch_size=settings.export_batch_size,
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
        _get_storage_resolver(),
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


# ---------------------------------------------------------------------
# Publishing — account connections (Slice α8.6a, ADR-0047)
# ---------------------------------------------------------------------
# The credential-ownership boundary + the connection lifecycle. The master key is injected
# (never fetched by adapters — W8.1.1); the credential service is the sole decryptor (C7).
# α8.6a wires only the deterministic Mock OAuth client (OQ1); the real YouTube client lands
# in α8.6c. Publishing is FAIL-CLOSED: without a master key the credential service cannot be
# built and every connection endpoint errors (never a plaintext fallback).


_SOCIAL_CALLBACK_PATH = "/api/v1/social-accounts/callback"


def _get_master_key_provider() -> IMasterKeyProvider:
    """Build the envelope master-key provider, or fail closed if no key is configured."""
    settings = _get_settings()
    key = settings.publishing_credential_master_key
    if key is None:
        raise RuntimeError(
            "publishing master key is not configured — publishing is unavailable "
            "(set publishing_credential_master_key)"
        )
    return EnvMasterKeyProvider(
        version=settings.publishing_credential_key_version,
        secret=key.get_secret_value(),
    )


def _get_youtube_http_client() -> httpx.AsyncClient:
    """Lazily build + memoise the shared httpx client for the YouTube leaves (α8.6c).

    Built on first use (only when YouTube is configured), so the common path opens no client
    it must close; disposed in ``shutdown()``. Both the OAuth client and the destination
    adapter share it. Configuration-blind: it holds no credential (the bearer/secret is
    passed per-request by the leaves).
    """
    global _youtube_client
    if _youtube_client is None:
        settings = _get_settings()
        _youtube_client = httpx.AsyncClient(timeout=settings.youtube_timeout_seconds)
    return _youtube_client


def _build_youtube_oauth_client() -> YouTubeOAuthClient | None:
    """The real YouTube OAuth client, or ``None`` when YouTube is unconfigured (fail-soft)."""
    settings = _get_settings()
    client_id = settings.youtube_oauth_client_id
    client_secret = settings.youtube_oauth_client_secret
    if client_id is None or client_secret is None:
        return None
    return YouTubeOAuthClient(
        http=_get_youtube_http_client(),
        client_id=client_id,
        client_secret=client_secret.get_secret_value(),
        clock=get_clock(),
        scopes=settings.youtube_oauth_scopes,
        authorize_url=settings.youtube_oauth_authorize_url,
        token_url=settings.youtube_oauth_token_url,
        revoke_url=settings.youtube_oauth_revoke_url,
        api_base_url=settings.youtube_api_base_url,
    )


def _get_oauth_clients() -> dict[str, ISocialOAuthClient]:
    """The per-platform OAuth clients (Mock always; YouTube iff configured — α8.6c, fail-soft)."""
    clients: dict[str, ISocialOAuthClient] = {"mock": MockSocialOAuthClient(clock=get_clock())}
    youtube = _build_youtube_oauth_client()
    if youtube is not None:
        clients["youtube"] = youtube
    return clients


def _get_oauth_state_signer() -> IOAuthStateSigner:
    """The signed, stateless OAuth state signer (reuses the JWT signing key + algorithm)."""
    settings = _get_settings()
    return JwtOAuthStateSigner(
        secret=settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
        ttl_seconds=settings.publishing_oauth_state_ttl_seconds,
        clock=get_clock(),
    )


def _get_social_redirect_uri() -> str:
    settings = _get_settings()
    return settings.publishing_oauth_redirect_base_url.rstrip("/") + _SOCIAL_CALLBACK_PATH


def get_social_credential_store() -> ISocialCredentialStore:
    """The credential service (sole decryptor). Raises if the master key is unavailable."""
    cipher = EnvelopeCipher(_get_master_key_provider())
    return SocialCredentialService(
        session_factory=get_session_factory(),
        cipher=cipher,
        oauth_clients=_get_oauth_clients(),
        clock=get_clock(),
    )


def get_start_social_connection_use_case() -> StartSocialConnection:
    return StartSocialConnection(
        oauth_clients=_get_oauth_clients(),
        state_signer=_get_oauth_state_signer(),
        redirect_uri=_get_social_redirect_uri(),
    )


def get_complete_social_connection_use_case() -> CompleteSocialConnection:
    return CompleteSocialConnection(
        uow=get_unit_of_work(),
        oauth_clients=_get_oauth_clients(),
        state_signer=_get_oauth_state_signer(),
        credential_store=get_social_credential_store(),
        redirect_uri=_get_social_redirect_uri(),
    )


def get_revoke_social_account_use_case() -> RevokeSocialAccount:
    return RevokeSocialAccount(
        uow=get_unit_of_work(),
        credential_store=get_social_credential_store(),
    )


def get_list_social_accounts_use_case() -> ListSocialAccounts:
    return ListSocialAccounts(uow=get_unit_of_work())


# ---------------------------------------------------------------------
# Use-case factories (Slice α8.6b — Publish runtime)
# ---------------------------------------------------------------------


def _get_destination_registry() -> DestinationRegistry:
    """The platform → adapter registry (Mock always; YouTube iff configured — α8.6c).

    Memoised. The Mock adapter is stateless + network-free; the real YouTube adapter is
    registered only when its OAuth credentials are configured (fail-soft — an unconfigured
    ``platform="youtube"`` publish then fails create-time validation, never at runtime). A
    YAML destination catalogue stays deferred until ≥2 real destinations justify it (§14).
    """
    global _destination_registry
    if _destination_registry is None:
        adapters: dict[str, IDestinationPublisher] = {"mock": MockDestination()}
        settings = _get_settings()
        if (
            settings.youtube_oauth_client_id is not None
            and settings.youtube_oauth_client_secret is not None
        ):
            adapters["youtube"] = YouTubeDestination(
                http=_get_youtube_http_client(),
                api_base_url=settings.youtube_api_base_url,
            )
        _destination_registry = DestinationRegistry(adapters)
    return _destination_registry


def get_create_publish_job_use_case() -> CreatePublishJob:
    """Factory: the α8.6b owner-facing create use case (validates against supported platforms)."""
    return CreatePublishJob(
        uow=get_unit_of_work(),
        supported_platforms=_get_destination_registry().supported_platforms(),
    )


def get_get_publish_job_use_case() -> GetPublishJob:
    return GetPublishJob(uow=get_unit_of_work())


def get_list_publish_jobs_use_case() -> ListPublishJobs:
    return ListPublishJobs(uow=get_unit_of_work())


def get_process_publish_job_use_case() -> ProcessPublishJob:
    """Factory: the α8.6b single-job publish use case (fresh UoW per call).

    Credential-blind runtime (PUB-5): it consumes the α8.6a credential service only to obtain
    a short-lived ``AuthorizedContext`` (never key material), and hands adapters that bearer.
    """
    settings = _get_settings()
    return ProcessPublishJob(
        uow=get_unit_of_work(),
        storage=_get_storage_resolver(),
        credential_store=get_social_credential_store(),
        destinations=_get_destination_registry(),
        workspace_dir=settings.render_workspace_dir,
        lease=timedelta(seconds=settings.render_timeout_seconds),
    )


def get_publish_worker() -> PublishWorker:
    """Factory: the α8.6b publish poll ingress (``run_once`` drains due queued publish jobs).

    Mirrors ``get_export_worker`` — a dedicated poller (PUB-7). Publishing is downstream and
    never triggers rendering/export (PUB-6).
    """
    settings = _get_settings()
    return PublishWorker(
        uow=get_unit_of_work(),
        process=get_process_publish_job_use_case(),
        batch_size=settings.publish_batch_size,
    )
