"""Dashboard 表格行构造（显示层）：把加载器返回的原始 dict 拼成
app.py 表格里的一行，时间字段在这里用 timefmt 格式化成本机当地时间。

加载器（dashboard_data）只返回原始 ISO 时间串，不格式化；提醒正文
（alerts）和简报（quality_digest）也调同一个 timefmt helper——
全项目只有 trend_radar/timefmt.py 一个时间格式化入口。

行构造器是纯函数（mock dict 进、显示 dict 出），可单测；app.py
只负责把行列表交给 Streamlit。
"""
from __future__ import annotations

from trend_radar.timefmt import format_local_time


def top_event_row(ev: dict, index: int, tz=None) -> dict:
    """最热事件 Top 10 的一行：新增“首次出现”列。"""
    return {
        "排名": index,
        "事件": ev.get("name", ""),
        "首次出现": format_local_time(ev.get("first_seen"), tz=tz),
        "综合分": round(float(ev.get("rank_score") or 0), 1),
        "平台数": ev.get("platform_count", 0),
        "平台": ", ".join(ev.get("platforms", [])),
        "最大增长/小时": ev.get("max_growth", 0),
        "原因": " · ".join(ev.get("rank_reasons") or []),
        "链接": ev.get("link_url") or None,
    }


def _published_time(it: dict, tz=None) -> str:
    """发布时间：优先 published_at，没有则用 first_seen（首次采集）。"""
    return format_local_time(it.get("published_at") or it.get("first_seen"), tz=tz)


def hot_item_row(it: dict, tz=None) -> dict:
    """实时热点榜的一行：新增“发布时间”列，原有的“年龄(小时)”保留。"""
    return {
        "标题": it.get("title", ""),
        "平台": it.get("platform", ""),
        "发布时间": _published_time(it, tz),
        "分数": it.get("score", 0),
        "增长/小时": it.get("growth", 0),
        "加速度": it.get("acceleration", 0),
        "年龄(小时)": it.get("age_hours", 0),
        "链接": it.get("url", ""),
    }


def fastest_item_row(it: dict, tz=None) -> dict:
    """正在爆发（最快增长）的一行：新增“发布时间”列，同上规则。"""
    return {
        "标题": it.get("title", ""),
        "平台": it.get("platform", ""),
        "发布时间": _published_time(it, tz),
        "增长/小时": it.get("growth", 0),
        "分数": it.get("score", 0),
        "链接": it.get("url", ""),
    }


def cross_platform_row(ev: dict, tz=None) -> dict:
    """跨平台事件的一行：“首次出现”列改用 timefmt 格式化（原来是
    没格式化的原始时间串）。"""
    return {
        "事件": ev.get("name_zh") or ev.get("name", ""),
        # rank_score 缺失或为 None 时显示 "—"（排名失败回退到原始聚类
        # 时事件没有 rank_score 字段）；0.0 分是真值，照常显示。
        "综合分": ev.get("rank_score") if ev.get("rank_score") is not None else "—",
        "平台数": ev.get("platform_count", 0),
        "平台": ", ".join(ev.get("platforms", [])),
        "条数": ev.get("item_count", 0),
        "总分": ev.get("total_score", 0),
        "最高分": ev.get("max_score", 0),
        "最大增长/小时": ev.get("max_growth", 0),
        "趋势": ev.get("trend", ""),
        "首次出现": format_local_time(ev.get("first_seen"), tz=tz),
    }


def burst_row(b: dict, tz=None) -> dict:
    """正在爆发的事件的一行：新增“检测时间”列（burst 的 detected_at）。"""
    return {
        "事件": b.get("name", ""),
        "爆发原因": ", ".join(b.get("reasons", [])),
        "检测时间": format_local_time(b.get("detected_at"), tz=tz),
        "增长/小时": f"{b['previous']['max_growth'] if b.get('previous') else '—'} → {b['current']['max_growth']}",
        "总分": f"{b['previous']['total_score'] if b.get('previous') else '—'} → {b['current']['total_score']}",
        "平台数": b["current"]["platform_count"],
        "事件 ID": b.get("event_id", ""),
    }
