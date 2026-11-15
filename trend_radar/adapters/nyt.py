"""NYT Article Search API（免费）。

免费额度（官网 developer.nytimes.com FAQ #11，多方 2026 年资料一致）：
每个 API 每天 500 次、每分钟 5 次；非商业用途，无需信用卡。
官网 FAQ 要求两次调用之间至少隔 12 秒——本模块在连续查询之间
sleep 12 秒。见 docs/NEWS_APIS.md。

未配置 NYT_API_KEY 时返回 []，并记录“未启用”。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import requests

from trend_radar.models import Item

logger = logging.getLogger(__name__)
BASE_URL = "https://api.nytimes.com/svc/search/v2/articlesearch.json"

# 每分钟 5 次上限：两次调用之间至少隔 12 秒（官网 FAQ 的要求）。
MIN_SECONDS_BETWEEN_CALLS = 12


def fetch(queries: list[str], timeout: int = 15) -> list[dict]:
    """按查询词搜索最新文章（sort=newest，每页 10 条，只取第 1 页）。"""
    api_key = os.environ.get("NYT_API_KEY", "")
    if not api_key:
        logger.info("NYT 未启用：未配置 NYT_API_KEY")
        return []
    results: list[dict] = []
    for index, query in enumerate(queries):
        if index > 0:
            time.sleep(MIN_SECONDS_BETWEEN_CALLS)
        try:
            resp = requests.get(
                BASE_URL,
                params={"q": query, "sort": "newest", "api-key": api_key},
                timeout=timeout,
            )
            resp.raise_for_status()
            docs = resp.json().get("response", {}).get("docs", [])
        except Exception as exc:
            logger.warning("NYT 采集失败 %r: %s", query, exc)
            continue
        for doc in docs:
            headline = doc.get("headline") or {}
            title = str(headline.get("main") or "").strip()
            url = str(doc.get("web_url") or "").strip()
            if not title or not url:
                continue
            results.append(
                Item(
                    platform="nyt",
                    title=title,
                    url=url,
                    published_at=str(doc.get("pub_date") or "") or None,
                    first_seen=datetime.now(timezone.utc).isoformat(),
                    engagement=0,
                    source="nytimes",
                ).to_dict()
            )
    logger.info("NYT: 采集到 %d 条", len(results))
    return results
