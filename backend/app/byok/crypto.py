"""Server-side secret encryption for BYOK credentials — issue #257.

Single canonical key: ``BYOK_ENCRYPTION_KEY`` (a Fernet key, generated via
:func:`generate_key`). Encryption/decryption fail closed when the key is
absent — credentials can neither be stored nor used without server-side key
management. Secrets are bytes in memory only at the call site; this module
never logs them.
"""

from __future__ import annotations

import os

from app.byok.errors import ByokError, SERVER_MISCONFIGURED


def encryption_key() -> bytes:
    raw = (os.environ.get("BYOK_ENCRYPTION_KEY") or "").strip()
    if not raw:
        raise ByokError(
            "BYOK_ENCRYPTION_KEY is not set; credential storage is unavailable",
            kind=SERVER_MISCONFIGURED,
        )
    return raw.encode("utf-8")


def generate_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("utf-8")


def _fernet():
    from cryptography.fernet import Fernet, InvalidToken

    try:
        return Fernet(encryption_key()), InvalidToken
    except ByokError:
        raise
    except Exception as exc:
        raise ByokError(
            "BYOK_ENCRYPTION_KEY is not a valid Fernet key",
            kind=SERVER_MISCONFIGURED,
        ) from exc


def encrypt_secret(secret: str) -> str:
    """Encrypt a raw provider secret to storable ciphertext."""
    if not isinstance(secret, str) or not secret.strip():
        from app.byok.errors import ByokError, MALFORMED

        raise ByokError("provider secret must be a non-empty string", kind=MALFORMED)
    fernet, _ = _fernet()
    return fernet.encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt server-side only. Callers must never return or log the result."""
    fernet, InvalidToken = _fernet()
    try:
        return fernet.decrypt((ciphertext or "").encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ByokError(
            "credential cannot be decrypted with the current server key",
            kind=SERVER_MISCONFIGURED,
        ) from exc
