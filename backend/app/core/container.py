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

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.interfaces.clock import IClock
from app.application.interfaces.provider_dispatcher import ProviderDispatcherPort
from app.application.interfaces.publisher import PublisherPort
from app.application.interfaces.repositories import IUserRepository
from app.application.interfaces.security import IPasswordHasher, ITokenIssuer
from app.application.interfaces.unit_of_work import IUnitOfWork
from app.application.use_cases.auth.login_user import LoginUser
from app.application.use_cases.auth.logout_session import LogoutSession
from app.application.use_cases.auth.refresh_session import RefreshSession
from app.application.use_cases.auth.register_user import RegisterUser
from app.application.use_cases.media.delete_media import DeleteMedia
from app.application.use_cases.media.get_media import GetMedia
from app.application.use_cases.media.list_media import ListMedia
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
from app.application.use_cases.users.update_profile import UpdateUserProfile
from app.application.use_cases.versions.branch_version import BranchProjectVersion
from app.application.use_cases.versions.create_version import CreateProjectVersion
from app.application.use_cases.versions.diff_versions import DiffProjectVersions
from app.application.use_cases.versions.get_version import GetProjectVersion
from app.application.use_cases.versions.list_versions import ListProjectVersions
from app.application.use_cases.versions.restore_version import RestoreProjectVersion
from app.application.use_cases.workflow.advance_workflow_run import AdvanceWorkflowRun
from app.application.use_cases.workflow.cancel_workflow_run import CancelWorkflowRun
from app.application.use_cases.workflow.create_workflow_run import CreateWorkflowRun
from app.application.use_cases.workflow.get_workflow_run import GetWorkflowRun
from app.application.use_cases.workflow.list_workflow_runs import ListWorkflowRuns
from app.core.config import Settings
from app.infrastructure.ai.dispatcher import StepCommandDispatcher
from app.infrastructure.ai.providers.registry import PROVIDER_REGISTRY, ProviderRegistry
from app.infrastructure.clock import SystemClock
from app.infrastructure.db.session import make_engine, make_session_factory
from app.infrastructure.publisher.in_process_publisher import InProcessPublisher
from app.infrastructure.repositories.user_repository import UserRepository
from app.infrastructure.security.jwt import JWTService
from app.infrastructure.security.password_hasher import PasswordHasher
from app.infrastructure.security.token_issuer import AuthTokenIssuer
from app.infrastructure.uow.sqlalchemy_unit_of_work import SqlAlchemyUnitOfWork

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_password_hasher: PasswordHasher | None = None
_jwt_service: JWTService | None = None
_token_issuer: AuthTokenIssuer | None = None
_dummy_password_hash: str | None = None
_clock: SystemClock | None = None
_publisher: PublisherPort | None = None


def init(settings: Settings) -> None:
    """Build the process-wide singletons.

    Idempotent: a second call with the container already initialised is
    a no-op. Tests that want to rebuild with different settings should
    call ``reset()`` first.
    """
    global _engine, _session_factory, _password_hasher, _jwt_service
    global _token_issuer, _dummy_password_hash, _clock, _publisher
    if _engine is not None:
        return
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
    # α7.3: the outbox relay's publish target. In-process, zero handlers by
    # default — the relay marks events published after a successful (empty)
    # fan-out. Real consumers / a broker-backed publisher are wired in later
    # slices behind the same ``PublisherPort`` without touching the relay.
    _publisher = InProcessPublisher()


async def shutdown() -> None:
    """Dispose the engine. Called by ``app.main``'s lifespan handler."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def reset() -> None:
    """Test-only: clear all singletons so the next ``init`` rebuilds them."""
    global _engine, _session_factory, _password_hasher, _jwt_service
    global _token_issuer, _dummy_password_hash, _clock, _publisher
    _engine = None
    _session_factory = None
    _password_hasher = None
    _jwt_service = None
    _token_issuer = None
    _dummy_password_hash = None
    _clock = None
    _publisher = None


def _require_init() -> None:
    if _engine is None:
        raise RuntimeError(
            "container not initialised — call init(settings) first "
            "(usually done by app.main.create_app)"
        )


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
    """The process-wide provider registry (α7.4).

    A framework-free singleton wired with the four deterministic mocks. Real
    providers register here in α8.x without changing callers. Stateless w.r.t.
    settings, so it needs no ``init``/``reset`` lifecycle.
    """
    return PROVIDER_REGISTRY


def get_step_command_dispatcher() -> ProviderDispatcherPort:
    """Factory: a ``StepCommandDispatcher`` over the process-wide registry (α7.4).

    Library-only: the α7.2 runner is **not** wired to it in this slice (D3.3); the
    α7.6 pipeline depends on this ``ProviderDispatcherPort`` to turn ``StepCommand``s
    into provider calls.
    """
    return StepCommandDispatcher(get_provider_registry())


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
    return AdvanceWorkflowRun(uow=get_unit_of_work())


def get_cancel_workflow_run_use_case() -> CancelWorkflowRun:
    return CancelWorkflowRun(uow=get_unit_of_work())
