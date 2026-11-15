"""NewsData.io 免费新闻 API：Free 计划。

免费额度（官网 newsdata.io/pricing，2026-11-15 核实）：每天 200 credits，
文章延迟 12 小时，过去 48 小时内容；/latest 每次调用 1 credit、免费档
最多 10 篇；免费档限速 30 次/15 分钟；无需信用卡。见 docs/NEWS_APIS.md。

未配置 NEWSDATA_API_KEY 时返回 []，并记录“未启用”。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

from trend_radar.models import Item

logger = logging.getLogger(__name__)
BASE_URL = "https://newsdata.io/api/1/latest"


def fetch(queries: list[str], size: int = 10, timeout: int = 15) -> list[dict]:
    """按查询词取最新文章（/latest，每次 1 credit）。"""
    api_key = os.environ.get("NEWSDATA_API_KEY", "")
    if not api_key:
        logger.info("NewsData 未启用：未配置 NEWSDATA_API_KEY")
        return []
    results: list[dict] = []
    for query in queries:
        try:
            resp = requests.get(
                BASE_URL,
                params={
                    "apikey": api_key,
                    "q": query,
                    "language": "en",
                    "size": size,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            items = resp.json().get("results", [])
        except Exception as exc:
            logger.warning("NewsData 采集失败 %r: %s", query, exc)
            continue
        for art in items:
            title = str(art.get("title") or "").strip()
            url = str(art.get("link") or "").strip()
            if not title or not url:
                continue
            results.append(
                Item(
                    platform="newsdata",
                    title=title,
                    url=url,
                    published_at=str(art.get("pubDate") or "") or None,
                    first_seen=datetime.now(timezone.utc).isoformat(),
                    engagement=0,
                    source=str(art.get("source_name") or "") or "newsdata",
                ).to_dict()
            )
    logger.info("NewsData: 采集到 %d 条", len(results))
    return results
