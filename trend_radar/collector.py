"""采集编排：加载配置与 .env，运行已启用的 Adapter，
去重、评分、写入 SQLite（含历史快照）。"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from trend_radar import db as dbmod
from trend_radar.adapters import gdelt, reddit, rss, tiktok, x_api, youtube
from trend_radar.scoring import score_item

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent


def load_env(env_path: Path | None = None) -> None:
    """加载 .env（优先用 python-dotenv，没有则用简单解析，不覆盖已有环境变量）。"""
    path = env_path or BASE_DIR / ".env"
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path)
        return
    except ImportError:
        pass
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(config_path: Path | None = None) -> dict:
    path = config_path or BASE_DIR / "config.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _youtube_state_path(db_path: Path) -> Path:
    """YouTube 上次采集时间戳文件（与数据库放同一目录，见 .gitignore）。"""
    return Path(db_path).parent / ".youtube_last_run"


def _youtube_due(state_path: Path, interval_minutes: int, now: datetime | None = None) -> bool:
    """距上次 YouTube 采集是否已满间隔。没有记录文件（首次运行）时返回 True。"""
    now = now or datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(
            Path(state_path).read_text(encoding="utf-8").strip().replace("Z", "+00:00")
        )
    except (OSError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= int(interval_minutes) * 60


def _mark_youtube_run(state_path: Path, now: datetime | None = None) -> None:
    """记下本次 YouTube 采集时间。"""
    now = now or datetime.now(timezone.utc)
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(now.isoformat(), encoding="utf-8")


def collect_once(config: dict | None = None, config_path: Path | None = None) -> dict:
    """跑一轮采集，返回各平台采集数量。未配置凭证的平台返回 0 条并记录未启用。"""
    load_env()
    cfg = config or load_config(config_path)
    sources = cfg.get("sources", {})
    interval_minutes = int(cfg.get("scan_interval_minutes", 15))
    interval_hours = max(interval_minutes / 60.0, 0.01)
    # YouTube 单独间隔（默认 30 分钟，见 config.yaml）：搜索桶上限 100 次/天，
    # 每 15 分钟搜 2 个词一天就是 192 次，接通当天即超限。
    youtube_cfg = sources.get("youtube", {})
    youtube_interval_minutes = int(youtube_cfg.get("interval_minutes", 60))
    youtube_interval_hours = max(youtube_interval_minutes / 60.0, 0.01)
    db_path = BASE_DIR / cfg.get("db_path", "data/trends.sqlite3")

    all_items: list[dict] = []
    counts: dict[str, int] = {}

    def run(name: str, fn) -> None:
        try:
            items = fn() or []
        except Exception as exc:  # 单个平台失败不中断整轮
            logger.warning("%s 采集异常: %s", name, exc)
            items = []
        counts[name] = len(items)
        all_items.extend(items)

    if sources.get("rss", {}).get("enabled", True):
        run("rss", lambda: rss.fetch(sources["rss"].get("feeds", [])))
    if sources.get("gdelt", {}).get("enabled", True):
        run("gdelt", lambda: gdelt.fetch(sources["gdelt"].get("query", "")))
    if sources.get("reddit", {}).get("enabled", False):
        run("reddit", lambda: reddit.fetch(sources["reddit"].get("subreddits", [])))
    if youtube_cfg.get("enabled", False):
        state_path = _youtube_state_path(db_path)
        if _youtube_due(state_path, youtube_interval_minutes):
            run("youtube", lambda: youtube.fetch(youtube_cfg.get("queries", [])))
            # 只有配了 Key 才记时间：没 Key 时不消耗配额，填好 Key 后下一轮立即就能跑，不用等满 30 分钟。
            if os.environ.get("YOUTUBE_API_KEY"):
                _mark_youtube_run(state_path)
        else:
            counts["youtube"] = 0
            logger.info("YouTube 本轮跳过：未满 %d 分钟间隔", youtube_interval_minutes)
    if sources.get("x", {}).get("enabled", False):
        run("x", lambda: x_api.fetch(sources["x"].get("queries", [])))
    if sources.get("tiktok", {}).get("enabled", False):
        run("tiktok", lambda: tiktok.fetch(sources["tiktok"].get("queries", [])))

    # 跨平台计数：同一标题出现在几个平台
    title_platforms: dict[str, set] = defaultdict(set)
    for item in all_items:
        title_platforms[item.get("title", "").strip().lower()].add(item.get("platform", ""))
    # 去重：同一 URL 只保留互动量最高的一条
    deduped: dict[str, dict] = {}
    for item in all_items:
        url = item.get("url", "")
        if not url:
            continue
        prev = deduped.get(url)
        if prev is None or float(item.get("engagement") or 0) > float(prev.get("engagement") or 0):
            deduped[url] = item

    conn = dbmod.connect(db_path)
    saved = 0
    try:
        for item in deduped.values():
            row = conn.execute(
                "SELECT id FROM items WHERE url = ?", (item.get("url", ""),)
            ).fetchone()
            previous = 0.0
            if row:
                previous = dbmod.previous_engagement(conn, int(row["id"]))
            cross = len(title_platforms.get(item.get("title", "").strip().lower(), set()))
            # YouTube 快照是每 30 分钟一次，用 0.5 小时算增长才对；若仍用全局 0.25 小时，
            # 增长速度会被高估 2 倍。其他平台沿用全局扫描间隔。
            item_hours = (
                youtube_interval_hours if item.get("platform") == "youtube" else interval_hours
            )
            result = score_item(
                item,
                previous_engagement=previous,
                interval_hours=item_hours,
                cross_platform_count=max(1, cross),
            )
            item_id = dbmod.upsert_item(conn, item, result)
            dbmod.record_snapshot(conn, item_id, float(item.get("engagement") or 0), result["score"])
            saved += 1

        # P2 准备：每轮采集后聚类并持久化事件快照（失败不影响本轮采集）。
        # 只有接在这里，events / event_snapshots 才会随扫描自动积累，
        # 事件才有跨轮次的稳定 ID 与可比的历史，爆发检测才有上一轮可比。
        try:
            from trend_radar.burst import detect_bursts
            from trend_radar.clustering import cluster_from_db, persist_events

            events = cluster_from_db(db_path)
            persist_events(conn, events)
            bursts = detect_bursts(conn, thresholds=cfg.get("burst"))
            counts["events"] = len(events)
            counts["bursts"] = len(bursts)
            if bursts:
                logger.info(
                    "爆发事件 %d 个: %s",
                    len(bursts),
                    "; ".join(f"{b['name']}({','.join(b['reasons'])})" for b in bursts),
                )
        except Exception as exc:  # 聚类/持久化失败不影响采集结果
            logger.warning("事件聚类/持久化失败（不影响采集）: %s", exc)
            counts["events"] = 0
            counts["bursts"] = 0
    finally:
        conn.close()

    counts["saved"] = saved
    logger.info("本轮采集完成: %s", counts)
    return counts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(collect_once())
