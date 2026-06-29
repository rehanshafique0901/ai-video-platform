# Phase 1 — Architecture, Folder Structure & Tech Decisions

> **Governed by:** [`rule.md`](./rule.md)
> **Status:** REVISION 2 — DRAFT, awaiting user approval before Phase 2.
> **Scope of this document:** High-level architecture, folder layout, technology choices, design patterns, and cross-cutting concerns. **No implementation code.**

### Revision History

| Rev | Date | Changes |
|---|---|---|
| 1 | initial | First architecture draft |
| 2 | rev-2 | Added: (CR-1) AI Provider Plugin System, (CR-2) Multiple Rendering Pipelines, (CR-3) Split AI Orchestration, (CR-4) Event Bus, (CR-5) Multi-storage providers, (CR-6) Versioned Projects, (CR-7) Resumable Workflow Engine, (CR-8) Asset Library, (CR-9) Feature Flags, (CR-10) Explicit Domain Layer. |
| 3 | this revision | Added: (CR-11) AI Model Registry, (CR-12) AI Cost Tracking, (CR-13) Queue Priorities. Created sibling docs: `ROADMAP.md`, `DECISIONS.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `API_CONTRACT.md`. |

---

## 1. System Overview

The platform is a multi-tenant SaaS that turns a user prompt or script into a fully rendered video. It is composed of seven logical planes:

| Plane | Responsibility | Primary Tech |
|---|---|---|
| **Edge / Client** | Next.js app, marketing site, dashboard, timeline editor, real-time preview | Next.js 15, React, TS, Tailwind, ShadCN, Zustand, React Query |
| **API Gateway** | Auth, request validation, rate limiting, routing, OpenAPI surface | FastAPI |
| **Domain Services** | Pure business rules — entities, value objects, invariants | Python (no framework deps) |
| **Application Services** | Use cases that orchestrate domain + infrastructure | FastAPI + SQLAlchemy |
| **AI Orchestration** | Agents, Providers, Prompts, Memory, Tools, Chains, Workflows — each isolated | LangGraph, LangChain, CrewAI, AutoGen |
| **Workflow Engine** | Resumable, checkpointed multi-step pipelines (Pipelines A/B/C…) | LangGraph + Celery + Redis |
| **Event Bus** | Async, decoupled inter-module communication | Redis Streams (pluggable: NATS / Kafka) |
| **Media & Storage** | Image, video, voice generation; FFmpeg rendering; pluggable object storage | FFmpeg, MoviePy, OpenCV, provider SDKs, Local/S3/R2/Azure/GCS |

### 1.1 High-Level Topology

```
                          ┌────────────────────┐
                          │      Browser       │
                          │  Next.js 15 (SSR)  │
                          └─────────┬──────────┘
                                    │ HTTPS / WSS
                                    ▼
                          ┌────────────────────┐
                          │   API Gateway      │
                          │     FastAPI        │
                          │ (JWT, OAuth, RBAC) │
                          └────┬──────┬────────┘
                               │      │
                    ┌──────────┘      └──────────┐
                    ▼                            ▼
          ┌──────────────────┐         ┌──────────────────────┐
          │ Application/     │         │ Workflow Engine      │
          │ Domain Services  │◄───────►│ (LangGraph + Celery) │
          │  (use cases)     │         │ resumable pipelines  │
          └────────┬─────────┘         └──────────┬───────────┘
                   │                              │
                   ▼                              ▼
          ┌────────────────────────────────────────────────────┐
          │                  EVENT BUS                         │
          │              (Redis Streams)                       │
          │  ProjectCreated · ScriptGenerated · SceneCreated   │
          │  PromptGenerated · ImageFinished · VideoFinished   │
          │  RenderStarted · RenderFinished · ExportCompleted  │
          └────┬───────────────┬───────────────┬───────────────┘
               │               │               │
               ▼               ▼               ▼
       ┌────────────┐  ┌────────────────┐  ┌────────────────┐
       │  Workers   │  │  AI Orchestr.  │  │  Notifications │
       │ (Celery)   │  │ Agents/Chains  │  │  Email / WS    │
       └─────┬──────┘  └────────┬───────┘  └────────────────┘
             │                  │
             ▼                  ▼
   ┌──────────────────┐  ┌────────────────────────────────┐
   │  PostgreSQL      │  │  AI Provider Plugin Registry    │
   │  + Alembic       │  │  ┌──────────────────────────┐  │
   │  (Versioned      │  │  │ LLM:  OpenAI / Anthropic │  │
   │   Projects,      │  │  │       Gemini / Local     │  │
   │   Asset Library, │  │  │ IMG:  Flux / SDXL /      │  │
   │   Event Log)     │  │  │       Ideogram / DALL-E /│  │
   └──────────────────┘  │  │       ComfyUI            │  │
                         │  │ VID:  Veo / Runway /     │  │
                         │  │       Pika / Kling /Luma │  │
                         │  │       SVD / ComfyUI      │  │
                         │  │ TTS:  ElevenLabs / XTTS /│  │
                         │  │       OpenAI / Edge /    │  │
                         │  │       Coqui             │  │
                         │  └──────────────────────────┘  │
                         └────────────────┬───────────────┘
                                          │
                                          ▼
                                ┌──────────────────────┐
                                │  Render Workers      │
                                │  (Pipelines A/B/C…)  │
                                │  FFmpeg / MoviePy    │
                                └──────────┬───────────┘
                                           ▼
                                ┌──────────────────────┐
                                │  Storage Provider    │
                                │  Plugin Registry     │
                                │  Local / S3 / R2 /   │
                                │  Azure Blob / GCS    │
                                └──────────────────────┘
```

### 1.2 Cross-cutting Concerns

- **AuthN/AuthZ:** JWT (access + refresh) at gateway; OAuth (Google) federated; RBAC roles (`user`, `pro`, `admin`).
- **Observability:** Structured JSON logs (`structlog`), OpenTelemetry traces, Prometheus metrics, Sentry for errors.
- **Configuration:** 12-factor; `.env` per environment; secrets via Docker secrets / cloud KMS in prod.
- **Async:** Celery + Redis for long-running jobs (generation, rendering). WebSockets for live progress.
- **Idempotency:** All generation jobs keyed by `(project_id, scene_id, provider, params_hash)`.

---

## 2. Architectural Patterns Applied

| Pattern | Where it lives | Why |
|---|---|---|
| **Clean Architecture** | `domain → application → infrastructure → interfaces` layering inside backend | Keeps business rules independent of frameworks |
| **SOLID** | Across services | Small, single-purpose classes |
| **Repository Pattern** | `infrastructure/db/repositories/` | Decouples persistence from domain |
| **Factory Pattern** | `infrastructure/plugins/registry/` | Build provider clients (LLM/image/video/voice/storage) from config |
| **Strategy Pattern** | `ai/providers/` and `rendering/pipelines/` | Swappable AI providers + swappable rendering pipelines (A/B/C…) |
| **Plugin / Provider Pattern (CR-1, CR-5)** | `ai/providers/`, `infrastructure/storage/providers/` | Add new AI vendor or storage backend with one new class |
| **Dependency Injection** | FastAPI `Depends`, frontend `Provider` components | Testability, no globals |
| **Domain-Driven Design (CR-10)** | Explicit `domain/` layer with entities, value objects, aggregates, domain events | Clear ownership; everything else depends on domain |
| **CQRS-lite** | Read models for dashboard analytics, write models for project mutations | Performance |
| **Event-Driven Architecture (CR-4)** | `infrastructure/events/` event bus + domain events | Loose coupling between modules |
| **Workflow / Saga Engine (CR-7)** | `ai/workflows/` LangGraph state machines + checkpointer | Resumable, observable, long-running pipelines |
| **Event Sourcing (lite) / Project Versioning (CR-6)** | `domain/projects/versioning/` | Every edit is a new immutable version (Canva-style) |
| **Feature Toggle (CR-9)** | `core/feature_flags/` (Unleash-compatible interface) | Enable/disable providers, pipelines, UI features without deploy |
| **Registry Pattern (CR-11)** | `infrastructure/ai_models/registry.py` | Discover, version, deprecate, default-select AI models without code edits |
| **Decorator / Middleware (CR-12)** | `infrastructure/ai/middleware/usage_recorder.py` | Single recorder wraps every provider call → immutable usage records |
| **Priority Queues (CR-13)** | `infrastructure/queue/queues.py` + `routing.py` | Five-tier Celery routing with tenant fairness & backpressure |

---

## 3. Top-Level Repository Layout

```
ai creation/
├── rule.md                          # governing requirements (do not modify without approval)
├── ARCHITECTURE.md                  # THIS file
├── ROADMAP.md                       # phases, milestones, sequencing
├── DECISIONS.md                     # ADRs with rationale (one source of truth)
├── CHANGELOG.md                     # one entry per completed phase
├── CONTRIBUTING.md                  # coding standards & contribution workflow
├── API_CONTRACT.md                  # API surface designed BEFORE implementation
├── README.md                        # (Phase 9) top-level overview
├── docker-compose.yml               # (Phase 9) local dev stack
├── docker-compose.prod.yml          # (Phase 9) prod stack
├── .env.example                     # required env vars (no secrets)
├── .github/
│   └── workflows/                   # (Phase 9) CI: lint, test, build, deploy
│       ├── backend.yml
│       ├── frontend.yml
│       └── release.yml
│
├── docs/                            # generated & hand-authored docs
│   ├── architecture/
│   │   ├── system-context.md
│   │   ├── container-diagram.md
│   │   ├── component-diagram.md
│   │   └── adr/                     # Architecture Decision Records
│   │       └── 0001-record-architecture-decisions.md
│   ├── database/
│   │   └── schema.md                # (Phase 2)
│   ├── api/
│   │   └── openapi.yaml             # (Phase 4) generated from FastAPI
│   ├── deployment/
│   │   └── guide.md                 # (Phase 9)
│   └── developer/
│       └── onboarding.md
│
├── infra/                           # (Phase 9)
│   ├── docker/
│   │   ├── backend.Dockerfile
│   │   ├── worker.Dockerfile
│   │   ├── frontend.Dockerfile
│   │   └── nginx.conf
│   ├── k8s/                         # optional kubernetes manifests
│   └── terraform/                   # optional cloud IaC
│
├── backend/                         # Python / FastAPI monorepo
│   └── (see §4)
│
└── frontend/                        # Next.js 15 app
    └── (see §5)
