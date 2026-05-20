"""YouTube channel scraper service handler."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, ClassVar

import aiohttp

from swarm.logging import get_logger
from swarm.services.registry import ServiceContext, ServiceResult

_log = get_logger("services.youtube_scraper")

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


async def _fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, str],
) -> dict[str, Any]:
    """GET *url* with *params* and return parsed JSON."""
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        data: dict[str, Any] = await resp.json()
        return data


@dataclass
class YouTubeScraper:
    """Fetch recent videos from YouTube channels via the Data API v3."""

    description = "Fetch recent videos from YouTube channels (Data API v3)."
    example_config: ClassVar[dict[str, Any]] = {
        "api_key": "",
        "channels": ["UC_x5XG1OV2P6uZZ5FSM9Ttw"],
        "max_results": 10,
    }

    async def execute(
        self,
        config: dict[str, Any],
        context: ServiceContext,
    ) -> ServiceResult:
        api_key = config.get("api_key") or os.environ.get("YOUTUBE_API_KEY", "")
        if not api_key:
            return ServiceResult(
                success=False,
                error="Missing api_key in config and YOUTUBE_API_KEY env var",
            )

        channels: list[str] = config.get("channels", [])
        if not channels:
            return ServiceResult(success=False, error="No channels specified")

        max_results = config.get("max_results", 10)
        videos: list[dict[str, Any]] = []

        try:
            async with aiohttp.ClientSession() as session:
                # Search each channel for recent videos
                video_ids: list[str] = []
                for channel in channels:
                    params = {
                        "key": api_key,
                        "channelId": channel,
                        "part": "snippet",
                        "type": "video",
                        "order": "date",
                        "maxResults": str(max_results),
                    }
                    data = await _fetch_json(session, _SEARCH_URL, params)
                    for item in data.get("items", []):
                        vid = item.get("id", {}).get("videoId")
                        if vid:
                            video_ids.append(vid)

                if not video_ids:
                    return ServiceResult(data={"videos": []})

                # Batch-fetch video details
                details_params = {
                    "key": api_key,
                    "id": ",".join(video_ids),
                    "part": "snippet,statistics",
                }
                details = await _fetch_json(session, _VIDEOS_URL, details_params)
                for item in details.get("items", []):
                    snippet = item.get("snippet", {})
                    stats = item.get("statistics", {})
                    videos.append(
                        {
                            "video_id": item["id"],
                            "title": snippet.get("title", ""),
                            "description": snippet.get("description", ""),
                            "tags": snippet.get("tags", []),
                            "published_at": snippet.get("publishedAt", ""),
                            "thumbnail_url": snippet.get("thumbnails", {})
                            .get("high", {})
                            .get("url", ""),
                            "view_count": int(stats.get("viewCount", 0)),
                        }
                    )
        except aiohttp.ClientError as exc:
            _log.error("YouTube API error: %s", exc)
            return ServiceResult(success=False, error=f"HTTP error: {exc}")

        _log.info("fetched %d videos from %d channels", len(videos), len(channels))
        return ServiceResult(data={"videos": videos})
