"""事件综合排名 Top N：把一轮采集到的事件聚合成一个榜。

解决的问题：Dashboard 原来有 5 节，但没有一个地方能直接回答
“现在最值得看的事件是哪些”。热点榜是按条目排的，跨平台事件一节
是按 (平台数, 总分, 最高分) 的临时排序——这里把它换成一个说得清
的综合分。

公式（每个分量先归一化到 0-1，再按权重加权求和，×100 得 0-100 分）：
  综合分 = 0.30 × 增长 + 0.20 × 成员强度 + 0.20 × 爆发 + 0.20 × 跨平台 + 0.10 × 新鲜度

各分量：
  增长：事件的 max_growth（最大增长/小时），在本批事件里 min-max
    归一化。增长优先是本项目的第一性原理：趋势是变化，不是总量。
  成员强度：0.7 × max_score + 0.3 × log1p(total_score)，再 min-max
    归一化。max_score 是主信号；total_score 用 log 压一下再加，
    这样条数很多的大事件有加分，但不会靠堆条数垄断榜单。
  爆发：事件出现在本轮 burst 列表里为 1，否则为 0（固定加分，不
    是归一化——爆发本身就是加速度信号，见 docs/P2_PREP.md）。
  跨平台：platform_count，在本批事件里 min-max 归一化。同一件事
    在多个平台出现，说明它在扩散。
  新鲜度：半衰期衰减 0.5 ** (age_hours / half_life)，默认半衰期 24
    小时；不做 min-max——新鲜度有绝对含义（“多久以前首次出现”），
    不是相对量。first_seen 缺失或解析失败时按“很老”算（0 分）。

归一化说明：min-max 是在本批事件内部算的，所以排名是相对的，不
是绝对的——同一事件在不同轮的综合分不能直接比较。某个分量在全批
里都一样（max == min）时，该分量对所有事件都记 0.5 分，不会除零。

权重与半衰期全部是初始假设，需用 1-2 周真实数据校准（和条目评分
权重、聚类阈值、爆发阈值一样，见 docs/P2_PREP.md、docs/TOP_EVENTS.md）。
不要把它们当成调好的参数：最先值得重新看的是 growth 权重（RSS /
GDELT 没有互动量时 max_growth 全是 0，该分量退化）和半衰期
（24 小时对新闻来说可能太长或太短）。
权重可通过 rank_events 的 weights 参数、或 config.yaml 的 ranking
一节覆盖；半衰期可通过 freshness_half_life_hours 参数、或 weights
里的同名键、或 config 覆盖。

纯函数：不读数据库、不联网、不改输入（返回的是输入 dict 的浅拷贝
加 rank_score / rank_reasons）；同一输入永远得到同一顺序（同分时
按 event_id 排）。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trend_radar.scoring import parse_dt

BASE_DIR = Path(__file__).resolve().parent.parent

# 默认权重：与条目评分的思路一致（增长 30%、其余 20/20/20/10），
# 只是“加速度”在事件层由“爆发”代替。全部是初始假设，未校准。
DEFAULT_WEIGHTS = {
    "growth": 0.30,
    "member_strength": 0.20,
    "burst": 0.20,
    "cross_platform": 0.20,
    "novelty": 0.10,
}
DEFAULT_HALF_LIFE_HOURS = 24.0
DEFAULT_TOP_N = 10

# 爆发规则名 -> 展示用中文（与 trend_radar/alerts.py 保持一致）。
BURST_REASON_WORDS = {
    "growth_spike": "增速突增",
    "score_surge": "总分跃升",
    "new_multi_platform": "新事件首次跨平台",
}

# 新鲜度原因只对最近出现的事件写（超过这个小时数就不写了，
# 否则每个事件的原因里都挂着“xx 小时前首次出现”，没有信息量）。
FRESHNESS_REASON_MAX_HOURS = 12.0

_WEIGHT_KEYS = ("growth", "member_strength", "burst", "cross_platform", "novelty")


def load_ranking_config() -> dict:
    """读 config.yaml 的 ranking 一节；读不到就用默认（不依赖 yaml 是否安装）。

    返回 {"top_n", "weights", "freshness_half_life_hours"}。
    """
    cfg = {
        "top_n": DEFAULT_TOP_N,
        "weights": dict(DEFAULT_WEIGHTS),
        "freshness_half_life_hours": DEFAULT_HALF_LIFE_HOURS,
    }
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        section = raw.get("ranking") or {}
        if isinstance(section, dict):
            if section.get("top_n") is not None:
                cfg["top_n"] = max(1, int(section["top_n"]))
            weights = section.get("weights")
            if isinstance(weights, dict):
                for key in _WEIGHT_KEYS:
                    if weights.get(key) is not None:
                        cfg["weights"][key] = float(weights[key])
            if section.get("freshness_half_life_hours") is not None:
                cfg["freshness_half_life_hours"] = float(section["freshness_half_life_hours"])
    except Exception:
        pass
    return cfg


def _resolve_weights(weights: dict | None) -> dict[str, float]:
    """合并默认权重与调用方覆盖；权重按总和归一化（这样综合分恒在 0-100）。"""
    merged = dict(DEFAULT_WEIGHTS)
    if isinstance(weights, dict):
        for key in _WEIGHT_KEYS:
            if weights.get(key) is not None:
                merged[key] = float(weights[key])
    total = sum(max(0.0, v) for v in merged.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {key: max(0.0, value) / total for key, value in merged.items()}


def _resolve_half_life(
    freshness_half_life_hours: float | None, weights: dict | None
) -> float:
    """半衰期：显式参数优先，其次 weights 里的同名键，最后默认。"""
    if freshness_half_life_hours is not None:
        return max(1.0, float(freshness_half_life_hours))
    if isinstance(weights, dict) and weights.get("freshness_half_life_hours") is not None:
        return max(1.0, float(weights["freshness_half_life_hours"]))
    return DEFAULT_HALF_LIFE_HOURS


def _minmax(values: list[float]) -> list[float]:
    """一批数 min-max 归一化到 0-1；全相等时都记 0.5（不除零）。"""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _age_hours(event: dict, now: datetime) -> float | None:
    """事件首次出现到 now 的小时数；first_seen 缺失或解析失败返回 None。"""
    first_seen = parse_dt(event.get("first_seen"))
    if first_seen is None:
        return None
    return max(0.0, (now - first_seen).total_seconds() / 3600)


def _rank_reasons(
    event: dict,
    burst_reasons: list[str],
    age_hours: float | None,
    is_fastest_growth: bool,
) -> list[str]:
    """给一个事件生成展示用的原因（中文短语，只写有信息量的）。

    顺序：爆发原因 → 平台数 → 增长最快 → 新鲜度；什么都没命中时
    兜底写最高分，保证原因列表永远非空。
    """
    reasons: list[str] = []
    for reason in burst_reasons:
        word = BURST_REASON_WORDS.get(reason)
        if word:
            reasons.append(word)
    platform_count = int(event.get("platform_count") or 0)
    if platform_count >= 2:
        reasons.append(f"{platform_count} 个平台")
    if is_fastest_growth:
        reasons.append("增长最快")
    if age_hours is not None and age_hours < FRESHNESS_REASON_MAX_HOURS:
        if age_hours < 1:
            reasons.append("1 小时内首次出现")
        else:
            reasons.append(f"{int(round(age_hours))} 小时前首次出现")
    if not reasons:
        max_score = float(event.get("max_score") or 0)
        reasons.append(f"最高分 {max_score:.2f}")
    return reasons


def rank_events(
    events: list[dict],
    bursts: list[dict] | None = None,
    now: datetime | str | None = None,
    weights: dict | None = None,
    freshness_half_life_hours: float | None = None,
) -> list[dict]:
    """把事件按综合分从高到低排好。

    每个返回项是输入 dict 的浅拷贝，加 rank_score（0-100，1 位小数）
    和 rank_reasons（中文短语列表）。不改动输入；空输入返回 []；
    同分按 event_id 排，同一输入永远得到同一顺序。
    """
    if not events:
        return []
    if now is None:
        now_dt = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        now_dt = now
    else:
        now_dt = parse_dt(now) or datetime.now(timezone.utc)

    resolved_weights = _resolve_weights(weights)
    half_life = _resolve_half_life(freshness_half_life_hours, weights)

    burst_by_id: dict[str, list[str]] = {}
    for burst in bursts or []:
        event_id = burst.get("event_id")
        if event_id:
            burst_by_id[event_id] = list(burst.get("reasons") or [])

    growth_raw = [max(0.0, float(ev.get("max_growth") or 0)) for ev in events]
    member_raw = [
        0.7 * max(0.0, float(ev.get("max_score") or 0))
        + 0.3 * math.log1p(max(0.0, float(ev.get("total_score") or 0)))
        for ev in events
    ]
    platform_raw = [float(ev.get("platform_count") or 0) for ev in events]
    growth_norm = _minmax(growth_raw)
    member_norm = _minmax(member_raw)
    platform_norm = _minmax(platform_raw)

    ages = [_age_hours(ev, now_dt) for ev in events]
    max_growth = max(growth_raw) if growth_raw else 0.0

    ranked: list[dict] = []
    for idx, ev in enumerate(events):
        event_id = ev.get("event_id") or ""
        burst_reasons = burst_by_id.get(event_id, [])
        age_hours = ages[idx]
        if age_hours is None:
            novelty = 0.0  # first_seen 缺失：按“很老”算，不 crash。
        else:
            novelty = 0.5 ** (age_hours / half_life)
        score = (
            resolved_weights["growth"] * growth_norm[idx]
            + resolved_weights["member_strength"] * member_norm[idx]
            + resolved_weights["burst"] * (1.0 if burst_reasons else 0.0)
            + resolved_weights["cross_platform"] * platform_norm[idx]
            + resolved_weights["novelty"] * novelty
        )
        out = dict(ev)
        out["rank_score"] = round(score * 100, 1)
        out["rank_reasons"] = _rank_reasons(
            ev,
            burst_reasons,
            age_hours,
            is_fastest_growth=(growth_raw[idx] > 0 and growth_raw[idx] >= max_growth),
        )
        ranked.append(out)
    ranked.sort(key=lambda e: (-e["rank_score"], e.get("event_id") or ""))
    return ranked