```

---

## 4. Backend Folder Structure (Clean Architecture + DDD)

```
backend/
├── pyproject.toml                   # poetry / uv project file
├── alembic.ini
├── alembic/
│   └── versions/                    # (Phase 2) migrations
│
├── app/
│   ├── main.py                      # FastAPI entrypoint, lifespan, routers
│   │
│   ├── api/                         # ====== INTERFACE / DELIVERY LAYER ======
│   │   ├── v1/
│   │   │   ├── __init__.py
│   │   │   ├── deps.py              # FastAPI dependency providers
│   │   │   ├── routers/
│   │   │   │   ├── auth.py
│   │   │   │   ├── users.py
│   │   │   │   ├── projects.py
│   │   │   │   ├── project_versions.py     # CR-6
│   │   │   │   ├── scenes.py
│   │   │   │   ├── asset_library.py        # CR-8
│   │   │   │   ├── workflows.py            # CR-7 (start / pause / resume)
│   │   │   │   ├── pipelines.py            # CR-2 (list pipelines A/B/C…)
│   │   │   │   ├── ai_script.py
│   │   │   │   ├── ai_storyboard.py
│   │   │   │   ├── ai_image.py
│   │   │   │   ├── ai_video.py
│   │   │   │   ├── ai_voice.py
│   │   │   │   ├── ai_subtitle.py
│   │   │   │   ├── timeline.py
│   │   │   │   ├── render.py
│   │   │   │   ├── export.py
│   │   │   │   ├── billing.py
│   │   │   │   ├── credits.py
│   │   │   │   ├── templates.py
│   │   │   │   ├── analytics.py
│   │   │   │   ├── notifications.py
│   │   │   │   ├── feature_flags.py        # CR-9 (admin only)
│   │   │   │   ├── plugins.py              # CR-1 (list available providers)
│   │   │   │   ├── ai_models.py            # CR-11 (list/refresh/override models)
│   │   │   │   ├── usage.py                # CR-12 (per-user/project usage queries)
│   │   │   │   ├── queues.py               # CR-13 (admin: depth, age, DLQs)
│   │   │   │   └── webhooks.py
│   │   │   └── schemas/             # Pydantic request/response DTOs
│   │   │       ├── auth.py
│   │   │       ├── project.py
│   │   │       ├── project_version.py
│   │   │       ├── workflow.py
│   │   │       ├── pipeline.py
│   │   │       ├── asset.py
│   │   │       └── ...
│   │   └── ws/                      # WebSocket handlers
│   │       ├── progress.py          # job progress streams (subscribes to event bus)
│   │       ├── workflow.py          # CR-7 live workflow state
│   │       └── timeline.py          # collaborative editing (future)
│   │
│   ├── domain/                      # ====== DOMAIN LAYER (CR-10) ======
│   │   │                            # Pure Python. No ORM, no FastAPI, no SDKs.
│   │   ├── shared/
│   │   │   ├── entity.py            # base Entity, AggregateRoot
│   │   │   ├── value_object.py
│   │   │   ├── domain_event.py      # CR-4 base class
│   │   │   ├── exceptions.py
│   │   │   ├── ids.py               # typed IDs (UserId, ProjectId, …)
│   │   │   └── result.py
│   │   ├── identity/
│   │   │   ├── entities.py          # User, Role, Session
│   │   │   ├── value_objects.py     # Email, PasswordHash
│   │   │   ├── events.py            # UserRegistered, EmailVerified
│   │   │   └── policies.py
│   │   ├── projects/                # CR-10 aggregate root
│   │   │   ├── entities.py          # Project (aggregate root), Folder, Tag
│   │   │   ├── versioning/          # CR-6
│   │   │   │   ├── version.py       # ProjectVersion entity (immutable snapshot)
│   │   │   │   ├── diff.py          # version diff value object
│   │   │   │   └── policies.py      # autosave policy, branch policy
│   │   │   ├── value_objects.py     # AspectRatio, Duration, Language, Style
│   │   │   └── events.py            # ProjectCreated, ProjectVersionCreated, …
│   │   ├── scenes/
│   │   │   ├── entities.py          # Scene, Storyboard
│   │   │   ├── value_objects.py     # CameraAngle, Lighting, Weather, Emotion
│   │   │   └── events.py            # SceneCreated, StoryboardGenerated
│   │   ├── prompts/                 # CR-10
│   │   │   ├── entities.py          # Prompt (image/video/voice/style/negative)
│   │   │   ├── value_objects.py     # PromptKind, PromptText
│   │   │   └── events.py            # PromptGenerated
│   │   ├── media/                   # CR-10
│   │   │   ├── entities.py          # Image, Video, Narration, Subtitle,
│   │   │   │                        # Music, SoundEffect, Thumbnail
│   │   │   ├── value_objects.py     # MimeType, Duration, Resolution
│   │   │   └── events.py            # ImageFinished, VideoFinished, …
│   │   ├── timeline/                # CR-10
│   │   │   ├── entities.py          # Timeline, Track, Clip, Transition
│   │   │   ├── value_objects.py     # TimeRange, ZIndex
│   │   │   └── events.py
│   │   ├── render/                  # CR-10
│   │   │   ├── entities.py          # RenderJob, ExportJob
│   │   │   ├── value_objects.py     # ExportFormat, Quality
│   │   │   └── events.py            # RenderStarted, RenderFinished,
│   │   │                            # ExportCompleted
│   │   ├── billing/                 # CR-10
│   │   │   ├── entities.py          # Subscription, Invoice, Plan
│   │   │   ├── value_objects.py     # Money, BillingCycle
│   │   │   └── events.py
│   │   ├── credits/                 # CR-10
│   │   │   ├── entities.py          # CreditLedger, CreditTransaction
│   │   │   ├── value_objects.py     # CreditAmount
│   │   │   └── events.py            # CreditsConsumed, CreditsPurchased
│   │   ├── workflows/               # CR-7 domain
│   │   │   ├── entities.py          # WorkflowRun, WorkflowStep, Checkpoint
│   │   │   ├── value_objects.py     # WorkflowStatus, StepStatus
│   │   │   └── events.py            # WorkflowStarted, StepCompleted,
│   │   │                            # WorkflowPaused, WorkflowResumed
│   │   ├── asset_library/           # CR-8 domain
│   │   │   ├── entities.py          # LibraryAsset, LibraryFolder, AssetTag
│   │   │   ├── value_objects.py     # AssetKind (image|video|music|voice|
│   │   │   │                        # subtitle|prompt|thumbnail)
│   │   │   └── events.py            # AssetAddedToLibrary
│   │   ├── ai_models/               # CR-11 domain
│   │   │   ├── entities.py          # AIModel, PricingTable, ModelOutputLimits
│   │   │   ├── value_objects.py     # ModelStatus, Modality, Capability, ModelId
│   │   │   ├── events.py            # ModelRegistered, ModelDeprecated,
│   │   │   │                        # ModelRetired, ModelAutoUpgraded
│   │   │   └── policies.py          # selection + deprecation policies
│   │   └── usage/                   # CR-12 domain
│   │       ├── entities.py          # UsageRecord (immutable aggregate root)
│   │       ├── value_objects.py     # Money, UsageStatus, PricingTable
│   │       ├── events.py            # UsageRecorded, CostReconciled
│   │       └── policies.py          # cost-estimation strategies per kind
│   │
│   ├── application/                 # ====== APPLICATION / USE CASES ======
│   │   ├── auth/
│   │   │   ├── register_user.py
│   │   │   ├── login_user.py
│   │   │   ├── refresh_token.py
│   │   │   ├── verify_email.py
│   │   │   └── reset_password.py
│   │   ├── projects/
│   │   │   ├── create_project.py
│   │   │   ├── duplicate_project.py
│   │   │   ├── delete_project.py
│   │   │   ├── list_projects.py
│   │   │   ├── autosave_project.py
│   │   │   ├── snapshot_version.py          # CR-6
│   │   │   ├── restore_version.py           # CR-6
│   │   │   └── list_versions.py             # CR-6
│   │   ├── workflows/                       # CR-7
│   │   │   ├── start_workflow.py            # picks Pipeline A/B/C…
│   │   │   ├── pause_workflow.py
│   │   │   ├── resume_workflow.py
│   │   │   ├── cancel_workflow.py
│   │   │   └── get_workflow_status.py
│   │   ├── ai_pipeline/
│   │   │   ├── analyze_script.py
│   │   │   ├── generate_storyboard.py
│   │   │   ├── split_scenes.py
│   │   │   ├── generate_prompts.py
│   │   │   ├── generate_images.py
│   │   │   ├── generate_videos.py
│   │   │   ├── generate_voice.py
│   │   │   ├── generate_subtitles.py
│   │   │   ├── select_music.py
│   │   │   └── render_video.py
│   │   ├── asset_library/                   # CR-8
│   │   │   ├── add_asset_to_library.py
│   │   │   ├── search_library.py
│   │   │   ├── reuse_asset.py
│   │   │   └── tag_asset.py
│   │   ├── billing/
│   │   │   ├── purchase_credits.py
│   │   │   ├── deduct_credits.py
│   │   │   └── subscribe_plan.py
│   │   ├── feature_flags/                   # CR-9
│   │   │   ├── evaluate_flag.py
│   │   │   ├── set_flag.py
│   │   │   └── list_flags.py
│   │   ├── ai_models/                       # CR-11
│   │   │   ├── register_model.py
│   │   │   ├── deprecate_model.py
│   │   │   ├── list_models.py
│   │   │   ├── select_default_model.py
│   │   │   └── upgrade_model.py
│   │   ├── usage/                           # CR-12
│   │   │   ├── record_usage.py              # single recorder use case
│   │   │   ├── reconcile_costs.py
│   │   │   ├── query_usage.py
│   │   │   └── export_usage.py
│   │   ├── event_handlers/                  # CR-4 subscribers
│   │   │   ├── on_image_finished.py         # → adds to library, emits next step
│   │   │   ├── on_video_finished.py
│   │   │   ├── on_render_finished.py
│   │   │   ├── on_export_completed.py
│   │   │   └── on_scene_created.py
│   │   └── interfaces/              # Ports (ABCs) — implemented in infrastructure
│   │       ├── repositories.py
│   │       ├── unit_of_work.py
│   │       ├── event_bus.py                 # CR-4 IEventBus
│   │       ├── feature_flag_provider.py     # CR-9
│   │       ├── workflow_engine.py           # CR-7
│   │       ├── storage_provider.py          # CR-5
│   │       ├── llm_provider.py              # CR-1
│   │       ├── image_provider.py            # CR-1
│   │       ├── video_provider.py            # CR-1
│   │       ├── voice_provider.py            # CR-1
│   │       ├── rendering_pipeline.py        # CR-2
│   │       ├── model_registry.py            # CR-11
│   │       ├── usage_recorder.py            # CR-12
│   │       ├── queue_router.py              # CR-13
│   │       ├── mailer.py
│   │       └── payments.py
│   │
│   ├── ai/                          # ====== AI ORCHESTRATION (CR-3) ======
│   │   │                            # Each subfolder is independently testable.
│   │   ├── agents/                  # autonomous personas (CrewAI / AutoGen)
│   │   │   ├── base.py              # BaseAgent
│   │   │   ├── script_agent.py
│   │   │   ├── analysis_agent.py
│   │   │   ├── storyboard_agent.py
│   │   │   ├── prompt_agent.py
│   │   │   ├── voice_agent.py
│   │   │   ├── subtitle_agent.py
│   │   │   ├── image_agent.py
│   │   │   ├── video_agent.py
│   │   │   ├── render_agent.py
│   │   │   └── seo_agent.py
│   │   ├── providers/               # CR-1 AI provider plugin contracts + impls
│   │   │   ├── base/
│   │   │   │   ├── plugin.py        # BasePlugin (name, version, capabilities,
│   │   │   │   │                    # health_check, cost_per_unit)
│   │   │   │   ├── llm.py           # LLMProvider ABC
│   │   │   │   ├── image.py         # ImageProvider ABC
│   │   │   │   ├── video.py         # VideoProvider ABC
│   │   │   │   └── voice.py         # VoiceProvider ABC
│   │   │   ├── llm/
│   │   │   │   ├── openai.py
│   │   │   │   ├── anthropic.py
│   │   │   │   ├── gemini.py
│   │   │   │   └── local_ollama.py
│   │   │   ├── image/
│   │   │   │   ├── flux.py
│   │   │   │   ├── sdxl.py
│   │   │   │   ├── comfyui.py
│   │   │   │   ├── ideogram.py
│   │   │   │   └── dalle.py
│   │   │   ├── video/
│   │   │   │   ├── google_veo.py
│   │   │   │   ├── runway.py
│   │   │   │   ├── kling.py
│   │   │   │   ├── pika.py
│   │   │   │   ├── luma.py
│   │   │   │   ├── svd.py
│   │   │   │   └── comfyui_video.py
│   │   │   └── voice/
│   │   │       ├── elevenlabs.py
│   │   │       ├── xtts.py
│   │   │       ├── openai_tts.py
│   │   │       ├── edge_tts.py
│   │   │       └── coqui.py
│   │   ├── prompts/                 # versioned prompt templates (jinja2)
│   │   │   ├── registry.py          # PromptRegistry (load by id + version)
│   │   │   ├── script/
│   │   │   │   ├── shorts.v1.j2
│   │   │   │   └── documentary.v1.j2
│   │   │   ├── storyboard/
│   │   │   ├── prompt_engineering/
│   │   │   └── seo/
│   │   ├── memory/                  # agent memory backends
│   │   │   ├── base.py              # MemoryStore ABC
│   │   │   ├── redis_memory.py      # short-term
│   │   │   ├── pgvector_memory.py   # long-term semantic
│   │   │   └── summary_buffer.py    # token-budgeted rolling summary
│   │   ├── tools/                   # tools agents can call
│   │   │   ├── base.py              # BaseTool
│   │   │   ├── web_search.py
│   │   │   ├── image_generate.py    # wraps an ImageProvider
│   │   │   ├── video_generate.py
│   │   │   ├── voice_synth.py
│   │   │   ├── ffmpeg_probe.py
│   │   │   └── storage_put.py
│   │   ├── chains/                  # composable LangChain chains
│   │   │   ├── analyze_chain.py
│   │   │   ├── storyboard_chain.py
│   │   │   ├── prompt_chain.py
│   │   │   └── seo_chain.py
│   │   └── workflows/               # CR-7 LangGraph state machines
│   │       ├── engine.py            # WorkflowEngine (start/pause/resume/cancel)
│   │       ├── checkpointer.py      # Postgres-backed checkpoint store
│   │       ├── base_pipeline.py     # BaseRenderingPipeline (CR-2)
│   │       ├── pipeline_a_stock_footage.py    # CR-2 Pipeline A
│   │       ├── pipeline_b_ai_images_motion.py # CR-2 Pipeline B
│   │       ├── pipeline_c_ai_video_clips.py   # CR-2 Pipeline C
│   │       └── registry.py          # PipelineRegistry (lookup by id)
│   │
│   ├── infrastructure/              # ====== INFRA / ADAPTERS ======
│   │   ├── db/
│   │   │   ├── base.py              # SQLAlchemy Declarative Base
│   │   │   ├── session.py
│   │   │   ├── models/              # ORM models (separate from domain entities)
│   │   │   │   ├── user.py
│   │   │   │   ├── project.py
│   │   │   │   ├── project_version.py        # CR-6
│   │   │   │   ├── scene.py
│   │   │   │   ├── prompt.py
│   │   │   │   ├── media_asset.py
│   │   │   │   ├── library_asset.py          # CR-8
│   │   │   │   ├── timeline.py
│   │   │   │   ├── render_job.py
│   │   │   │   ├── export_job.py
│   │   │   │   ├── workflow_run.py           # CR-7
│   │   │   │   ├── workflow_checkpoint.py    # CR-7
│   │   │   │   ├── event_log.py              # CR-4 (audit + replay)
│   │   │   │   ├── event_outbox.py           # CR-4 (transactional outbox)
│   │   │   │   ├── feature_flag.py           # CR-9
│   │   │   │   ├── ai_model.py               # CR-11
│   │   │   │   ├── usage_record.py           # CR-12
│   │   │   │   ├── cost_reconciliation.py    # CR-12
│   │   │   │   ├── template.py
│   │   │   │   ├── credit_ledger.py
│   │   │   │   ├── invoice.py
│   │   │   │   ├── log.py
│   │   │   │   ├── settings.py
│   │   │   │   ├── analytics_event.py
│   │   │   │   └── notification.py
│   │   │   └── repositories/
│   │   │       ├── user_repository.py
│   │   │       ├── project_repository.py
│   │   │       ├── project_version_repository.py
│   │   │       ├── library_asset_repository.py
│   │   │       ├── workflow_repository.py
│   │   │       └── ...
│   │   ├── events/                  # ====== EVENT BUS (CR-4) ======
│   │   │   ├── base.py              # IEventBus, Event envelope schema
│   │   │   ├── redis_streams_bus.py # default impl
│   │   │   ├── nats_bus.py          # alt impl (pluggable)
│   │   │   ├── kafka_bus.py         # alt impl (pluggable)
│   │   │   ├── in_memory_bus.py     # tests
│   │   │   ├── dispatcher.py        # routes events → handlers
│   │   │   ├── outbox.py            # transactional outbox pattern
│   │   │   └── topics.py            # canonical topic/event name registry
│   │   ├── storage/                 # ====== STORAGE PLUGINS (CR-5) ======
│   │   │   ├── base.py              # StorageProvider ABC
│   │   │   ├── registry.py          # StorageRegistry (factory)
│   │   │   └── providers/
│   │   │       ├── local.py
│   │   │       ├── s3.py
│   │   │       ├── r2.py
│   │   │       ├── azure_blob.py
│   │   │       └── gcs.py
│   │   ├── cache/
│   │   │   └── redis_client.py
│   │   ├── queue/                            # CR-13 priority queues
│   │   │   ├── celery_app.py
│   │   │   ├── queues.py                     # canonical queue names + routing
│   │   │   ├── routing.py                    # queue selection per task + caller
│   │   │   ├── policies/
│   │   │   │   ├── tier_policy.py
│   │   │   │   ├── rate_limit_policy.py
│   │   │   │   └── overload_policy.py
│   │   │   └── tasks/
│   │   │       ├── workflow_tasks.py         # CR-7
│   │   │       ├── ai_tasks.py
│   │   │       ├── render_tasks.py
│   │   │       ├── email_tasks.py
│   │   │       └── reconcile_tasks.py        # CR-12 cost reconciliation
│   │   ├── rendering/               # rendering primitives (engines, not pipelines)
│   │   │   ├── ffmpeg_engine.py
│   │   │   ├── moviepy_engine.py
│   │   │   ├── transitions.py
│   │   │   ├── subtitle_burner.py
│   │   │   ├── stock_footage_client.py       # CR-2 Pipeline A dep
│   │   │   └── exporters/
│   │   │       ├── mp4_exporter.py
│   │   │       ├── mov_exporter.py
│   │   │       ├── gif_exporter.py
│   │   │       └── webm_exporter.py
│   │   ├── feature_flags/                   # CR-9
│   │   │   ├── base.py              # FeatureFlagProvider ABC
│   │   │   ├── db_flag_provider.py  # default: Postgres-backed
│   │   │   ├── unleash_provider.py  # optional: Unleash adapter
│   │   │   └── env_flag_provider.py # dev/tests
│   │   ├── plugins/                 # plugin discovery + registry (CR-1, CR-5)
│   │   │   ├── registry.py          # discover + register at startup
│   │   │   ├── loader.py            # import all entries via entry_points
│   │   │   └── manifest.py          # plugin metadata schema
│   │   ├── ai_models/                       # CR-11 model registry
│   │   │   ├── registry.py
│   │   │   ├── discovery.py                 # calls provider.list_models()
│   │   │   └── seed/
│   │   │       └── builtin_models.yaml      # bootstrap catalogue
│   │   ├── ai/
│   │   │   └── middleware/                  # CR-12
│   │   │       └── usage_recorder.py        # wraps every provider call
│   │   ├── mail/
│   │   │   └── smtp_mailer.py
│   │   ├── payments/
│   │   │   └── stripe_client.py
│   │   └── security/
│   │       ├── jwt.py
│   │       ├── oauth_google.py
│   │       └── password_hasher.py
│   │
│   ├── core/                        # ====== FRAMEWORK GLUE ======
│   │   ├── config.py                # pydantic-settings (all env vars)
│   │   ├── logging.py               # structlog setup
│   │   ├── telemetry.py             # OTel setup
│   │   ├── errors.py                # global exception handlers
│   │   ├── middleware.py            # request id, CORS, rate-limit
│   │   └── container.py             # DI container wiring (binds ABCs → impls,
│   │                                # consults feature flags + plugin registry)
│   │
│   └── workers/
│       ├── workflow_worker.py       # CR-7 Celery worker for workflow steps
│       ├── render_worker.py         # standalone celery render worker entry
│       ├── ai_worker.py             # standalone celery AI worker entry
│       └── event_worker.py          # CR-4 dispatches events from bus → handlers
│
└── tests/
    ├── unit/
    ├── integration/
    ├── e2e/
    └── conftest.py
