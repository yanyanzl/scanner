"""热点情报 Dashboard：读取 data/trends.sqlite3 并展示排名。

没有数据时明确提示先运行采集，不展示任何示例/虚假数据。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trends.sqlite3"

st.set_page_config(page_title="Global Trend Radar", layout="wide")
st.title("🌍 Global Trend Radar — 全球热点雷达")


def load_items(platform: str) -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        if platform == "全部":
            rows = conn.execute(
                "SELECT * FROM items ORDER BY score DESC, growth DESC LIMIT 100"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM items WHERE platform = ? ORDER BY score DESC, growth DESC LIMIT 100",
                (platform,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load_fastest() -> list[dict]:
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM items ORDER BY growth DESC LIMIT 20"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


platform = st.selectbox(
    "平台筛选", ["全部", "rss", "gdelt", "reddit", "youtube", "x", "tiktok"]
)
items = load_items(platform)

if not items:
    st.warning("暂无数据。请先在另一个 PowerShell 运行：python run_scheduler.py，等一轮采集完成后再刷新本页。")
    st.stop()

st.subheader("🔥 实时热点榜（按综合热度分数排序，前 100 条）")
st.dataframe(
    [
        {
            "标题": it.get("title", ""),
            "平台": it.get("platform", ""),
            "分数": it.get("score", 0),
            "增长/小时": it.get("growth", 0),
            "加速度": it.get("acceleration", 0),
            "年龄(小时)": it.get("age_hours", 0),
            "链接": it.get("url", ""),
        }
        for it in items
    ],
    use_container_width=True,
)

st.subheader("🚀 正在爆发（过去一轮增长最快的前 20 条）")
fastest = load_fastest()
st.dataframe(
    [
        {
            "标题": it.get("title", ""),
            "平台": it.get("platform", ""),
            "增长/小时": it.get("growth", 0),
            "分数": it.get("score", 0),
            "链接": it.get("url", ""),
        }
        for it in fastest
    ],
    use_container_width=True,
)

st.caption("分数权重：增长速度 30%、传播规模 20%、增长加速度 20%、跨平台 20%、新颖性 10%。不同平台口径不同，先在平台内标准化再综合。")

st.subheader("🧩 跨平台事件（P1 v0.1：标题 TF-IDF 聚类）")
try:
    from trend_radar.clustering import cluster_from_db

    events = cluster_from_db(DB_PATH, hours=24)
except Exception as exc:  # 聚类失败不影响已有榜单
    events = []
    st.info(f"事件聚类暂不可用：{exc}")
if not events:
    st.info("暂无事件：窗口内没有可聚类的内容，或数据库为空。")
else:
    st.dataframe(
        [
            {
                "事件": ev.get("name", ""),
                "平台数": ev.get("platform_count", 0),
                "平台": ", ".join(ev.get("platforms", [])),
                "条数": ev.get("item_count", 0),
                "总分": ev.get("total_score", 0),
                "最高分": ev.get("max_score", 0),
                "最大增长/小时": ev.get("max_growth", 0),
                "趋势": ev.get("trend", ""),
                "首次出现": ev.get("first_seen", ""),
            }
            for ev in events[:10]
        ],
        use_container_width=True,
    )
