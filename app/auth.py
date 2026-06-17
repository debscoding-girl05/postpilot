"""Authentication: argon2 password hashing + a JWT session cookie.

The JWT (signed with APP_SECRET) is stored in an HTTP-only cookie, so the
same-origin SPA sends it automatically on every fetch. `current_user` is a
FastAPI dependency that resolves the logged-in user or raises 401.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, Request

from app.database import User, get_session

COOKIE_NAME = "pp_session"
TOKEN_TTL_DAYS = 30
_ph = PasswordHasher()


def _secret() -> str:
    return os.getenv("APP_SECRET", "change-this-to-random-string")


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(password_hash: str, pw: str) -> bool:
    try:
        return _ph.verify(password_hash, pw)
    except (VerifyMismatchError, Exception):
        return False


def create_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=TOKEN_TTL_DAYS)).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, _secret(), algorithms=["HS256"])
        return int(payload["sub"])
    except Exception:
        return None


def new_agent_token() -> str:
    """Token the user's local agent will use to authenticate (Phase 2)."""
    return "ppa_" + secrets.token_urlsafe(32)


async def current_user(request: Request) -> User:
    """FastAPI dependency: resolve the logged-in user from the session cookie."""
    token = request.cookies.get(COOKIE_NAME)
    user_id = decode_token(token) if token else None
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    async with get_session() as session:
        user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(401, "Not authenticated")
    return user


async def user_from_agent_token(token: str) -> User | None:
    """Resolve a user by their agent token (Phase 2 agent API)."""
    if not token:
        return None
    from app.database import select

    async with get_session() as session:
        result = await session.execute(select(User).where(User.agent_token == token))
        return result.scalars().first()


__all__ = [
    "current_user",
    "hash_password",
    "verify_password",
    "create_token",
    "decode_token",
    "new_agent_token",
    "user_from_agent_token",
    "COOKIE_NAME",
    "TOKEN_TTL_DAYS",
]
