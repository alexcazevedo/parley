"""Symmetric encryption for secrets-at-rest (tool auth configs, channel
webhook secrets) using Fernet with the app-wide `SECRETS_KEY`.
"""

from functools import lru_cache

from cryptography.fernet import Fernet

from app.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    return Fernet(settings.secrets_key.encode("utf-8"))


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string, returning a base64 token safe to store as text."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    """Decrypt a token produced by `encrypt`, returning the original plaintext."""
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
