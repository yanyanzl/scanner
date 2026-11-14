"""统一数据模型：所有平台先归一化成 Item，再入库、评分。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Item:
    platform: str  # rss / gdelt / reddit / youtube / x / tiktok
    title: str
    url: str
    published_at: Optional[str] = None
    first_seen: Optional[str] = None
    engagement: float = 0.0
    views: float = 0.0
    likes: float = 0.0
    shares: float = 0.0
    comments: float = 0.0
    source: str = ""
    language: str = ""
    summary_zh: str = ""

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "first_seen": self.first_seen,
            "engagement": self.engagement,
            "views": self.views,
            "likes": self.likes,
            "shares": self.shares,
            "comments": self.comments,
            "source": self.source,
            "language": self.language,
            "summary_zh": self.summary_zh,
        }

    @staticmethod
    def engagement_from_parts(views=0, likes=0, shares=0, comments=0) -> float:
        return float(views or 0) * 0.1 + float(likes or 0) + float(shares or 0) * 2 + float(comments or 0)
