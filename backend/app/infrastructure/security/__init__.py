"""Security primitives — password hashing, JWT issue/verify, OAuth (later).

Slice α1 ships ``PasswordHasher`` and ``JWTService``; both are
unit-tested but not yet wired into any endpoint. Slice α2 wires them
into the register/login use cases.
"""
