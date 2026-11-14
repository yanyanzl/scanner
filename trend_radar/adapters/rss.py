"""RSS 采集：公开 RSS 源，无需凭证，低成本基础数据源。"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from trend_radar.models import Item

logger = logging.getLogger(__name__)


def _parse_with_stdlib(content: bytes, feed_url: str) -> list[dict]:
    """无 feedparser 时的备用解析（标准库 RSS 解析）。"""
    items: list[dict] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.warning("RSS 解析失败 %s: %s", feed_url, exc)
        return items
    for entry in root.iter("item"):
        title = entry.findtext("title") or ""
        link = entry.findtext("link") or ""
        pub = entry.findtext("pubDate") or ""
        if title and link:
            items.append(
                Item(
                    platform="rss",
                    title=title.strip(),
                    url=link.strip(),
                    published_at=pub.strip() or None,
                    first_seen=datetime.now(timezone.utc).isoformat(),
                    source=feed_url,
                ).to_dict()
            )
    return items


def fetch(feed_urls: list[str], timeout: int = 15) -> list[dict]:
    """采集一组 RSS 源，返回归一化 Item dict 列表。"""
    results: list[dict] = []
    try:
        import feedparser  # type: ignore
    except ImportError:
        feedparser = None
        logger.info("未安装 feedparser，使用标准库解析 RSS")

    for feed_url in feed_urls:
        try:
            resp = requests.get(
                feed_url, timeout=timeout, headers={"User-Agent": "GlobalTrendRadar/0.1"}
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("RSS 采集失败 %s: %s", feed_url, exc)
            continue

        if feedparser is not None:
            feed = feedparser.parse(resp.content)
            for entry in feed.entries:
                title = entry.get("title", "")
                link = entry.get("link", "")
                if not title or not link:
                    continue
                published = entry.get("published") or entry.get("updated") or ""
                results.append(
                    Item(
                        platform="rss",
                        title=str(title).strip(),
                        url=str(link).strip(),
                        published_at=str(published).strip() or None,
                        first_seen=datetime.now(timezone.utc).isoformat(),
                        source=feed_url,
                    ).to_dict()
                )
        else:
            results.extend(_parse_with_stdlib(resp.content, feed_url))

        logger.info("RSS %s: 采集到 %d 条", feed_url, len(results))
    return results
