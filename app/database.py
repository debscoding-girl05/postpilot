"""SQLAlchemy 2.x async models + engine for PostPilot.

Uses a single SQLite database (data/postpilot.db) accessed via aiosqlite for the
app's own tables. Note: APScheduler uses the *sync* sqlite driver against the same
file for its job store (see app/scheduler.py) -- SQLite handles the concurrent
access fine for this single-process app.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "postpilot.db"

# Main DB: SQLite for dev; set DATABASE_URL (e.g. postgresql+asyncpg://...) to switch.
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite+aiosqlite:///{DB_PATH}"

# APScheduler's job store uses a *sync* engine. It only stores scheduled-job metadata
# (post id + run time), so it stays on SQLite for now even if the main DB is Postgres.
SYNC_DATABASE_URL = f"sqlite:///{DB_PATH}"


def utcnow() -> datetime:
    """Timezone-aware UTC now (stored naive in SQLite but conceptually UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Token the user's local agent uses to authenticate (Phase 2). Nullable for now.
    agent_token: Mapped[str | None] = mapped_column(String(64), index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active")
    auth_data: Mapped[str | None] = mapped_column(Text)  # encrypted JSON or empty
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used: Mapped[datetime | None] = mapped_column(DateTime)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "platform": self.platform,
            "username": self.username,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used": self.last_used.isoformat() if self.last_used else None,
        }


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    media_paths: Mapped[str | None] = mapped_column(Text)  # JSON array of paths
    platforms: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)

    def to_dict(self) -> dict:
        import json

        return {
            "id": self.id,
            "content": self.content,
            "media_paths": json.loads(self.media_paths) if self.media_paths else [],
            "platforms": json.loads(self.platforms) if self.platforms else [],
            "scheduled_for": self.scheduled_for.isoformat() if self.scheduled_for else None,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "notes": self.notes,
        }


class PostResult(Base):
    __tablename__ = "post_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # success/failed/skipped
    platform_post_id: Mapped[str | None] = mapped_column(Text)
    error_msg: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "post_id": self.post_id,
            "platform": self.platform,
            "status": self.status,
            "platform_post_id": self.platform_post_id,
            "error_msg": self.error_msg,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
        }


# --- Engine / session factory -------------------------------------------------

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Create tables if they don't exist. Called on startup."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "sessions").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "media").mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_session() -> AsyncSession:
    """Return a new AsyncSession. Caller is responsible for closing/using `async with`."""
    return async_session_maker()


__all__ = [
    "User",
    "Account",
    "Post",
    "PostResult",
    "Base",
    "engine",
    "async_session_maker",
    "init_db",
    "get_session",
    "select",
    "utcnow",
    "DATA_DIR",
    "DB_PATH",
    "SYNC_DATABASE_URL",
]
