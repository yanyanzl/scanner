"""Guardian Open Platform 免费 API：Developer key。

免费额度：500 次请求/天、1 次/秒，非商业用途，无需信用卡。
数字来源：2026-10-22 对官网（open-platform.theguardian.com）的核查
与多方 2026 年资料一致；旧文档有 5,000 次/天、12 次/秒的说法——
本项目按保守的 500 次/天使用，两种说法下都不会超限。见 docs/NEWS_APIS.md。

未配置 GUARDIAN_API_KEY 时返回 []，并记录“未启用”。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

from trend_radar.models import Item

logger = logging.getLogger(__name__)
BASE_URL = "https://content.guardianapis.com/search"


def fetch(queries: list[str], page_size: int = 20, timeout: int = 15) -> list[dict]:
    """按查询词搜索最新文章（order-by=newest）。"""
    api_key = os.environ.get("GUARDIAN_API_KEY", "")
    if not api_key:
        logger.info("Guardian 未启用：未配置 GUARDIAN_API_KEY")
        return []
    results: list[dict] = []
    for query in queries:
        try:
            resp = requests.get(
                BASE_URL,
                params={
                    "q": query,
                    "order-by": "newest",
                    "page-size": page_size,
                    "api-key": api_key,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            items = resp.json().get("response", {}).get("results", [])
        except Exception as exc:
            logger.warning("Guardian 采集失败 %r: %s", query, exc)
            continue
        for art in items:
            title = str(art.get("webTitle") or "").strip()
            url = str(art.get("webUrl") or "").strip()
            if not title or not url:
                continue
            results.append(
                Item(
                    platform="guardian",
                    title=title,
                    url=url,
                    published_at=str(art.get("webPublicationDate") or "") or None,
                    first_seen=datetime.now(timezone.utc).isoformat(),
                    engagement=0,
                    source=str(art.get("sectionName") or "") or "guardian",
                ).to_dict()
            )
    logger.info("Guardian: 采集到 %d 条", len(results))
    return results
