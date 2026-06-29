"""HTTP / WebSocket transport — FastAPI routers, schemas, dependencies.

Empty at the close of Phase 2 Step B. Phase 4 populates this package
with the routers backing every operation in `API_CONTRACT.md`. API code
must only depend on ``app.application`` (service layer); it must not
talk to ``app.infrastructure`` directly. The forbidden-import contract
that enforces this is declared in `pyproject.toml` and runs in CI gate
stage 3 (mypy + import-linter).
"""
