"""Domain layer — framework-free aggregate roots, value objects, services.

Empty at the close of Phase 2 Step B. Phase 3 populates this package with
the entities listed in ``ARCHITECTURE.md`` §6 (User, Project, Storyboard,
Prompt, Media variants, Timeline, RenderJob, ExportJob, WorkflowRun,
LibraryAsset, Subscription). The package skeleton exists today so the
import-linter contracts declared in ``pyproject.toml`` are active from
the first Phase 3 commit — a Phase 3 PR that accidentally imports
``app.infrastructure`` from ``app.domain`` will fail the CI gate's
stage 3 (mypy + import-linter) before it can land.
"""
