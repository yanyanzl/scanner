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


def _display_row(row: dict) -> dict:
    """把一行里的标题换成中文标题（有 title_zh 时），原标题保留在
    title_original 里；没有翻译时原样返回。

    app.py 直接读 row["title"] 显示，所以在这里替换，app.py 不用改。
    聚类键不受影响：聚类直接读数据库的原始 title，不走这里。
    """
    title_zh = (row.get("title_zh") or "").strip()
    if title_zh and title_zh != row.get("title"):
        row = dict(row)
        row["title_original"] = row.get("title", "")
        row["title"] = title_zh
    return row


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
        return [_display_row(dict(r)) for r in rows]
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
        return [_display_row(dict(r)) for r in rows]
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
            SELECT s.collected_at AS t, i.title AS title, i.title_zh AS title_zh,
                   s.engagement AS engagement
            FROM snapshots s JOIN items i ON i.id = s.item_id
            ORDER BY s.collected_at ASC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        # 老数据库还没有 title_zh 列（迁移只在 collector 打开数据库时跑）：
        # 退回不带中文标题的旧查询，图表照常画，只是图例用原标题。
        if "no such column" not in str(exc):
            return [], {}
        try:
            rows = conn.execute(
                """
                SELECT s.collected_at AS t, i.title AS title,
                       s.engagement AS engagement
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
        return _history_series(rows, use_zh=False, limit=limit)
    finally:
        conn.close()
    if not rows:
        return [], {}
    return _history_series(rows, use_zh=True, limit=limit)


def _history_series(
    rows, use_zh: bool, limit: int
) -> tuple[list[str], dict[str, list[float | None]]]:
    """把快照行拼成 (时间点, {标题: 各时间点互动量})；use_zh 为真时图例用中文标题。"""

    # 每个标题只保留一个序列；标题过长时截断，避免图例过长。
    # 图例用中文标题（有 title_zh 时），没有时用原标题。
    def short(title: str) -> str:
        title = (title or "").strip()
        return title if len(title) <= 40 else title[:37] + "…"

    by_title: dict[str, dict[str, float]] = {}
    for r in rows:
        t = str(r["t"])
        zh = (r["title_zh"] or "").strip() if use_zh else ""
        display = zh or str(r["title"])
        by_title.setdefault(short(display), {})[t] = float(r["engagement"] or 0)
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


_LINK_URL_IN_CHUNK = 500


def _earliest_collected_url(conn, urls) -> str:
    """在 items 表里找给定 URL 中 collected_at 最早的一个；找不到返回 ""。

    URL 分批查（SQLite 变量数上限），每批取最早的一条再跨批比较。
    collected_at 是 ISO 时间文本，字符串比较即时间比较。
    """
    best_url = ""
    best_time: str | None = None
    url_list = sorted(urls)
    for i in range(0, len(url_list), _LINK_URL_IN_CHUNK):
        part = url_list[i : i + _LINK_URL_IN_CHUNK]
        placeholders = ",".join("?" * len(part))
        row = conn.execute(
            f"SELECT url, collected_at FROM items WHERE url IN ({placeholders}) "
            "AND url != '' ORDER BY collected_at ASC LIMIT 1",
            part,
        ).fetchone()
        if row and row[0]:
            t = str(row[1] or "")
            if best_time is None or t < best_time:
                best_time = t
                best_url = str(row[0])
    return best_url


def _event_link_url(conn, ev: dict) -> str:
    """事件的代表链接：分数最高成员的 URL；该成员没有 URL 时，取事件里
    最早采集到的成员 URL；都没有时返回 ""（调用方显示纯文本，不给死链接）。

    members 在 cluster_items 里已按分数降序排好；最早采集需要查
    items 表的 collected_at（成员 dict 里没有这个字段），所以回退
    需要一个数据库连接；conn 为 None 时退回按分数顺序取第一个有
    URL 的成员。任何一步失败都返回 ""，不抛异常。
    """
    try:
        members = ev.get("items") or []
        if members:
            top = max(members, key=lambda it: float(it.get("score") or 0))
            url = (top.get("url") or "").strip()
            if url:
                return url
        urls = {(it.get("url") or "").strip() for it in members}
        urls.discard("")
        if conn is not None and urls:
            try:
                best = _earliest_collected_url(conn, urls)
                if best:
                    return best
            except sqlite3.Error:
                pass
        for it in members:
            url = (it.get("url") or "").strip()
            if url:
                return url
    except Exception:
        pass
    return ""


def load_top_events(db_path: str | Path, limit: int | None = None) -> list[dict]:
    """取综合排名最靠前的事件（Top N，见 trend_radar/event_ranking.py）。

    读最近 24 小时的聚类事件 + 本轮爆发列表，按综合分排序。
    事件名优先显示中文名（app.py 直接读 ev["name"]，这里替换就不用改
    app.py）；原名保留在 name_original 里。

    缺 DB、没事件、或内部任何一步失败时返回 []，不抛异常。
    limit 为 None 时用 config.yaml 的 ranking.top_n（默认 10）。
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        from trend_radar.clustering import cluster_from_db
        from trend_radar.event_ranking import load_ranking_config, rank_events

        cfg = load_ranking_config()
        top_n = int(limit) if limit is not None else int(cfg.get("top_n", 10))
        events = cluster_from_db(db_path, hours=24)
        if not events:
            return []
        bursts = load_bursts(db_path)
        ranked = rank_events(
            events,
            bursts=bursts,
            weights=cfg.get("weights"),
            freshness_half_life_hours=cfg.get("freshness_half_life_hours"),
        )
        for ev in ranked:
            name_zh = (ev.get("name_zh") or "").strip()
            if name_zh and name_zh != ev.get("name"):
                ev["name_original"] = ev.get("name", "")
                ev["name"] = name_zh
        # 代表链接：分数最高成员的 URL（没有时取最早采集的成员 URL，
        # 都没有时为 ""）。链接计算失败不影响排名，只记空链接。
        conn = _connect(db_path)
        try:
            for ev in ranked:
                ev["link_url"] = _event_link_url(conn, ev)
        finally:
            if conn is not None:
                conn.close()
        return ranked[: max(1, top_n)]
    except Exception as exc:  # 排名失败不影响 Dashboard 其余各节
        logger.warning("加载最热事件失败：%s", exc)
        return []


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

        bursts = detect_bursts(conn, thresholds=load_burst_config())
        # 事件名优先显示中文名（app.py 直接读 b["name"]，这里替换就不用改 app.py）。
        for b in bursts:
            name_zh = (b.get("name_zh") or "").strip()
            if name_zh and name_zh != b.get("name"):
                b["name_original"] = b.get("name", "")
                b["name"] = name_zh
        return bursts
    except sqlite3.Error:
        return []
    finally:
        conn.close()
