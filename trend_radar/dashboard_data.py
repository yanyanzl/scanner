"""Dashboard 数据加载：只读 data/trends.sqlite3，不依赖 Streamlit。

app.py 从这里 import；这样加载逻辑可以用单元测试直接测，
而不用 import 会执行 Streamlit 调用的 app.py。

所有函数对缺失的数据库文件都返回空结果，不抛异常、不造假数据。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_LIMIT = 100
FASTEST_LIMIT = 20
HISTORY_LIMIT = 5


def _connect(db_path: str | Path) -> sqlite3.Connection | None:
    path = Path(db_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def load_items(db_path: str | Path, platform: str = "全部", limit: int = DEFAULT_LIMIT) -> list[dict]:
    """按分数取 items；platform != "全部" 时只取该平台。缺 DB 返回 []。"""
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        if platform == "全部":
            rows = conn.execute(
                "SELECT * FROM items ORDER BY score DESC, growth DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM items WHERE platform = ? ORDER BY score DESC, growth DESC LIMIT ?",
                (platform, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def load_fastest(db_path: str | Path, limit: int = FASTEST_LIMIT) -> list[dict]:
    """按增长速度取前 N 条。缺 DB 返回 []。"""
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM items ORDER BY growth DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def load_history(
    db_path: str | Path, limit: int = HISTORY_LIMIT
) -> tuple[list[str], dict[str, list[float | None]]]:
    """取互动量随时间变化的历史（snapshots 表的真实快照）。

    选互动量曾经大于 0、且按增长速度排在前面的最多 `limit` 条内容，
    返回 (时间点列表, {标题: [各时间点的互动量, 缺失为 None]})。
    时间点按快照时间升序排列。

    没有数据库、没有快照、或所有快照互动量都为 0（例如只有 RSS /
    GDELT 数据时）返回 ([], {})——调用方应显示“暂无互动量历史”，
    而不是画一条全是 0 的假曲线。
    """
    conn = _connect(db_path)
    if conn is None:
        return [], {}
    try:
        rows = conn.execute(
            """
            SELECT s.collected_at AS t, i.title AS title, s.engagement AS engagement
            FROM snapshots s JOIN items i ON i.id = s.item_id
            ORDER BY s.collected_at ASC
            """
        ).fetchall()
    except sqlite3.Error:
        return [], {}
    finally:
        conn.close()
    if not rows:
        return [], {}

    # 每个标题只保留一个序列；标题过长时截断，避免图例过长。
    def short(title: str) -> str:
        title = (title or "").strip()
        return title if len(title) <= 40 else title[:37] + "…"

    by_title: dict[str, dict[str, float]] = {}
    for r in rows:
        t = str(r["t"])
        by_title.setdefault(short(str(r["title"])), {})[t] = float(r["engagement"] or 0)
    # 只要互动量曾经大于 0 的标题（全是 0 的是 RSS/GDELT，不画曲线）。
    by_title = {t: p for t, p in by_title.items() if max(p.values()) > 0}
    if not by_title:
        return [], {}

    # 按该标题的最大互动量排序（增长最快的在前），只取前 limit 个。
    ranked = sorted(
        by_title.items(),
        key=lambda kv: max(kv[1].values()),
        reverse=True,
    )[: max(1, int(limit))]
    times = sorted({t for _, points in ranked for t in points})
    series = {title: [points.get(t) for t in times] for title, points in ranked}
    return times, series


def load_bursts(db_path: str | Path) -> list[dict]:
    """取当前爆发的事件（P2 准备，见 trend_radar/burst.py）。

    缺 DB、或 events 表还不存在（老数据库第一次连接后自动建表，
    但一轮采集都没跑过时表里没数据）时返回 []。
    """
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        from trend_radar.burst import detect_bursts, load_burst_config

        return detect_bursts(conn, thresholds=load_burst_config())
    except sqlite3.Error:
        return []
    finally:
        conn.close()
