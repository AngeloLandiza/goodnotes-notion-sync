"""Encryption for credentials held on someone else's behalf.

Once this app stores a classmate's Notion token, their Canvas token and their
Google refresh token, a database dump stops being an inconvenience and becomes
a breach of their accounts. A Canvas token in particular reads everything in
their LMS. So secrets are encrypted before they reach Postgres, and the key
lives only in the environment.

Fernet (AES-128-CBC + HMAC-SHA256) from `cryptography`, wrapped so that key
rotation is possible without a migration: `APP_ENCRYPTION_KEY` encrypts,
`APP_ENCRYPTION_KEY_OLD` can still decrypt.
"""

from __future__ import annotations

import base64
import os

__all__ = ["CryptoError", "SecretBox", "generate_key"]


class CryptoError(RuntimeError):
    pass


def generate_key() -> str:
    """A fresh key, printable. `python -m goodnotes_notion_sync keygen`."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _fernet_module():
    try:
        from cryptography.fernet import Fernet, InvalidToken, MultiFernet
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise CryptoError(
            "The `cryptography` package is required to store credentials. "
            "pip install -r requirements.txt"
        ) from exc
    return Fernet, MultiFernet, InvalidToken


class SecretBox:
    """Encrypt and decrypt one secret at a time."""

    def __init__(self, keys: list[str]) -> None:
        Fernet, MultiFernet, _ = _fernet_module()
        usable = [k.strip() for k in keys if k and k.strip()]
        if not usable:
            raise CryptoError(
                "No APP_ENCRYPTION_KEY set. Refusing to store credentials in "
                "plaintext -- generate one with `python -m "
                "goodnotes_notion_sync keygen`."
            )
        try:
            fernets = [Fernet(k.encode("ascii")) for k in usable]
        except Exception as exc:  # noqa: BLE001 - surfaces as a config error
            raise CryptoError(
                f"APP_ENCRYPTION_KEY is not a valid Fernet key: {exc}. It must "
                "be 32 url-safe base64 bytes; `keygen` prints one."
            ) from exc
        # First key wins for encryption; every key can still decrypt.
        self._box = MultiFernet(fernets)
        self._key_count = len(fernets)

    @property
    def key_count(self) -> int:
        return self._key_count

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "SecretBox":
        env = environ if environ is not None else os.environ
        return cls(
            [
                env.get("APP_ENCRYPTION_KEY", ""),
                env.get("APP_ENCRYPTION_KEY_OLD", ""),
            ]
        )

    @classmethod
    def optional(cls, environ: dict | None = None) -> "SecretBox | None":
        """A box, or None when no key is configured.

        For code paths that must keep working without a database -- never for
        one that is about to write a secret.
        """
        try:
            return cls.from_env(environ)
        except CryptoError:
            return None

    def encrypt(self, plaintext: str) -> bytes:
        if not isinstance(plaintext, str):
            raise CryptoError("Only text secrets are stored")
        return self._box.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, blob: bytes | memoryview | None) -> str:
        if not blob:
            return ""
        _, _, InvalidToken = _fernet_module()
        try:
            return self._box.decrypt(bytes(blob)).decode("utf-8")
        except InvalidToken as exc:
            raise CryptoError(
                "Could not decrypt a stored credential. The most likely cause "
                "is that APP_ENCRYPTION_KEY changed; put the previous value in "
                "APP_ENCRYPTION_KEY_OLD to read old rows while new ones are "
                "written with the new key."
            ) from exc

    def rotate(self, blob: bytes | memoryview) -> bytes:
        """Re-encrypt an old-key value under the current key."""
        return self._box.rotate(bytes(blob))
