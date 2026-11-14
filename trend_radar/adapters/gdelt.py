"""GDELT 采集：全球新闻事件数据库公开 API，无需凭证。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from trend_radar.models import Item

logger = logging.getLogger(__name__)
API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def fetch(query: str = "", max_records: int = 50, timeout: int = 15) -> list[dict]:
    """查询 GDELT Doc API，返回归一化 Item dict 列表。"""
    if not query:
        logger.info("GDELT 未启用：config.yaml 中未配置查询词")
        return []
    params = {"query": query, "mode": "artlist", "maxrecords": max_records, "format": "json"}
    try:
        resp = requests.get(API_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("GDELT 采集失败: %s", exc)
        return []

    results: list[dict] = []
    for article in data.get("articles", []):
        title = article.get("title", "")
        url = article.get("url", "")
        if not title or not url:
            continue
        results.append(
            Item(
                platform="gdelt",
                title=str(title).strip(),
                url=str(url).strip(),
                published_at=article.get("seendate"),
                first_seen=datetime.now(timezone.utc).isoformat(),
                source=article.get("domain", "gdelt"),
                language=article.get("language", ""),
            ).to_dict()
        )
    logger.info("GDELT: 采集到 %d 条", len(results))
    return results
