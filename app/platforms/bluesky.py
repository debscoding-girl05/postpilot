"""Bluesky driver using the atproto SDK.

Stateless: we log in with the app password on every post. auth_data carries
{"identifier": ..., "app_password": ...}.
"""
from __future__ import annotations

import asyncio

from app.content_processor import process_image_for_platform
from app.platforms.base import BasePlatform, PostPayload


class BlueskyPlatform(BasePlatform):
    name = "bluesky"
    char_limit = 300
    supports_images = True
    supports_video = True

    def __init__(self) -> None:
        self._auth: dict = {}

    def _login_sync(self, auth_data: dict):
        from atproto import Client

        client = Client()
        client.login(auth_data["identifier"], auth_data["app_password"])
        return client

    async def authenticate(self, auth_data: dict) -> bool:
        if not auth_data.get("identifier") or not auth_data.get("app_password"):
            return False
        self._auth = auth_data
        try:
            await asyncio.to_thread(self._login_sync, auth_data)
            return True
        except Exception:
            return False

    def _post_sync(self, payload: PostPayload) -> str:
        from atproto import models

        client = self._login_sync(self._auth)
        caption = self.adapt_caption(payload.content)

        # A single video takes priority: Bluesky allows one video per post (no images).
        video = next(
            (p for p in payload.media_paths
             if str(p).lower().endswith((".mp4", ".mov", ".webm"))),
            None,
        )
        if video:
            with open(video, "rb") as fh:
                response = client.send_video(text=caption, video=fh.read(), video_alt="")
            return response.uri

        if payload.media_paths:
            images = []
            # Bluesky allows up to 4 images per post.
            for path in payload.media_paths[:4]:
                blob_bytes = process_image_for_platform(path, "bluesky")
                upload = client.upload_blob(blob_bytes)
                images.append(
                    models.AppBskyEmbedImages.Image(alt="", image=upload.blob)
                )
            embed = models.AppBskyEmbedImages.Main(images=images)
            response = client.send_post(text=caption, embed=embed)
        else:
            response = client.send_post(text=caption)

        # response.uri looks like at://did/app.bsky.feed.post/<rkey>
        return response.uri

    async def post(self, payload: PostPayload) -> str:
        return await asyncio.to_thread(self._post_sync, payload)


__all__ = ["BlueskyPlatform"]
