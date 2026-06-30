"""Framework glue — runtime configuration, logging, errors, middleware, DI container.

``app.core`` is the only module outside ``app.infrastructure`` that is
allowed to import infrastructure directly. The API layer reaches
infrastructure through ``app.core.container``, satisfying the
import-linter contract that forbids ``app.api`` from importing
infrastructure modules.
"""
