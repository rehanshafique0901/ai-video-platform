"""Application layer — use-case orchestration and transaction boundaries.

Empty at the close of Phase 2 Step B. Phase 3 fills this package with the
``*Service`` classes that compose repositories and domain entities into
use cases (``CreateProject``, ``GenerateScene``, ``StartRender``, ...).
Application services own transactions; repositories do not. Domain
entities own invariants; services do not duplicate them.

See `ROADMAP.md` Phase 3 and `ARCHITECTURE.md` §3 (layered model) for
the precise responsibilities allocated here.
"""
