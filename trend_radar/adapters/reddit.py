"""Reddit 采集：官方 OAuth（client credentials），需配置凭证。

未配置 REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET 时返回 []，
并记录“未启用”，绝不假装已采集到数据，也不绕过访问限制。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

from trend_radar.models import Item

logger = logging.getLogger(__name__)


def _get_token() -> str | None:
    client_id = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        logger.info("Reddit 未启用：未配置 REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET")
        return None
    try:
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": "GlobalTrendRadar/0.1"},
            timeout=15,
        )
        resp.raise_for_status()
        return str(resp.json().get("access_token", ""))
    except requests.RequestException as exc:
        logger.warning("Reddit 获取 token 失败: %s", exc)
        return None


def fetch(subreddits: list[str], limit: int = 25, timeout: int = 15) -> list[dict]:
    """采集指定 subreddit 的热门帖子，返回归一化 Item dict 列表。"""
    token = _get_token()
    if not token:
        return []
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "GlobalTrendRadar/0.1",
    }
    results: list[dict] = []
    for sub in subreddits:
        try:
            resp = requests.get(
                f"https://oauth.reddit.com/r/{sub}/hot",
                headers=headers,
                params={"limit": limit},
                timeout=timeout,
            )
            resp.raise_for_status()
            posts = resp.json().get("data", {}).get("children", [])
        except requests.RequestException as exc:
            logger.warning("Reddit 采集失败 r/%s: %s", sub, exc)
            continue
        for child in posts:
            data = child.get("data", {})
            title = data.get("title", "")
            url = data.get("url", "")
            if not title or not url:
                continue
            created = data.get("created_utc")
            published = (
                datetime.fromtimestamp(float(created), tz=timezone.utc).isoformat()
                if created
                else None
            )
            score = float(data.get("score") or 0)
            comments = float(data.get("num_comments") or 0)
            results.append(
                Item(
                    platform="reddit",
                    title=str(title).strip(),
                    url=str(url).strip(),
                    published_at=published,
                    first_seen=datetime.now(timezone.utc).isoformat(),
                    engagement=Item.engagement_from_parts(likes=score, comments=comments),
                    likes=score,
                    comments=comments,
                    source=f"r/{sub}",
                ).to_dict()
            )
    logger.info("Reddit: 采集到 %d 条", len(results))
    return results