```

---

## 5. Frontend Folder Structure (Next.js 15 App Router)

```
frontend/
├── package.json
├── tsconfig.json
├── next.config.ts
├── tailwind.config.ts
├── postcss.config.js
├── .env.local.example
│
├── public/
│   ├── fonts/
│   └── images/
│
└── src/
    ├── app/                         # Next.js 15 App Router
    │   ├── layout.tsx
    │   ├── page.tsx                 # marketing landing
    │   ├── (marketing)/
    │   │   ├── pricing/page.tsx
    │   │   ├── features/page.tsx
    │   │   └── blog/[slug]/page.tsx
    │   ├── (auth)/
    │   │   ├── login/page.tsx
    │   │   ├── register/page.tsx
    │   │   ├── verify-email/page.tsx
    │   │   └── reset-password/page.tsx
    │   ├── (app)/                   # authenticated app
    │   │   ├── layout.tsx           # sidebar + topbar shell
    │   │   ├── dashboard/page.tsx
    │   │   ├── projects/
    │   │   │   ├── page.tsx
    │   │   │   └── [projectId]/
    │   │   │       ├── page.tsx
    │   │   │       ├── editor/page.tsx
    │   │   │       ├── preview/page.tsx
    │   │   │       ├── versions/page.tsx        # CR-6 version history
    │   │   │       ├── workflow/page.tsx        # CR-7 live workflow status
    │   │   │       └── export/page.tsx
    │   │   ├── library/page.tsx                 # CR-8 asset library
    │   │   ├── pipelines/page.tsx               # CR-2 choose Pipeline A/B/C
    │   │   ├── templates/page.tsx
    │   │   ├── analytics/page.tsx
    │   │   ├── credits/page.tsx
    │   │   ├── billing/page.tsx
    │   │   ├── settings/page.tsx
    │   │   └── admin/                           # CR-9 / CR-1 / CR-11 / CR-12 / CR-13
    │   │       ├── flags/page.tsx               # feature flag console
    │   │       ├── plugins/page.tsx             # AI provider plugin console
    │   │       ├── models/page.tsx              # AI model registry console
    │   │       ├── usage/page.tsx               # platform-wide usage & cost
    │   │       └── queues/page.tsx              # queue depth, age, DLQ inspector
    │   └── api/                     # Next.js route handlers (proxy only)
    │       └── health/route.ts
    │
    ├── features/                    # ====== FEATURE-FIRST MODULES ======
    │   ├── auth/
    │   │   ├── components/
    │   │   ├── hooks/
    │   │   ├── api/                 # React Query hooks
    │   │   ├── store/               # Zustand slices
    │   │   └── schemas/             # Zod
    │   ├── projects/
    │   ├── project-versions/        # CR-6
    │   ├── workflows/               # CR-7 status, pause/resume controls
    │   ├── pipelines/               # CR-2 picker for Pipeline A/B/C
    │   ├── library/                 # CR-8 unified asset library
    │   ├── timeline/                # the editor (Phase 7)
    │   │   ├── components/
    │   │   │   ├── Timeline.tsx
    │   │   │   ├── TrackRow.tsx
    │   │   │   ├── ClipBlock.tsx
    │   │   │   ├── Playhead.tsx
    │   │   │   └── Toolbar.tsx
    │   │   ├── hooks/
    │   │   ├── store/
    │   │   └── utils/
    │   ├── preview/
    │   ├── script/
    │   ├── storyboard/
    │   ├── media/                   # image + video gen UI
    │   ├── voice/
    │   ├── subtitles/
    │   ├── music/
    │   ├── render/
    │   ├── export/
    │   ├── billing/
    │   ├── credits/
    │   ├── analytics/
    │   ├── notifications/
    │   ├── feature-flags/           # CR-9 client-side flag evaluation + admin UI
    │   └── plugins/                 # CR-1 admin: enable/disable AI providers
    │
    ├── components/                  # shared, framework-agnostic UI
    │   ├── ui/                      # ShadCN primitives
    │   ├── layout/
    │   │   ├── Sidebar.tsx
    │   │   ├── Topbar.tsx
    │   │   └── AppShell.tsx
    │   ├── feedback/
    │   │   ├── Toast.tsx
    │   │   └── ProgressBar.tsx
    │   └── data/
    │       ├── DataTable.tsx
    │       └── EmptyState.tsx
    │
    ├── lib/                         # cross-cutting client utilities
    │   ├── api/
    │   │   ├── client.ts            # fetch wrapper with auth
    │   │   └── endpoints.ts         # typed endpoint map
    │   ├── auth/
    │   │   ├── session.ts
    │   │   └── guards.tsx
    │   ├── query/
    │   │   └── client.ts            # React Query client
    │   ├── ws/
    │   │   └── socket.ts            # WebSocket helper
    │   ├── analytics/
    │   ├── animations/              # Framer Motion variants
    │   ├── validation/              # shared Zod schemas
    │   └── utils/
    │
    ├── stores/                      # global Zustand stores (cross-feature)
    │   ├── user-store.ts
    │   ├── project-store.ts
    │   └── ui-store.ts
    │
    ├── styles/
    │   ├── globals.css
    │   └── tokens.css               # design tokens (dark theme)
    │
    └── types/
        ├── api.d.ts                 # generated from OpenAPI
        └── domain.d.ts
