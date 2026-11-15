"""热点情报 Dashboard：读取 data/trends.sqlite3 并展示排名。

没有数据时明确提示先运行采集，不展示任何示例/虚假数据。
数据加载逻辑在 trend_radar/dashboard_data.py（不依赖 Streamlit，可单测）。
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from trend_radar.dashboard_data import load_bursts, load_fastest, load_history, load_items

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "trends.sqlite3"

st.set_page_config(page_title="Global Trend Radar", layout="wide")
st.title("🌍 Global Trend Radar — 全球热点雷达")


platform = st.selectbox(
    "平台筛选", ["全部", "rss", "gdelt", "reddit", "youtube", "x", "tiktok"]
)
items = load_items(DB_PATH, platform)

if not items:
    st.warning(
        "暂无数据。请先运行采集：Windows 双击「启动扫描.bat」，macOS 在终端运行 ./启动扫描.sh，"
        "等一轮采集完成后再刷新本页。"
    )
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
    width="stretch",
)

st.subheader("🚀 正在爆发（过去一轮增长最快的前 20 条）")
fastest = load_fastest(DB_PATH)
if not fastest or max(float(it.get("growth") or 0) for it in fastest) <= 0:
    st.info(
        "暂无增长数据：当前已启用的来源（RSS / GDELT）不带互动量，增长速度都为 0。"
        "接通 Reddit / YouTube 并跑几轮后，这里会显示增长最快的条目。"
    )
else:
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
        width="stretch",
    )

st.caption("分数权重：增长速度 30%、传播规模 20%、增长加速度 20%、跨平台 20%、新颖性 10%。不同平台口径不同，先在平台内标准化再综合。")

st.subheader("📈 互动量随时间变化（快照历史）")
times, series = load_history(DB_PATH)
if not times or not series:
    st.info(
        "暂无互动量历史：快照里还没有带互动量的记录（RSS / GDELT 不带互动量）。"
        "接通 Reddit / YouTube 并跑几轮后，这里会画出互动量随时间变化的曲线。"
    )
else:
    import pandas as pd

    chart_df = pd.DataFrame(series, index=[t[:19] for t in times])
    chart_df.index.name = "时间"
    st.line_chart(chart_df)
    st.caption(
        f"曲线来自 snapshots 表的真实快照（{len(times)} 个时间点，"
        f"最多 {len(series)} 条内容）。缺失的时间点表示该轮没有该内容的快照。"
    )

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
        width="stretch",
    )

st.subheader("💥 正在爆发的事件（P2 准备：与上一轮事件快照对比）")
try:
    bursts = load_bursts(DB_PATH)
except Exception:  # 爆发检测失败不影响已有榜单
    bursts = []
if not bursts:
    st.info(
        "暂无爆发事件：需要至少两轮事件快照才能比较增长（新事件首次出现"
        "就跨多个平台时也会报）。每轮采集后事件快照自动积累。"
    )
else:
    st.dataframe(
        [
            {
                "事件": b.get("name", ""),
                "爆发原因": ", ".join(b.get("reasons", [])),
                "增长/小时": f"{b['previous']['max_growth'] if b.get('previous') else '—'} → {b['current']['max_growth']}",
                "总分": f"{b['previous']['total_score'] if b.get('previous') else '—'} → {b['current']['total_score']}",
                "平台数": b["current"]["platform_count"],
                "事件 ID": b.get("event_id", ""),
            }
            for b in bursts
        ],
        width="stretch",
    )
