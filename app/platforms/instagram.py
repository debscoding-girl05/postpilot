"""Instagram driver using instagrapi with a saved session.

Session lives at data/sessions/instagram.json (instagrapi settings dump). Instagram
does not support text-only posts, so a post with no media is skipped.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from app.content_processor import process_image_for_platform
from app.platforms.base import BasePlatform, PostPayload

SESSION_PATH = Path("data/sessions/instagram.json")


class InstagramSessionExpired(Exception):
    """Raised when the saved session is no longer valid (login/challenge required)."""


class InstagramPlatform(BasePlatform):
    name = "instagram"
    char_limit = 2200
    supports_images = True
    supports_video = True

    def _client(self):
        from instagrapi import Client

        cl = Client()
        if not SESSION_PATH.exists():
            return cl
        data = json.loads(SESSION_PATH.read_text())
        if isinstance(data, dict) and "cookies" in data:
            # Playwright storage_state from the browser-login flow — instagrapi
            # logs in from the web session's `sessionid` cookie.
            sessionid = next(
                (c["value"] for c in data["cookies"] if c["name"] == "sessionid"), None
            )
            if not sessionid:
                raise InstagramSessionExpired("No Instagram sessionid in captured session")
            cl.login_by_sessionid(sessionid)
        else:
            cl.load_settings(str(SESSION_PATH))
        return cl

    async def authenticate(self, auth_data: dict) -> bool:
        if not SESSION_PATH.exists():
            return False

        def _check() -> bool:
            from instagrapi.exceptions import ChallengeRequired, LoginRequired

            cl = self._client()
            try:
                cl.get_timeline_feed()  # cheap authenticated call
                return True
            except (LoginRequired, ChallengeRequired):
                return False
            except Exception:
                return False

        try:
            return await asyncio.to_thread(_check)
        except Exception:
            return False

    def _post_sync(self, payload: PostPayload) -> str:
        from instagrapi.exceptions import ChallengeRequired, LoginRequired

        if not payload.media_paths:
            raise ValueError("Instagram requires at least one image or video")

        caption = self.adapt_caption(payload.content)
        paths = [Path(p) for p in payload.media_paths]

        # Separate videos from images.
        videos = [p for p in paths if p.suffix.lower() in (".mp4", ".mov")]
        images = [p for p in paths if p not in videos]

        tmp_files: list[str] = []
        try:
            cl = self._client()
            if videos:
                media = cl.video_upload(str(videos[0]), caption)
            else:
                processed: list[Path] = []
                for p in images:
                    img_bytes = process_image_for_platform(p, "instagram")
                    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                    tmp.write(img_bytes)
                    tmp.close()
                    tmp_files.append(tmp.name)
                    processed.append(Path(tmp.name))

                if len(processed) == 1:
                    media = cl.photo_upload(str(processed[0]), caption)
                else:
                    media = cl.album_upload([str(p) for p in processed], caption)
            return str(media.pk)
        except (LoginRequired, ChallengeRequired) as exc:
            raise InstagramSessionExpired(str(exc)) from exc
        finally:
            for f in tmp_files:
                Path(f).unlink(missing_ok=True)

    async def post(self, payload: PostPayload) -> str:
        return await asyncio.to_thread(self._post_sync, payload)


__all__ = ["InstagramPlatform", "InstagramSessionExpired"]
