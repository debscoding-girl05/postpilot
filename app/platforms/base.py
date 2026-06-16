"""Abstract base class shared by every platform driver."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PostPayload:
    content: str
    media_paths: list[Path] = field(default_factory=list)
    platform_options: dict = field(default_factory=dict)


class BasePlatform(ABC):
    name: str = "base"
    char_limit: int = 500
    supports_images: bool = True
    supports_video: bool = False

    @abstractmethod
    async def authenticate(self, auth_data: dict) -> bool:
        """Verify credentials/session are still valid. Returns True if usable."""
        ...

    @abstractmethod
    async def post(self, payload: PostPayload) -> str:
        """Post content. Returns the platform post ID on success, raises on failure."""
        ...

    def adapt_caption(self, text: str) -> str:
        """Truncate to char_limit, preserving meaning. Default: truncate + ellipsis."""
        if len(text) <= self.char_limit:
            return text
        return text[: self.char_limit - 3] + "..."


__all__ = ["BasePlatform", "PostPayload"]
