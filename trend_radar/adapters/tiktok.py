"""TikTok 采集：预留官方授权数据接口适配器（stub）。

TikTok 官方 API 存在资格和产品用途限制，不能假设个人项目
可以使用，也不能通过绕过访问限制的爬虫获取全平台实时热门视频。
本模块目前不采集任何数据：始终返回 []，并记录“未启用”。
接通官方授权数据接口后，在此实现 fetch()，保持返回
归一化 Item dict 列表的约定不变。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def fetch(queries: list[str] | None = None, timeout: int = 15) -> list[dict]:
    """未启用：TikTok 需要官方授权数据接口，当前未配置。"""
    logger.info("TikTok 未启用：需要官方授权数据接口，未配置；不使用绕过访问限制的爬虫")
    return []
