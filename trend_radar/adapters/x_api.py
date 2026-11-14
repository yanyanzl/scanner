"""X（Twitter）采集：官方 X API v2，需配置有权限的 Bearer Token。

未配置 X_BEARER_TOKEN 时返回 []，并记录“未启用”。
X API 按付费套餐计费，不假设存在普通开发者长期免费读取途径，
也不使用绕过访问限制的爬虫。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

from trend_radar.models import Item

logger = logging.getLogger(__name__)
SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"


def fetch(queries: list[str], max_results: int = 10, timeout: int = 15) -> list[dict]:
    """搜索最近帖子，返回归一化 Item dict 列表。"""
    token = os.environ.get("X_BEARER_TOKEN", "")
    if not token:
        logger.info("X 未启用：未配置 X_BEARER_TOKEN（按付费服务处理）")
        return []
    headers = {"Authorization": f"Bearer {token}"}
    results: list[dict] = []
    for query in queries:
        try:
            resp = requests.get(
                SEARCH_URL,
                headers=headers,
                params={
                    "query": query,
                    "max_results": max_results,
                    "tweet.fields": "created_at,public_metrics",
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            tweets = resp.json().get("data", [])
        except requests.RequestException as exc:
            logger.warning("X 采集失败 %r: %s", query, exc)
            continue
        for tweet in tweets:
            text = tweet.get("text", "")
            tweet_id = tweet.get("id", "")
            if not text or not tweet_id:
                continue
            metrics = tweet.get("public_metrics", {})
            likes = float(metrics.get("like_count") or 0)
            shares = float(metrics.get("retweet_count") or 0)
            comments = float(metrics.get("reply_count") or 0)
            views = float(metrics.get("impression_count") or 0)
            results.append(
                Item(
                    platform="x",
                    title=str(text).strip()[:200],
                    url=f"https://x.com/i/status/{tweet_id}",
                    published_at=tweet.get("created_at"),
                    first_seen=datetime.now(timezone.utc).isoformat(),
                    engagement=Item.engagement_from_parts(
                        views=views, likes=likes, shares=shares, comments=comments
                    ),
                    views=views,
                    likes=likes,
                    shares=shares,
                    comments=comments,
                    source="X",
                ).to_dict()
            )
    logger.info("X: 采集到 %d 条", len(results))
    return results
