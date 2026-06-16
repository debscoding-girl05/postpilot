"""Platform driver registry.

Drivers are imported lazily (inside get_driver) so that a missing optional
dependency for one platform never breaks the whole app.
"""
from __future__ import annotations

from app.platforms.base import BasePlatform, PostPayload

SUPPORTED_PLATFORMS = [
    "bluesky",
    "mastodon",
    "instagram",
    "twitter",
    "linkedin",
    "tiktok",
]

# Platforms that use stored API credentials (auth_data) vs a captured browser session.
CREDENTIAL_PLATFORMS = {"bluesky", "mastodon"}
SESSION_PLATFORMS = {"instagram", "twitter", "linkedin", "tiktok"}


def get_driver(platform: str) -> BasePlatform:
    """Instantiate the driver for a platform. Raises ValueError if unknown."""
    if platform == "bluesky":
        from app.platforms.bluesky import BlueskyPlatform

        return BlueskyPlatform()
    if platform == "mastodon":
        from app.platforms.mastodon import MastodonPlatform

        return MastodonPlatform()
    if platform == "instagram":
        from app.platforms.instagram import InstagramPlatform

        return InstagramPlatform()
    if platform == "twitter":
        from app.platforms.twitter import TwitterPlatform

        return TwitterPlatform()
    if platform == "linkedin":
        from app.platforms.linkedin import LinkedInPlatform

        return LinkedInPlatform()
    if platform == "tiktok":
        from app.platforms.tiktok import TikTokPlatform

        return TikTokPlatform()
    raise ValueError(f"Unknown platform: {platform}")


__all__ = [
    "BasePlatform",
    "PostPayload",
    "get_driver",
    "SUPPORTED_PLATFORMS",
    "CREDENTIAL_PLATFORMS",
    "SESSION_PLATFORMS",
]
