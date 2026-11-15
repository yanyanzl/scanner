"""搜索主题：config.yaml 里 topics 一节，所有需要关键词的来源共用。

来源：
- Google News / Bing RSS：每个主题一个搜索 feed（URL 生成见本模块），
  外加一个 Google News 头条 feed（不需要关键词，见下）。
- 免费新闻 API（NewsAPI / Guardian / NYT / NewsData）：每个主题调一次接口。
- GDELT（停用中）：重开时查询词由主题拼成。
- YouTube 不走这里：搜索桶 100 次/天，最多 2 个查询词，见 config.yaml。

改主题不用改代码：改 config.yaml 的 topics 一节，重启扫描器即可。
"""
from __future__ import annotations

from urllib.parse import quote_plus

# 默认主题：键是类别（只作可读性），值是该类别的查询词列表。
# 全部是英文关键词——所有来源都是英文内容，中文关键词匹配不到任何东西。
# 每个查询词字符串里的 OR 是来源接口自己的语法（Google/Bing/NewsAPI
# 都接受 OR），不要拆成多个词。
DEFAULT_TOPICS: dict[str, list[str]] = {
    "breaking": ["breaking news"],
    "economy": ["stock market OR inflation OR interest rates"],
    "tech": ["technology"],
    "ai": ["artificial intelligence OR AI"],
    "politics": ["war OR geopolitics"],
}

# Google News 头条 feed（不需要关键词）：2026-11-15 用项目自己的 RSS
# 解析实测可用，38 条，最新是当天的文章。这是“当前最热话题”最好的
# 来源，因为它不依赖任何关键词。
GOOGLE_NEWS_TOP_STORIES_URL = "https://news.google.com/rss?hl=en-GB&gl=GB&ceid=GB:en"

GOOGLE_NEWS_SEARCH_URL = "https://news.google.com/rss/search"
BING_NEWS_SEARCH_URL = "https://www.bing.com/news/search"


def default_topics() -> list[str]:
    """默认主题拍扁后的查询词列表（保持配置里的顺序）。"""
    queries: list[str] = []
    for group in DEFAULT_TOPICS.values():
        queries.extend(group)
    return queries


def load_topics(cfg: dict) -> list[str]:
    """从 config 读 topics 一节，拍扁、去重、保序。

    topics 一节缺失、不是 dict、或拍扁后为空时用默认主题。
    值可以是字符串（一个查询词）或字符串列表。
    """
    section = cfg.get("topics")
    if not isinstance(section, dict):
        return default_topics()
    queries: list[str] = []
    seen: set[str] = set()
    for value in section.values():
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, list):
            candidates = [v for v in value if isinstance(v, str)]
        else:
            continue
        for query in candidates:
            query = query.strip()
            if query and query not in seen:
                seen.add(query)
                queries.append(query)
    return queries or default_topics()


def google_news_feed_urls(topics: list[str]) -> list[str]:
    """Google News feed：头条 1 个 + 每个主题 1 个搜索 feed。"""
    urls = [GOOGLE_NEWS_TOP_STORIES_URL]
    for query in topics:
        urls.append(
            f"{GOOGLE_NEWS_SEARCH_URL}?q={quote_plus(query)}&hl=en-GB&gl=GB&ceid=GB:en"
        )
    return urls


def bing_news_feed_urls(topics: list[str]) -> list[str]:
    """Bing News feed：每个主题 1 个搜索 feed。"""
    return [
        f"{BING_NEWS_SEARCH_URL}?q={quote_plus(query)}&format=rss" for query in topics
    ]


def gdelt_query(topics: list[str]) -> str:
    """把主题拼成 GDELT Doc API 的查询词（停用中，重开时用）。

    含 OR 的主题加括号，避免和外层的 OR 混在一起：
    breaking news OR (stock market OR inflation OR interest rates)
    OR technology OR (artificial intelligence OR AI) OR (war OR geopolitics)。
    """
    parts = [f"({q})" if " OR " in q else q for q in topics]
    return " OR ".join(parts)
