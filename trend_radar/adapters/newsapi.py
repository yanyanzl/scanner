"""NewsAPI.org 免费新闻 API：Developer 计划。

免费额度（官网 newsapi.org/pricing，2026-11-15 核实；100 次/天与
24 小时延迟见官网定价页的多方引用，官网 FAQ 确认 Developer 计划
仅限开发测试环境）：100 次请求/天，文章延迟约 24 小时，仅限开发
测试环境使用（不能用于生产环境，包括内部工具），无需信用卡。
见 docs/NEWS_APIS.md。

未配置 NEWSAPI_KEY 时返回 []，并记录“未启用”，绝不假装已采集到数据。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

from trend_radar.models import Item

logger = logging.getLogger(__name__)
BASE_URL = "https://newsapi.org/v2/everything"


def fetch(queries: list[str], page_size: int = 20, timeout: int = 15) -> list[dict]:
    """按查询词搜索最新文章（/v2/everything，sortBy=publishedAt）。"""
    api_key = os.environ.get("NEWSAPI_KEY", "")
    if not api_key:
        logger.info("NewsAPI 未启用：未配置 NEWSAPI_KEY")
        return []
    results: list[dict] = []
    for query in queries:
        try:
            resp = requests.get(
                BASE_URL,
                params={
                    "q": query,
                    "sortBy": "publishedAt",
                    "pageSize": page_size,
                    "language": "en",
                    "apiKey": api_key,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
        except Exception as exc:
            # RequestException（429/401/网络错）与 JSON 解析失败都一样处理：
            # 记一条 warning，跳过这个查询，不中断本轮。
            logger.warning("NewsAPI 采集失败 %r: %s", query, exc)
            continue
        for art in articles:
            title = str(art.get("title") or "").strip()
            url = str(art.get("url") or "").strip()
            if not title or not url:
                continue
            if title == "[Removed]":
                # NewsAPI 对已下架文章返回的占位标题，不是真实标题。
                continue
            source = art.get("source") or {}
            results.append(
                Item(
                    platform="newsapi",
                    title=title,
                    url=url,
                    published_at=str(art.get("publishedAt") or "") or None,
                    first_seen=datetime.now(timezone.utc).isoformat(),
                    engagement=0,
                    source=str(source.get("name") or "") or "newsapi",
                ).to_dict()
            )
    logger.info("NewsAPI: 采集到 %d 条", len(results))
    return results
