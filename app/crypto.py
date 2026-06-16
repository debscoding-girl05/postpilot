"""Symmetric encryption for stored credentials (Bluesky/Mastodon auth_data).

We derive a stable Fernet key from APP_SECRET so the same secret always produces
the same key across restarts. Never store plaintext passwords/tokens in the DB.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    secret = os.getenv("APP_SECRET", "change-this-to-random-string")
    # Derive a 32-byte key from the secret, then urlsafe-base64 encode for Fernet.
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_json(data: dict) -> str:
    raw = json.dumps(data).encode("utf-8")
    return _fernet().encrypt(raw).decode("utf-8")


def decrypt_json(token: str | None) -> dict:
    if not token:
        return {}
    try:
        raw = _fernet().decrypt(token.encode("utf-8"))
        return json.loads(raw.decode("utf-8"))
    except (InvalidToken, ValueError):
        return {}


__all__ = ["encrypt_json", "decrypt_json"]
