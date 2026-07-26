"""Publishing credential ownership (ADR-0047) — the *only* place OAuth tokens decrypt.

This package is the credential-service boundary (C7). It is the sole importer of the
``cryptography`` primitives (import-linter enforced): the envelope cipher wraps a per-record
data key with an externally-managed master key and encrypts the tokens with AES-256-GCM.
Nothing above this seam ever sees plaintext token material or key bytes.
"""
