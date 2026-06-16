"""Mastodon driver using Mastodon.py.

auth_data carries {"instance_url": ..., "access_token": ...}.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from app.content_processor import process_image_for_platform
from app.platforms.base import BasePlatform, PostPayload


class MastodonPlatform(BasePlatform):
    name = "mastodon"
    char_limit = 500
    supports_images = True
    supports_video = True

    def __init__(self) -> None:
        self._auth: dict = {}

    def _client(self, auth_data: dict):
        from mastodon import Mastodon

        return Mastodon(
            access_token=auth_data["access_token"],
            api_base_url=auth_data["instance_url"],
        )

    async def authenticate(self, auth_data: dict) -> bool:
        if not auth_data.get("instance_url") or not auth_data.get("access_token"):
            return False
        self._auth = auth_data
        try:
            await asyncio.to_thread(lambda: self._client(auth_data).account_verify_credentials())
            return True
        except Exception:
            return False

    def _post_sync(self, payload: PostPayload) -> str:
        client = self._client(self._auth)
        caption = self.adapt_caption(payload.content)

        media_ids = []
        # Mastodon allows up to 4 attachments.
        for path in payload.media_paths[:4]:
            suffix = path.suffix.lower()
            if suffix in (".mp4", ".mov", ".webm"):
                media = client.media_post(str(path))
            else:
                img_bytes = process_image_for_platform(path, "mastodon")
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name
                try:
                    media = client.media_post(tmp_path, mime_type="image/png")
                finally:
                    Path(tmp_path).unlink(missing_ok=True)
            media_ids.append(media["id"])

        if media_ids:
            status = client.status_post(status=caption, media_ids=media_ids)
        else:
            status = client.status_post(status=caption)
        return str(status["id"])

    async def post(self, payload: PostPayload) -> str:
        return await asyncio.to_thread(self._post_sync, payload)


__all__ = ["MastodonPlatform"]
