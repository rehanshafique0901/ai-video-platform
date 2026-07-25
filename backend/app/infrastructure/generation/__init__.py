"""Infrastructure adapters for the generation slice (Increment 3).

Concrete implementations of the generation ports declared in
``app.application.interfaces``: a Pillow-based image feature extractor and a thin
Pollinations image generator. Each adapter is deliberately narrow — it implements
exactly one port and contains no planning, verification, repair, or provider
selection logic (those live in the domain / use case).
"""
