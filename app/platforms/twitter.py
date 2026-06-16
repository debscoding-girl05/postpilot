"""Twitter/X driver using twikit with the browser session captured at connect.

The connect flow saves a Playwright storage_state JSON (cookies + origins), but
twikit wants a plain {name: value} cookie dict — so we convert it. twikit is async
natively, so no thread offloading is needed.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.content_processor import process_image_for_platform
from app.platforms.base import BasePlatform, PostPayload

SESSION_PATH = Path("data/sessions/twitter.json")


def _cookies_from_storage_state() -> dict:
    """Convert a Playwright storage_state file into twikit's {name: value} dict."""
    data = json.loads(SESSION_PATH.read_text())
    if isinstance(data, dict) and "cookies" in data:
        return {c["name"]: c["value"] for c in data["cookies"]}
    # Already a plain name->value dict (twikit's own format).
    return data


class TwitterPlatform(BasePlatform):
    name = "twitter"
    char_limit = 280
    supports_images = True
    supports_video = True

    def _client(self):
        from twikit import Client

        client = Client("en-US")
        client.set_cookies(_cookies_from_storage_state())
        return client

    async def authenticate(self, auth_data: dict) -> bool:
        if not SESSION_PATH.exists():
            return False
        try:
            client = self._client()
            await client.user()  # resolves the authenticated user
            return True
        except Exception:
            return False

    async def post(self, payload: PostPayload) -> str:
        client = self._client()
        caption = self.adapt_caption(payload.content)

        media_ids: list[str] = []
        tmp_files: list[str] = []
        try:
            for path in payload.media_paths[:4]:
                path = Path(path)
                suffix = path.suffix.lower()
                if suffix in (".mp4", ".mov"):
                    media_id = await client.upload_media(str(path))
                else:
                    img_bytes = process_image_for_platform(path, "twitter")
                    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    tmp.write(img_bytes)
                    tmp.close()
                    tmp_files.append(tmp.name)
                    media_id = await client.upload_media(tmp.name)
                media_ids.append(media_id)

            if media_ids:
                tweet = await client.create_tweet(text=caption, media_ids=media_ids)
            else:
                tweet = await client.create_tweet(text=caption)
            return str(tweet.id)
        finally:
            for f in tmp_files:
                Path(f).unlink(missing_ok=True)


__all__ = ["TwitterPlatform"]
