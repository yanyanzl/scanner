"""YouTube 采集：官方 YouTube Data API v3，需配置 API Key。

未配置 YOUTUBE_API_KEY 时返回 []，并记录“未启用”，
绝不假装已采集到数据。注意默认配额为每天 10,000 units，
搜索请求消耗较高，请按配额控制扫描频率和查询数量。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

from trend_radar.models import Item

logger = logging.getLogger(__name__)
SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


def fetch(queries: list[str], max_results: int = 10, timeout: int = 15) -> list[dict]:
    """按查询词搜索最新视频，并补齐播放量/点赞数等统计数据。"""
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        logger.info("YouTube 未启用：未配置 YOUTUBE_API_KEY")
        return []
    results: list[dict] = []
    for query in queries:
        try:
            resp = requests.get(
                SEARCH_URL,
                params={
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "order": "date",
                    "maxResults": max_results,
                    "key": api_key,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            search_items = resp.json().get("items", [])
        except requests.RequestException as exc:
            logger.warning("YouTube 搜索失败 %r: %s", query, exc)
            continue
        video_ids = [it.get("id", {}).get("videoId") for it in search_items]
        video_ids = [v for v in video_ids if v]
        stats: dict[str, dict] = {}
        if video_ids:
            try:
                resp = requests.get(
                    VIDEOS_URL,
                    params={"part": "statistics,snippet", "id": ",".join(video_ids), "key": api_key},
                    timeout=timeout,
                )
                resp.raise_for_status()
                for v in resp.json().get("items", []):
                    stats[v["id"]] = v
            except requests.RequestException as exc:
                logger.warning("YouTube 统计数据获取失败: %s", exc)
        for it in search_items:
            video_id = it.get("id", {}).get("videoId")
            snippet = it.get("snippet", {})
            if not video_id or not snippet.get("title"):
                continue
            st = stats.get(video_id, {}).get("statistics", {})
            views = float(st.get("viewCount") or 0)
            likes = float(st.get("likeCount") or 0)
            comments = float(st.get("commentCount") or 0)
            results.append(
                Item(
                    platform="youtube",
                    title=str(snippet["title"]).strip(),
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    published_at=snippet.get("publishedAt"),
                    first_seen=datetime.now(timezone.utc).isoformat(),
                    engagement=Item.engagement_from_parts(views=views, likes=likes, comments=comments),
                    views=views,
                    likes=likes,
                    comments=comments,
                    source=snippet.get("channelTitle", ""),
                ).to_dict()
            )
    logger.info("YouTube: 采集到 %d 条", len(results))
    return results
