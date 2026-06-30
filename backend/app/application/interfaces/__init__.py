"""Ports — abstract interfaces implemented by ``app.infrastructure``.

Use cases under ``app.application`` depend only on these ports so they
remain free of SQLAlchemy, FastAPI, and other framework concerns.
"""
