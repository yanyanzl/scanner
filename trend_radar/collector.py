"""采集编排：加载配置与 .env，运行已启用的 Adapter，
去重、评分、写入 SQLite（含历史快照）。"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
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


def collect_once(config: dict | None = None, config_path: Path | None = None) -> dict:
    """跑一轮采集，返回各平台采集数量。未配置凭证的平台返回 0 条并记录未启用。"""
    load_env()
    cfg = config or load_config(config_path)
    sources = cfg.get("sources", {})
    interval_minutes = int(cfg.get("scan_interval_minutes", 15))
    interval_hours = max(interval_minutes / 60.0, 0.01)
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
    if sources.get("youtube", {}).get("enabled", False):
        run("youtube", lambda: youtube.fetch(sources["youtube"].get("queries", [])))
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
            result = score_item(
                item,
                previous_engagement=previous,
                interval_hours=interval_hours,
                cross_platform_count=max(1, cross),
            )
            item_id = dbmod.upsert_item(conn, item, result)
            dbmod.record_snapshot(conn, item_id, float(item.get("engagement") or 0), result["score"])
            saved += 1
    finally:
        conn.close()

    counts["saved"] = saved
    logger.info("本轮采集完成: %s", counts)
    return counts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(collect_once())