```

---

## 6. Bounded Contexts & Module Boundaries (CR-10)

Every context owns one or more **aggregate roots** in `domain/`. Nothing outside a context may reach into another context's internal entities — communication happens through the **Event Bus (CR-4)** or via well-defined application use cases.

| Bounded Context | Aggregate Root(s) | Backend modules | Frontend feature | Domain Events Emitted |
|---|---|---|---|---|
| **Identity** | `User` | `domain/identity`, `application/auth` | `features/auth` | `UserRegistered`, `EmailVerified`, `PasswordReset` |
| **Projects** | `Project` (contains `ProjectVersion`s, CR-6) | `domain/projects`, `application/projects` | `features/projects` | `ProjectCreated`, `ProjectVersionCreated`, `ProjectAutosaved`, `ProjectRestored` |
| **Scenes & Storyboards** | `Storyboard` (contains `Scene`s) | `domain/scenes`, `application/ai_pipeline` | `features/storyboard` | `SceneCreated`, `StoryboardGenerated` |
| **Prompts** | `Prompt` | `domain/prompts`, `application/ai_pipeline` | `features/script` | `PromptGenerated` |
| **Media** | `Image`, `Video`, `Narration`, `Subtitle`, `Music`, `SoundEffect`, `Thumbnail` | `domain/media`, `ai/providers/*` | `features/media`, `voice`, `subtitles`, `music` | `ImageFinished`, `VideoFinished`, `VoiceFinished`, `SubtitlesFinished` |
| **Timeline** | `Timeline` (contains `Track`s & `Clip`s) | `domain/timeline` | `features/timeline`, `preview` | `TimelineUpdated`, `ClipAdded`, `ClipMoved` |
| **Rendering & Export** | `RenderJob`, `ExportJob` | `domain/render`, `infrastructure/rendering`, `ai/workflows/pipeline_*` | `features/render`, `export` | `RenderStarted`, `RenderFinished`, `ExportCompleted` |
| **Workflows** (CR-7) | `WorkflowRun` (contains `WorkflowStep`s & `Checkpoint`s) | `domain/workflows`, `ai/workflows` | (cross-cutting status panel) | `WorkflowStarted`, `WorkflowPaused`, `WorkflowResumed`, `WorkflowCompleted`, `WorkflowFailed` |
| **Asset Library** (CR-8) | `LibraryAsset` | `domain/asset_library`, `application/asset_library` | `features/library` | `AssetAddedToLibrary` |
| **Billing & Credits** | `Subscription`, `CreditLedger` | `domain/billing`, `domain/credits`, `infrastructure/payments` | `features/billing`, `credits` | `CreditsConsumed`, `CreditsPurchased`, `SubscriptionChanged` |
| **Analytics & Notifications** | `AnalyticsEvent`, `Notification` | `infrastructure/db/models/` | `features/analytics`, `notifications` | (subscribers only — does not emit) |
| **Feature Flags** (CR-9) | `FeatureFlag` | `domain/feature_flags` (lightweight), `infrastructure/feature_flags` | `features/admin/flags` | `FeatureFlagChanged` |
| **AI Models** (CR-11) | `AIModel` | `domain/ai_models`, `application/ai_models`, `infrastructure/ai_models` | `features/admin/models` | `ModelRegistered`, `ModelDeprecated`, `ModelRetired`, `ModelAutoUpgraded` |
| **Usage & Cost** (CR-12) | `UsageRecord` | `domain/usage`, `application/usage`, `infrastructure/ai/middleware/usage_recorder.py` | `features/analytics`, `features/billing` | `UsageRecorded`, `CostReconciled` |

Communication between contexts:
- **Synchronous within one use case:** internal Python function calls via the DI container (monolith deploy).
- **Asynchronous between contexts:** domain events published to the **Event Bus (CR-4)** — Redis Streams by default, NATS / Kafka pluggable. Subscribers live in `application/event_handlers/`.
- **Transactional integrity:** the **outbox pattern** (`infrastructure/events/outbox.py`) guarantees that domain state and event publication commit atomically.

---

## 7. Data Flow — End-to-End Generation

A generation run is always driven by the **Workflow Engine (CR-7)** running one of the **Rendering Pipelines (CR-2)**. Every state transition publishes a domain event onto the **Event Bus (CR-4)**.

```
1. POST /api/v1/projects/{id}/workflows               (gateway)
   body: { pipeline_id: "pipeline_b_ai_images_motion", inputs: {…} }
2. application.workflows.start_workflow()             (use case)
   ├─ creates WorkflowRun (domain)
   ├─ writes checkpoint "step:0 / status:queued"
   ├─ emits WorkflowStarted to Event Bus
   └─ enqueues Celery task `workflow.run(run_id)`
3. ai/workflows/engine.WorkflowEngine resumes the run
   ├─ loads checkpointer state from Postgres
   ├─ executes next LangGraph node
   ├─ writes checkpoint after each node
   └─ emits StepCompleted / domain event per node

   Example for Pipeline B (AI Images → Motion → Video):
     Script ► Analyze ► Storyboard ► SceneSplit ► Prompts
                ▼          ▼            ▼           ▼
              (LLM)      (LLM)       (LLM)       (LLM)        ← emits PromptGenerated
                                                  ▼
                                  ImagesAgent ► (Image Provider Plugin)
                                                  ▼            ← emits ImageFinished
                                  MotionAgent ► (Video Provider: img2vid)
                                                  ▼            ← emits VideoFinished
                                  VoiceAgent  ► (Voice Provider Plugin)
                                  SubtitleAgent
                                  MusicAgent
                                                  ▼
                            (each asset persisted to Asset Library, CR-8)
                                                  ▼
                                  RenderAgent ► Rendering Pipeline (FFmpeg)
                                                  ▼            ← emits RenderStarted / RenderFinished
                                  ExportAgent ► Storage Provider Plugin (CR-5)
                                                  ▼            ← emits ExportCompleted

4. WebSocket /ws/workflows/{run_id} streams every event to the frontend.
5. On crash/pause, calling `resume_workflow` continues from the last checkpoint —
   no work is repeated.
```

All steps publish progress events; the Event Bus replays them to subscribers (notifications, analytics, asset library, frontend WebSocket bridge).

---

## 8. AI Provider Plugin System (CR-1)

The single most important extension point. **Adding any new AI vendor must require exactly one new class and one registry entry — no edits to existing code.**

### 8.1 Plugin Contract

Every AI provider implements `app/ai/providers/base/plugin.py::BasePlugin` **plus** one capability interface:

```
BasePlugin
├── name:           str                      # "openai", "google_veo", "runway", …
├── version:        str                      # semver of the adapter
├── kind:           PluginKind               # llm | image | video | voice
├── capabilities:   set[Capability]          # e.g. {TEXT_TO_VIDEO, IMG_TO_VIDEO}
├── cost_per_unit:  Money | None             # used by Credits context
├── rate_limits:    RateLimitSpec
├── config_schema:  pydantic.BaseModel       # validates env / settings
├── health_check()  → HealthStatus           # called by /healthz aggregator
└── close()         → None                   # for graceful shutdown
```

Capability interfaces (mix-in style ABCs):

| Interface | Methods |
|---|---|
| `LLMProvider` | `complete`, `chat`, `embed`, `stream` |
| `ImageProvider` | `generate`, `regenerate`, `variation`, `upscale`, `edit`, `face_consistency`, `character_consistency` |
| `VideoProvider` | `text_to_video`, `image_to_video`, `extend`, `replace_clip`, `motion_control`, `camera_control` |
| `VoiceProvider` | `synthesize`, `clone`, `list_voices` |

### 8.2 Plugin Discovery & Registration

- Each plugin lives in `app/ai/providers/<kind>/<name>.py`.
- Registration is **declarative** via a single decorator:
  ```
  @register_plugin(kind="video", name="runway", version="1.0.0",
                   capabilities={TEXT_TO_VIDEO, IMG_TO_VIDEO, EXTEND})
  class RunwayProvider(BasePlugin, VideoProvider): ...
  ```
- `infrastructure/plugins/loader.py` discovers all `@register_plugin` decorations at startup (and via Python `entry_points` for third-party packages → out-of-tree plugins).
- `infrastructure/plugins/registry.py` is the single lookup point:
  `registry.get(kind="video", name="runway")` or `registry.list(kind="video")`.

### 8.3 Selection Logic

Per generation request the resolver picks a provider in this priority order:

1. Explicit per-request override (`params.provider`) — must pass feature-flag check (CR-9).
2. Project-level default (`Project.settings.providers.video`).
3. User-level default (subscription tier may pin specific providers).
4. Tenant/global default from `settings.toml`.
5. Fallback chain (configurable): if Runway 5xx → try Pika → try Luma → fail with `NoHealthyProvider`.

### 8.4 Required Provider Coverage (per `rule.md`)

| Kind | Providers (initial set) |
|---|---|
| LLM | OpenAI, Anthropic, Gemini, Local (Ollama) |
| Image | FLUX, SDXL/Stable Diffusion XL, ComfyUI, Ideogram, DALL-E |
| Video | Google Veo, Runway, Kling AI, Pika, Luma, Stable Video Diffusion, ComfyUI |
| Voice | ElevenLabs, XTTS, OpenAI TTS, Edge TTS, Coqui |

Adding e.g. **MidJourney** later = drop `app/ai/providers/image/midjourney.py`, add the decorator, done.

---

## 8a. Multiple Rendering Pipelines (CR-2)

A **Rendering Pipeline** is a strategy implementing `application/interfaces/rendering_pipeline.py::RenderingPipeline` and registered in `ai/workflows/registry.py`. Each pipeline is a separate LangGraph state machine.

```
RenderingPipeline
├── id:            str                       # "pipeline_a_stock_footage"
├── name:          str
├── description:   str
├── required_caps: set[Capability]           # used to validate provider plugins
├── inputs_schema: pydantic.BaseModel        # what the user must provide
└── build_graph()  → langgraph.StateGraph    # the actual nodes & edges
```

### Initial Pipelines

**Pipeline A — Stock Footage Pipeline**
```
Script → Analyze → Storyboard → SceneSplit → Stock-Footage Match (per scene)
       → Voice  → Subtitles → Music → Timeline → Render → Export
```
Use case: news / explainers / fast turnaround. Stock footage client lives in `infrastructure/rendering/stock_footage_client.py`.

**Pipeline B — AI Images + Motion Pipeline**
```
Script → Analyze → Storyboard → SceneSplit → Prompts
       → AI Images (ImageProvider) → Motion (VideoProvider: img2vid)
       → Voice → Subtitles → Music → Timeline → Render → Export
```
Use case: cinematic / stylized content. Default for most users.

**Pipeline C — AI Video Clips Pipeline**
```
Script → Analyze → Storyboard → SceneSplit → Prompts
       → AI Video Clips (VideoProvider: text_to_video)
       → Voice → Subtitles → Music → Timeline → Render → Export
```
Use case: premium / motion-heavy content (Veo / Runway / Kling).

### Extensibility

Adding **Pipeline D — Mixed (stock + AI inserts)** = create one new file in `ai/workflows/pipeline_d_mixed.py`, register it. The frontend `pipelines` feature lists all registered pipelines automatically; no UI change required.

---

## 8b. AI Orchestration — Separated Concerns (CR-3)

The `app/ai/` package is deliberately split into seven **independent** subpackages so each can be unit-tested, mocked, and reasoned about in isolation:

| Subpackage | Responsibility | Depends on |
|---|---|---|
| `ai/agents/` | Autonomous personas (CrewAI/AutoGen) — `ScriptAgent`, `ImageAgent`, … | `providers`, `prompts`, `memory`, `tools` |
| `ai/providers/` | External AI vendors behind plugin contracts (CR-1) | — (leaf) |
| `ai/prompts/` | Versioned Jinja2 prompt templates + `PromptRegistry` | — (leaf) |
| `ai/memory/` | Short-term (Redis) and long-term (pgvector) agent memory | `infrastructure/cache`, `infrastructure/db` |
| `ai/tools/` | Callable tools exposed to agents (web_search, image_generate, …) | `providers`, `storage` |
| `ai/chains/` | LangChain composable chains (analysis, storyboard, SEO, …) | `providers`, `prompts` |
| `ai/workflows/` | LangGraph state machines = the **Rendering Pipelines** (CR-2) | all of the above |

**Dependency rule:** arrows only flow downward in the table. `providers` never imports `agents`. `chains` never imports `workflows`. Static check enforced in CI (`import-linter`).

This separation means: a failing image generation is debuggable by running only `ai/providers/image/flux.py` plus its plugin test, with no agent or workflow involved.

---

## 8c. Event Bus (CR-4)

### 8c.1 Topics & Canonical Event Names

Defined in `infrastructure/events/topics.py`:

```
project.created
project.version.created
project.autosaved
script.generated
scene.created
storyboard.generated
prompt.generated
image.finished
video.finished
voice.finished
subtitles.finished
music.selected
render.started
render.progress
render.finished
export.completed
workflow.started
workflow.step.completed
workflow.paused
workflow.resumed
workflow.failed
workflow.completed
credits.consumed
credits.purchased
asset.added_to_library
feature_flag.changed
```

### 8c.2 Event Envelope

Every event uses the same envelope:
```
Event {
  id:           uuid
  topic:        str
  occurred_at:  datetime (UTC)
  tenant_id:    uuid | null
  user_id:      uuid | null
  correlation_id: uuid           # ties together a workflow run
  causation_id:   uuid | null    # the event that caused this one
  schema_version: int
  payload:      dict             # validated against per-topic Pydantic model
}
```

### 8c.3 Implementations

- **Default:** `redis_streams_bus.py` (consumer groups for at-least-once delivery).
- **Pluggable:** `nats_bus.py`, `kafka_bus.py`, `in_memory_bus.py` (tests).
- All implement the same `IEventBus` ABC — swap via config.

### 8c.4 Transactional Outbox

`infrastructure/events/outbox.py` writes domain events to an `outbox` table inside the same DB transaction as the aggregate change. A separate dispatcher (`workers/event_worker.py`) tails the outbox and publishes to the bus. This guarantees that "no event is ever lost when state was committed" and "no event is ever published for state that was rolled back."

### 8c.5 Subscribers

Located in `application/event_handlers/`. Examples:
- `on_image_finished` → add to Asset Library (CR-8) → emit next workflow step.
- `on_render_finished` → notify user → bump analytics → trigger export job.
- `on_export_completed` → email user with download link → award credits refund (if free retry).

---

## 8d. Storage Provider Plugin System (CR-5)

Storage backends implement `infrastructure/storage/base.py::StorageProvider`:

```
StorageProvider
├── put(key, stream, content_type, metadata) → AssetRef
├── get(key)                                  → BinaryStream
├── delete(key)                               → None
├── presign_put(key, ttl, content_type)       → str (URL)
├── presign_get(key, ttl)                     → str (URL)
├── stat(key)                                 → AssetStat
└── copy(src_key, dst_key)                    → None
```

Initial implementations (one file each in `infrastructure/storage/providers/`):

| Backend | File | Notes |
|---|---|---|
| Local filesystem | `local.py` | dev only |
| AWS S3 | `s3.py` | prod default for US |
| Cloudflare R2 | `r2.py` | egress-free option |
| Azure Blob | `azure_blob.py` | enterprise customers |
| Google Cloud Storage | `gcs.py` | GCP regions |

A `StorageRegistry` resolves the active backend per asset (rules: per-tenant pin, per-region routing, per-asset-kind override). Migration between backends is a single `copy(src, dst)` plus DB pointer update — no code change.

---

## 8e. Project Versioning (CR-6)

### Model

```
Project (aggregate root, mutable head)
└── ProjectVersion (immutable snapshot)
    ├── version_number: int (monotonic per project)
    ├── parent_version_id: uuid | null   ← supports branching
    ├── created_by: UserId
    ├── created_at: datetime
    ├── reason: enum {manual_save, autosave, restore, branch, generated}
    ├── snapshot: json (the full state of the project at that moment)
    └── diff_summary: json (cached human-readable diff vs parent)
```

### Behaviour

- **Every edit** that mutates a project goes through `application/projects/snapshot_version.py` which:
  1. Builds the new snapshot.
  2. Computes the diff vs current head.
  3. Persists the new `ProjectVersion`.
  4. Moves the project head pointer.
  5. Emits `project.version.created` on the bus.
- **Autosave** uses `reason=autosave`; debounced to one snapshot per N seconds with no-op coalescing.
- **Restore** is a non-destructive operation that creates a *new* version pointing at the restored snapshot (preserves linear history but visible as a branch in the UI).
- **Storage:** large snapshots (timeline + asset refs) live in JSONB; binary assets are referenced, never copied (asset library handles their lifecycle).

This is Canva-style: history is always linear in the timeline UI but the data model supports branching for templates and "duplicate from version" flows.

---

## 8f. Workflow Engine (CR-7)

### Properties

- **Resumable** — every step writes a checkpoint to Postgres (`workflow_checkpoints` table).
- **Pausable** — a pause sets `status=paused` and the engine yields after the current node finishes.
- **Cancellable** — cooperative; the engine checks a cancellation flag between nodes.
- **Replayable** — given a checkpoint id, the engine can re-execute from that point with identical inputs.
- **Observable** — every step emits a domain event; the UI subscribes via `/ws/workflows/{run_id}`.
- **Retry-aware** — per-step retry policy with exponential backoff; permanent failures escalate to `WorkflowFailed`.

### Components

| Component | Path | Role |
|---|---|---|
| `WorkflowEngine` | `app/ai/workflows/engine.py` | Owns the run loop |
| `Checkpointer` | `app/ai/workflows/checkpointer.py` | Postgres-backed LangGraph checkpoint store |
| `BaseRenderingPipeline` | `app/ai/workflows/base_pipeline.py` | Abstract base for CR-2 pipelines |
| `PipelineRegistry` | `app/ai/workflows/registry.py` | Lookup by `pipeline_id` |
| `workflow_worker` | `app/workers/workflow_worker.py` | Celery worker that calls `engine.tick(run_id)` |
| `WorkflowRun` (domain) | `app/domain/workflows/entities.py` | Aggregate root |

### API Surface

```
POST   /api/v1/workflows                    start (returns run_id)
GET    /api/v1/workflows/{run_id}           status + step list
POST   /api/v1/workflows/{run_id}/pause
POST   /api/v1/workflows/{run_id}/resume
POST   /api/v1/workflows/{run_id}/cancel
WS     /ws/workflows/{run_id}               live stream of events
```

---

## 8g. Asset Library (CR-8)

Every asset produced or uploaded — `Image`, `Video`, `Music`, `Subtitle`, `Voice`, `Prompt`, `Thumbnail` — is **automatically** persisted to the Asset Library.

### Model

```
LibraryAsset
├── id:           uuid
├── owner_id:     UserId
├── tenant_id:    TenantId
├── kind:         AssetKind
├── storage_ref:  str            # opaque key into StorageProvider (CR-5)
├── mime_type:    str
├── size_bytes:   int
├── duration:     Duration | null
├── source:       enum {generated, uploaded, stock}
├── source_meta:  json           # which provider/agent/version made it
├── tags:         list[str]
├── project_refs: list[ProjectId]  # back-pointer to projects using it
├── prompt_id:    PromptId | null
└── created_at:   datetime
```

### Behaviour

- Subscribers in `application/event_handlers/` listen for every `*.finished` event and call `add_asset_to_library`.
- Users can **re-use** any library asset across projects without regenerating (saves credits).
- Library supports search (text + vector via pgvector), tag filters, kind filters, and "find similar".
- Deletion is **soft** (the asset moves to trash) — a project version pointing at it still resolves.

---

## 8h. Feature Flags (CR-9)

### Capabilities

- Enable / disable individual AI providers (e.g. `provider.video.veo = on`, `provider.image.sdxl = off`).
- Enable / disable rendering pipelines (`pipeline.pipeline_c_ai_video_clips = on`).
- UI feature toggles (`ui.new_timeline = on`).
- Targeted rollouts: per-tenant, per-user, per-subscription-tier, or percentage rollout.
- Kill switches for runaway features.

### Contract

```
FeatureFlagProvider
├── evaluate(flag_key, context) → FlagValue
├── list(prefix=None)           → list[FlagDescriptor]
└── set(flag_key, rules)        → None              # admin only
```

Implementations:
- `db_flag_provider.py` — default; rules stored in `feature_flags` table.
- `unleash_provider.py` — for orgs already running Unleash.
- `env_flag_provider.py` — dev/CI.

The `core/container.py` consults the active flag provider on every plugin lookup, so a disabled provider is **invisible** to the rest of the system (it never appears in plugin lists, never gets selected, never has credits charged against it).

### API

```
GET   /api/v1/feature-flags                 list (filtered by caller's tenant)
PUT   /api/v1/feature-flags/{key}           admin only
GET   /api/v1/feature-flags/evaluate?key=…  evaluate against current context
```

The frontend has `features/feature-flags/` with a `<FeatureGate flag="…">` component and an admin console under `/admin/flags`.

---

## 8i. Provider/Storage Pattern Summary (Combined Strategy + Factory)

A single mental model unifies CR-1 and CR-5:

```
Layer 1: Plugin Contract (ABC in app/ai/providers/base/ or infrastructure/storage/base.py)
Layer 2: Plugin Implementation (one file per vendor)
Layer 3: Decorator-based Registration (@register_plugin)
Layer 4: Registry / Factory (discovers + caches all plugins at startup)
Layer 5: Selection (config + feature flags + per-request override + fallback chain)
Layer 6: Container Wiring (DI binds the resolved plugin into the use case)
```

Adding *anything* external = touch layers 2 + 3 only. Layers 1, 4, 5, 6 never change.

---

## 8j. AI Model Registry (CR-11)

The **Provider** plugin system (CR-1) abstracts the *vendor*. The **Model Registry** is the layer below it: every provider exposes a catalogue of concrete *models* (Veo 2, Veo 3, GPT-5, Gen-4, Gen-5, FLUX Dev, FLUX Pro, …). Model names must NEVER be hardcoded in business logic.

### 8j.1 Domain Object

```
domain/ai_models/entities.py

AIModel  (aggregate root inside the AIModels context)
├── id:            ModelId                 # stable internal id, e.g. "veo-3"
├── provider:      str                     # "google", "openai", "runway", …
├── vendor_model_id: str                   # exact id sent over the wire, e.g. "veo-3.0"
├── kind:          PluginKind              # llm | image | video | voice
├── capabilities:  set[Capability]         # TEXT_TO_VIDEO, IMG_TO_VIDEO, …
├── modalities:    set[Modality]           # text, image, video, audio
├── context_window: int | null             # tokens (LLM) / frames (video)
├── max_output:     ModelOutputLimits      # tokens / pixels / seconds / etc.
├── pricing:        PricingTable           # per-unit costs (see §8k)
├── latency_p50_ms: int | null             # tracked + updated by telemetry
├── status:         ModelStatus            # available | preview | deprecated | retired
├── released_at:    date | null
├── deprecated_at:  date | null            # set when vendor announces sunset
├── retires_at:     date | null            # hard cut-off
├── successor_id:   ModelId | null         # for graceful upgrades (Veo 2 → Veo 3)
├── tags:           list[str]              # "fast", "cinematic", "cheap", …
└── metadata:       json                   # vendor-specific extras
```

### 8j.2 Folder Layout (added to §4)

```
app/
├── domain/
│   └── ai_models/
│       ├── entities.py             # AIModel, PricingTable, ModelOutputLimits
│       ├── value_objects.py        # ModelStatus, Modality, Capability
│       ├── events.py               # ModelRegistered, ModelDeprecated, ModelRetired
│       └── policies.py             # selection + deprecation policies
├── application/
│   └── ai_models/
│       ├── register_model.py
│       ├── deprecate_model.py
│       ├── list_models.py          # filtered by kind / capability / status
│       ├── select_default_model.py # priority logic (see 8j.4)
│       └── upgrade_model.py        # bump default from deprecated → successor
├── ai/
│   └── providers/
│       └── <kind>/<vendor>.py      # MUST publish its model catalogue via
│                                   # `def list_models() -> list[AIModelSpec]`
└── infrastructure/
    ├── db/models/
    │   └── ai_model.py             # ORM mirror of domain AIModel
    └── ai_models/
        ├── registry.py             # in-memory cache; loaded at startup + DB-synced
        ├── discovery.py            # calls `provider.list_models()` per plugin
        └── seed/
            └── builtin_models.yaml # bootstrap catalogue (declarative, versioned)
```

### 8j.3 Discovery & Synchronisation

1. **Static seed** — `seed/builtin_models.yaml` lists every known model at release time. Source-controlled, reviewed.
2. **Provider self-declaration** — every provider plugin (CR-1) implements `list_models() -> list[AIModelSpec]`. On startup the registry calls this for each loaded provider.
3. **DB sync** — `ai_model` rows are upserted: new models inserted, missing-at-vendor models marked `deprecated`, retired models marked `retired` (but never deleted, for historical cost / billing).
4. **Live refresh** — `POST /api/v1/admin/ai-models/refresh` (admin-only) re-runs discovery without restart.
5. **Domain events** — `ModelRegistered`, `ModelDeprecated`, `ModelRetired` fired on the Event Bus (CR-4).

### 8j.4 Default Model Selection (priority order)

Mirrors the provider selection chain (§8.3) but at the model level:

1. Explicit per-request `model_id` (must be `available` or `preview` and pass feature-flag CR-9).
2. Project-level pin (`Project.settings.models.<kind>`).
3. User / subscription-tier default.
4. Tenant / global default.
5. Registry policy: best `(quality_score, -cost, -latency_p50)` matching required capabilities, filtered to `status == available`.
6. Fallback: if selected model is `deprecated` and a `successor_id` exists → auto-upgrade and emit `ModelAutoUpgraded`.

### 8j.5 Deprecation Lifecycle

```
available  ─┐
            ├─►  deprecated  ──►  retired  (read-only; only usable for cost recon)
preview   ──┘
```

- `deprecated` models still work but emit a warning event and a UI banner.
- `retired` models reject new requests with `HTTP 410 Gone`; the registry routes to `successor_id` if present.

### 8j.6 API

```
GET   /api/v1/ai-models                     ?kind=video&capability=TEXT_TO_VIDEO&status=available
GET   /api/v1/ai-models/{id}
GET   /api/v1/ai-models/defaults            (resolved chain for the caller)
POST  /api/v1/admin/ai-models/refresh
PUT   /api/v1/admin/ai-models/{id}          (admin override: status, tags, default)
```

---

## 8k. AI Cost Tracking (CR-12)

Every external AI call **must** create exactly one immutable usage record. This is the source of truth for billing, credits, analytics, and ML-ops experimentation. The recorder is implemented once, in a single middleware around the provider plugin call — never inside the plugin itself.

### 8k.1 Domain Object

```
domain/usage/entities.py

UsageRecord  (aggregate root, immutable after commit)
├── id:                 UsageId            # uuid
├── tenant_id:          TenantId
├── user_id:            UserId
├── project_id:         ProjectId | null
├── scene_id:           SceneId   | null
├── workflow_run_id:    WorkflowRunId | null
├── step_id:            WorkflowStepId | null
├── provider:           str                # "google"
├── model_id:           ModelId            # "veo-3"
├── vendor_model_id:    str                # what the vendor saw
├── capability:         Capability         # TEXT_TO_VIDEO
├── request_id:         str                # provider's request id (idempotency)
├── started_at:         datetime
├── finished_at:        datetime
├── duration_ms:        int
├── status:             UsageStatus        # success | failed | partial | timeout
├── error_code:         str | null
│
│   ── quantitative axes (any combination, kind-dependent) ──
├── prompt_tokens:      int | null
├── completion_tokens:  int | null
├── total_tokens:       int | null
├── images_generated:   int | null
├── image_megapixels:   float | null
├── video_seconds:      float | null
├── audio_seconds:      float | null
├── embedding_count:    int | null
│
│   ── financial axes ──
├── estimated_cost:     Money              # computed from PricingTable at request time
├── actual_cost:        Money | null       # filled when vendor invoice reconciles
├── currency:           str                # ISO-4217
├── credits_consumed:   CreditAmount       # what we debited from CreditLedger
├── billable:           bool               # false for retries / system-initiated calls
│
└── metadata:           json               # request params hash, region, etc.
```

### 8k.2 Folder Layout

```
app/
├── domain/usage/
│   ├── entities.py
│   ├── value_objects.py    # Money, UsageStatus, PricingTable
│   ├── events.py           # UsageRecorded, CostReconciled
│   └── policies.py         # cost-estimation strategies per kind
├── application/usage/
│   ├── record_usage.py     # the single recorder use case
│   ├── reconcile_costs.py  # nightly job vs vendor invoices
│   ├── query_usage.py
│   └── export_usage.py     # CSV / Parquet exports
├── infrastructure/
│   ├── db/models/
│   │   ├── usage_record.py
│   │   └── cost_reconciliation.py
│   └── ai/middleware/
│       └── usage_recorder.py   # wraps every provider call (decorator)
```

### 8k.3 Recording Flow

```
provider.<kind>.<method>(params)
        │
        ▼
  usage_recorder middleware:
    1. precompute estimated_cost from AIModel.pricing  (CR-11)
    2. reserve credits (CreditLedger ledger entry: status=pending)
    3. call vendor
    4. on response → finalize UsageRecord (immutable insert)
    5. emit usage.recorded on Event Bus (CR-4)
    6. settle credits ledger (pending → consumed; release any over-reservation)
    7. attach UsageRecord.id to the workflow step (CR-7) for traceability
```

A single Celery beat task `reconcile_costs` runs nightly: pulls vendor invoices (where supported) and updates `actual_cost` + emits `cost.reconciled` for drift > tolerance.

### 8k.4 Reporting & API

```
GET  /api/v1/usage                          ?from=…&to=…&group_by=model|provider|user
GET  /api/v1/usage/{id}
GET  /api/v1/usage/summary                  (rollups for dashboard)
GET  /api/v1/projects/{id}/usage            (drill-down per project)
GET  /api/v1/admin/cost-reconciliation
```

The dashboard `features/analytics/` consumes these to render: **per-user spend, per-project spend, per-model breakdown, error-rate by provider, average latency by model, credit-to-cost margin**.

### 8k.5 Anti-Drift Guarantees

- The recorder is **the only path** to a provider plugin. Direct calls are forbidden (enforced by `import-linter`: plugins are private to the middleware).
- `request_id` is unique; duplicate inserts are no-ops. Retries do not double-charge.
- `actual_cost` overrides `estimated_cost` on reconciliation; the delta is logged for accountability.

---

## 8l. Queue Priorities (CR-13)

All async work flows through Celery. Jobs are routed into one of five priority queues, each with its own concurrency settings and SLA target:

| Queue | Use cases | SLA target | Concurrency policy |
|---|---|---|---|
| `critical` | Webhooks, payment confirmations, auth events, billing reconciliation triggers | < 5 s | Reserved workers; never starved |
| `high` | Paid-tier user workflows, real-time generation steps, export jobs for paid users | < 30 s start | High concurrency; preempts `normal` |
| `normal` | Free-tier user workflows, default generation, regular exports | < 2 min start | Default pool |
| `low` | Bulk regeneration, optional analytics enrichment, library re-indexing | < 15 min start | Throttled |
| `background` | Nightly cost reconciliation, vector re-embedding, log compaction, vendor catalogue sync | best-effort | One worker, off-peak |

### 8l.1 Folder Layout

```
app/infrastructure/queue/
├── celery_app.py
├── queues.py                # canonical queue names + Celery routing
├── routing.py               # decides queue per task + caller context
├── policies/
│   ├── tier_policy.py       # subscription tier → queue mapping
│   ├── rate_limit_policy.py # per-tenant rate caps
│   └── overload_policy.py   # circuit-break low/background when high is hot
└── tasks/
    ├── workflow_tasks.py    # decorated with @task(queue=route(...))
    ├── ai_tasks.py
    ├── render_tasks.py
    ├── email_tasks.py
    └── reconcile_tasks.py
```

### 8l.2 Routing Logic (`routing.py`)

```
def queue_for(task_name, *, user, payload) -> str:
    if task_name in CRITICAL_TASKS:        return "critical"
    if payload.get("priority_override"):   return validate(payload["priority_override"])
    tier = user.subscription.tier          # free | pro | business | enterprise
    if tier in {"business", "enterprise"}: return "high"
    if tier == "pro":                      return "normal"
    return "normal" if user.kind == "human" else "low"
```

Admins can override per-tenant priority via the feature-flag system (CR-9: `queue.override.tenant.<id> = high`).

### 8l.3 Backpressure & Fairness

- **Per-tenant token bucket** prevents a single tenant from monopolising `high`.
- **Per-provider token bucket** prevents one vendor's rate limit from blocking unrelated jobs.
- **Dead-letter queue** (`<queue>.dlq`) for tasks that fail beyond retry budget; surfaced in admin UI.
- **Visibility:** `/api/v1/admin/queues` returns depth + age per queue; Prometheus exports the same.

### 8l.4 Interaction with Workflow Engine (CR-7)

A single `WorkflowRun` may have steps routed to different queues — e.g. an Enterprise user's `RenderStep` goes to `high` while their `EmbeddingStep` goes to `background`. The engine reads `step.queue_hint` (set by the pipeline definition) when enqueueing.

---

## 9. Async & Real-time Strategy

| Concern | Channel |
|---|---|
| Long-running pipeline | Celery + Redis broker |
| Per-step progress | Redis Pub/Sub → FastAPI WebSocket bridge |
| Cancellation | Cooperative — Celery revoke + LangGraph checkpoint store |
| Retries | Celery autoretry with exponential backoff; max 3 per provider call |
| Idempotency | `params_hash` key in Redis (TTL 24h) |
| Rate limiting | Redis token bucket per user + per provider |

---

## 10. Security Decisions

- **Passwords:** Argon2id via `argon2-cffi`.
- **Tokens:** Access JWT 15 min, refresh JWT 30 days, rotation + reuse detection.
- **OAuth:** Authorization Code + PKCE for Google.
- **Secrets:** Never in repo; `.env` for dev, Docker secrets / cloud secret manager for prod.
- **CSP & headers:** strict CSP, HSTS, X-Frame-Options DENY at Nginx/edge.
- **Input validation:** Pydantic v2 on backend, Zod on frontend; both share OpenAPI-generated types.
- **File uploads:** signed presigned PUTs to object storage; mime sniffing on server before linking to a project.
- **PII:** users table separated from analytics_event table; logs scrub PII.

---

## 11. Environment Matrix

| Env | Purpose | DB | Storage | Providers |
|---|---|---|---|---|
| `local` | dev | local Postgres in Docker | local filesystem (`storage/`) | mocked or free tiers |
| `staging` | QA | managed Postgres | R2 staging bucket | real providers, low quotas |
| `prod` | live | managed Postgres (HA) | R2 + S3 cold backup | real providers, full quotas |

---

## 12. ADR Index (to be authored alongside each phase)

- **ADR-0001** Record architecture decisions — Accepted.
- **ADR-0002** Monolith-first, microservice-ready — Accepted.
- **ADR-0003** LangGraph as the workflow/pipeline orchestrator — Accepted.
- **ADR-0004** AI Provider Plugin System with decorator registration (CR-1) — Accepted.
- **ADR-0005** PostgreSQL + Alembic for persistence — Accepted.
- **ADR-0006** Next.js 15 App Router (server-first) — Accepted.
- **ADR-0007** Celery + Redis for async jobs — Accepted.
- **ADR-0008** Argon2id + JWT rotation for auth — Accepted.
- **ADR-0009** Multiple Rendering Pipelines (CR-2) as registered strategies — Accepted.
- **ADR-0010** Split AI orchestration into agents / providers / prompts / memory / tools / chains / workflows (CR-3) — Accepted.
- **ADR-0011** Event Bus with transactional outbox (CR-4); Redis Streams default, NATS/Kafka pluggable — Accepted.
- **ADR-0012** Multi-storage Provider Plugin (CR-5): Local / S3 / R2 / Azure Blob / GCS — Accepted.
- **ADR-0013** Project Versioning (CR-6) — immutable `ProjectVersion` snapshots; head pointer; Canva-style history — Accepted.
- **ADR-0014** Resumable Workflow Engine (CR-7) with Postgres checkpointer — Accepted.
- **ADR-0015** Asset Library (CR-8) — auto-persist every generated artefact — Accepted.
- **ADR-0016** Feature Flag system (CR-9) — pluggable provider, default DB-backed, optional Unleash — Accepted.
- **ADR-0017** Explicit Domain Layer (CR-10) — entities, value objects, aggregate roots; framework-free — Accepted.
- **ADR-0018** AI Model Registry (CR-11) — model catalogue separate from provider plugins; discovery + deprecation lifecycle — Accepted.
- **ADR-0019** Immutable AI Cost Tracking (CR-12) — single recorder middleware; UsageRecord is the billing source of truth — Accepted.
- **ADR-0020** Five-tier Priority Queues (CR-13) — Critical / High / Normal / Low / Background with tenant fairness — Accepted.
- **ADR-0021** First-class Idempotency Framework (CR-DB-1) — dedicated `idempotency_keys` table — Accepted (Phase 2 Step A).
- **ADR-0022** Database-backed Distributed Locks (CR-DB-2) — `distributed_locks` table with lease + heartbeat — Accepted (Phase 2 Step A).
- **ADR-0023** Audit Log separate from Event Log (CR-DB-3) — partitioned, immutable `audit_log`, Class C retention — Accepted (Phase 2 Step A).
- **ADR-0024** Explicit Configuration Tables (CR-DB-4) — `system_settings` / `tenant_settings` / `provider_settings`; supersedes generic `settings` table — Accepted (Phase 2 Step A).
- **ADR-0025** Defer dedicated `user_preferences` table — keep in `users.extra` JSONB until product justifies extraction — Accepted (Phase 2 Step A).

(Each ADR will get its own file under `docs/architecture/adr/` in Phase 9 docs sweep.)

---

## 13. Change Request Traceability Matrix

| CR | Title | Lives in |
|---|---|---|
| CR-1 | AI Provider Plugin System | §8 + `app/ai/providers/`, `infrastructure/plugins/` |
| CR-2 | Multiple Rendering Pipelines | §8a + `app/ai/workflows/pipeline_*.py`, `application/interfaces/rendering_pipeline.py` |
| CR-3 | Separated AI Orchestration | §8b + `app/ai/{agents,providers,prompts,memory,tools,chains,workflows}/` |
| CR-4 | Event Bus | §8c + `infrastructure/events/`, `application/event_handlers/`, `domain/*/events.py` |
| CR-5 | Multi-Storage Providers | §8d + `infrastructure/storage/providers/{local,s3,r2,azure_blob,gcs}.py` |
| CR-6 | Versioned Projects | §8e + `domain/projects/versioning/`, `infrastructure/db/models/project_version.py` |
| CR-7 | Resumable Workflow Engine | §8f + `app/ai/workflows/engine.py`, `checkpointer.py`, `domain/workflows/` |
| CR-8 | Asset Library | §8g + `domain/asset_library/`, `application/asset_library/`, `infrastructure/db/models/library_asset.py` |
| CR-9 | Feature Flags | §8h + `infrastructure/feature_flags/`, `application/feature_flags/`, `domain/*` consultation |
| CR-10 | Explicit Domain Layer | §6 + entire `app/domain/` tree |
| CR-11 | AI Model Registry | §8j + `domain/ai_models/`, `application/ai_models/`, `infrastructure/ai_models/{registry,discovery,seed}/` |
| CR-12 | AI Cost Tracking | §8k + `domain/usage/`, `application/usage/`, `infrastructure/ai/middleware/usage_recorder.py`, `infrastructure/db/models/{usage_record,cost_reconciliation}.py` |
| CR-13 | Queue Priorities | §8l + `infrastructure/queue/{queues,routing,policies/}.py` |

---

## 14. What Phase 1 Explicitly Does NOT Do

- No database schema / migrations (Phase 2).
- No auth implementation (Phase 3).
- No FastAPI router code (Phase 4).
- No React components beyond folder placeholders (Phase 5).
- No agent prompts, graph code, plugin implementations, event handlers, or workflow logic (Phase 6).
- No timeline editor logic (Phase 7).
- No FFmpeg pipelines or storage-provider code (Phase 8).
- No Docker / CI / feature-flag rule data (Phase 9).
- No tests (Phase 10).

These are intentionally deferred per `rule.md` § Development Process.

---

## 15. Approval Gate

Before proceeding to **Phase 2 — Database**, the following must be confirmed by the user:

1. The proposed topology (§1.1) matches the intended deployment model (monolith-first, microservice-ready, event-driven).
2. The Clean-Architecture layering (§4) and feature-first frontend (§5) are acceptable.
3. The explicit Domain Layer (§4 `app/domain/`, §6 bounded contexts) covers every entity required by `rule.md` — **CR-10 satisfied**.
4. The AI Provider Plugin System (§8) is the agreed extension mechanism — **CR-1 satisfied**.
5. The Rendering Pipelines A/B/C (§8a) cover the initial product flows; new pipelines pluggable — **CR-2 satisfied**.
6. The AI subpackage split — agents / providers / prompts / memory / tools / chains / workflows (§8b) — is the right boundary set — **CR-3 satisfied**.
7. The Event Bus + transactional outbox (§8c) is the inter-context communication backbone — **CR-4 satisfied**.
8. The Storage Provider plugin set (§8d) covers Local / S3 / R2 / Azure Blob / GCS — **CR-5 satisfied**.
9. The Project Versioning model (§8e) is Canva-equivalent — **CR-6 satisfied**.
10. The Workflow Engine is resumable / pausable / cancellable / replayable (§8f) — **CR-7 satisfied**.
11. The Asset Library auto-captures every generated artefact (§8g) — **CR-8 satisfied**.
12. The Feature Flag system (§8h) can disable any provider, pipeline, or UI feature without redeploy — **CR-9 satisfied**.
13. The AI Model Registry (§8j) handles discovery, versioning, deprecation, defaults, and cost per model — **CR-11 satisfied**.
14. AI Cost Tracking (§8k) records every provider call immutably with full granularity (tokens / images / seconds / credits / cost / duration) — **CR-12 satisfied**.
15. Five-tier Priority Queues (§8l) — Critical / High / Normal / Low / Background — with tenant fairness, backpressure, and DLQs — **CR-13 satisfied**.
16. Sibling documents `ROADMAP.md`, `DECISIONS.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `API_CONTRACT.md` are present and populated for Phase 1.
17. The deferred-items list (§14) is correct — nothing the user expects in Phase 1 is missing.

> **Reply with "approved" (or with further change requests) before Phase 2 begins.**
