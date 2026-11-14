"""热度评分：增长速度优先，而不是只看总量。

权重（config.yaml 可调，初始假设，需用真实数据校准）：
- 增长速度 growth        30%
- 传播规模 scale          20%
- 增长加速度 acceleration 20%
- 跨平台传播 cross-platform 20%
- 内容新颖性 novelty      10%

不同平台的互动口径不同，先在平台内部标准化，再跨平台综合。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

WEIGHTS = {
    "growth": 0.30,
    "scale": 0.20,
    "acceleration": 0.20,
    "cross_platform": 0.20,
    "novelty": 0.10,
}


def parse_dt(value: Any) -> Optional[datetime]:
    """解析 ISO / RFC822 / 时间戳，失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _log_scale(value: float, cap: float = 1_000_000.0) -> float:
    """把总量压到 0-1 之间，避免大号垄断排名。"""
    if value <= 0:
        return 0.0
    return min(1.0, math.log10(1 + value) / math.log10(1 + cap))


def score_item(
    item: dict,
    previous_engagement: float = 0.0,
    previous_growth: float = 0.0,
    interval_hours: float = 0.25,
    cross_platform_count: int = 1,
    is_new_topic: bool = False,
) -> dict:
    """给一条内容打分。

    item 至少需要 engagement（总互动）字段；published_at / first_seen
    用于计算内容年龄。返回包含 score 和各分项的 dict。
    """
    engagement = float(item.get("engagement") or 0)
    # 下面两行是之前出过换行语法错误的位置：必须是两行独立语句。
    published = parse_dt(item.get("published_at")) or parse_dt(item.get("first_seen"))
    age_hours = max(0, (datetime.now(timezone.utc) - published).total_seconds() / 3600) if published else 48

    # 增长速度：单位时间的互动增量
    hours = max(interval_hours, 0.01)
    growth = max(0.0, engagement - float(previous_engagement or 0)) / hours
    growth_score = min(1.0, growth / 10000.0)

    # 增长加速度：增长是否还在加快
    acceleration = (growth - float(previous_growth or 0)) / hours
    accel_score = 0.5 + min(0.5, max(-0.5, acceleration / 20000.0))

    # 传播规模：总量对数标准化
    scale_score = _log_scale(engagement)

    # 跨平台：出现在 1 个平台 0.25，2 个 0.6，3 个及以上 1.0
    cross_score = min(1.0, 0.25 + 0.35 * max(0, int(cross_platform_count or 1) - 1))

    # 新颖性：新内容加分，但随年龄衰减
    novelty_score = 1.0 if is_new_topic else max(0.0, 1.0 - age_hours / 72.0)

    score = (
        WEIGHTS["growth"] * growth_score
        + WEIGHTS["scale"] * scale_score
        + WEIGHTS["acceleration"] * accel_score
        + WEIGHTS["cross_platform"] * cross_score
        + WEIGHTS["novelty"] * novelty_score
    )

    return {
        "score": round(score, 4),
        "growth": round(growth, 2),
        "acceleration": round(acceleration, 2),
        "age_hours": round(age_hours, 2),
        "growth_score": round(growth_score, 4),
        "scale_score": round(scale_score, 4),
        "acceleration_score": round(accel_score, 4),
        "cross_platform_score": round(cross_score, 4),
        "novelty_score": round(novelty_score, 4),
    }
