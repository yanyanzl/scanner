"""采集编排：加载配置与 .env，运行已启用的 Adapter，
去重、评分、写入 SQLite（含历史快照）。"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from trend_radar import db as dbmod
from trend_radar import translate as translate_mod
from trend_radar.adapters import gdelt, reddit, rss, tiktok, x_api, youtube
from trend_radar.scoring import parse_dt, score_item

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


# 事件名每轮最多调几次翻译接口（事件少，10 次足够）。
EVENT_TRANSLATE_PER_ROUND = 10


def _translation_config(cfg: dict) -> dict:
    """读 config.yaml 的 translation 一节。

    注意：配置里完全没有 translation 一节时默认关闭——这样旧的
    内联配置（测试里大量使用）不会突然开始调翻译接口；正式的
    config.yaml 里有该节，enabled 默认为 true。
    """
    section = cfg.get("translation")
    if not isinstance(section, dict):
        return {
            "enabled": False,
            "target_lang": "zh-CN",
            "max_per_round": 30,
            "delay_seconds": 0.5,
        }
    return {
        "enabled": bool(section.get("enabled", True)),
        "target_lang": str(section.get("target_lang", "zh-CN")),
        "max_per_round": int(section.get("max_per_round", 30)),
        "delay_seconds": float(section.get("delay_seconds", 0.5)),
    }


def _translate_pending_items(conn, tr_cfg: dict) -> int:
    """翻译还没有中文标题的内容，返回本轮翻译接口调用次数。

    每轮上限 max_per_round 次接口调用；中文标题直接记 title_zh=原标题，
    不调接口、不占预算；相同标题只翻一次。只翻最近的记录（按
    collected_at 倒序），超预算的留到下一轮。单个标题失败只记 warning，
    不影响其他标题；本函数本身不抛异常。
    """
    if not tr_cfg.get("enabled", False):
        return 0
    max_calls = max(0, int(tr_cfg.get("max_per_round", 30)))
    target_lang = str(tr_cfg.get("target_lang", "zh-CN"))
    delay = max(0.0, float(tr_cfg.get("delay_seconds", 0.5)))
    rows = conn.execute(
        "SELECT id, title FROM items "
        "WHERE title_zh IS NULL OR title_zh = '' "
        "ORDER BY collected_at DESC LIMIT 500"
    ).fetchall()
    titles: list[str] = []
    seen: set[str] = set()
    for row in rows:
        title = (row["title"] or "").strip()
        if title and title not in seen:
            seen.add(title)
            titles.append(title)
    api_calls = 0
    for title in titles:
        api_called = False
        try:
            if not translate_mod.needs_translation(title):
                translated = title
            elif api_calls >= max_calls:
                continue  # 预算用完：该标题留到下一轮
            else:
                translated = translate_mod.translate_text(title, target_lang=target_lang)
                api_calls += 1
                api_called = True
                if delay:
                    time.sleep(delay)
        except Exception as exc:
            logger.warning("翻译标题失败（用原标题）: %s", exc)
            continue
        if api_called and translated == title:
            # 接口调了但返回了原标题（translate_text 失败回退）：
            # 不存！否则该标题会被当成“已翻好”而永久失去重试机会。
            # 下一轮它仍是 title_zh 为空，会再试一次。
            continue
        conn.execute(
            "UPDATE items SET title_zh = ? "
            "WHERE title = ? AND (title_zh IS NULL OR title_zh = '')",
            (translated, title),
        )
    conn.commit()
    if api_calls:
        logger.info("本轮翻译标题 %d 条（接口调用）", api_calls)
    return api_calls


def _translate_pending_events(conn, events: list[dict], tr_cfg: dict) -> int:
    """翻译本轮事件里还没有中文名的事件，返回本轮翻译接口调用次数。

    聚类时已从成员标题的 title_zh 算出 name_zh 的事件，persist_events
    已存进库，这里查库跳过；其余的在这里直接翻事件名。中文事件名
    直接记 name_zh=原名，不调接口。每轮上限 10 次接口调用。
    """
    if not tr_cfg.get("enabled", False) or not events:
        return 0
    target_lang = str(tr_cfg.get("target_lang", "zh-CN"))
    delay = max(0.0, float(tr_cfg.get("delay_seconds", 0.5)))
    api_calls = 0
    for ev in events:
        event_id = ev.get("event_id", "")
        name = (ev.get("name") or "").strip()
        if not event_id or not name:
            continue
        row = conn.execute(
            "SELECT name_zh FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row and (row["name_zh"] or "").strip():
            continue
        api_called = False
        try:
            if not translate_mod.needs_translation(name):
                translated = name
            elif api_calls >= EVENT_TRANSLATE_PER_ROUND:
                continue  # 预算用完：该事件留到下一轮
            else:
                translated = translate_mod.translate_text(name, target_lang=target_lang)
                api_calls += 1
                api_called = True
                if delay:
                    time.sleep(delay)
        except Exception as exc:
            logger.warning("翻译事件名失败（用原名）: %s", exc)
            continue
        if api_called and translated == name:
            # 同上：接口失败回退返回原名时不存，下一轮再试。
            continue
        dbmod.set_event_name_zh(conn, event_id, translated)
    return api_calls


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
    # GDELT 单独间隔（默认 60 分钟，见 config.yaml）：2026-11-15 连续 3 次失败
    # （两次 429、一次返回限流提示文本而非 JSON），gdelt.py 里已加退避重试，
    # 这里再把频率从每 15 分钟降到每小时 1 次。GDELT 没有互动量数据，
    # 降频对增长计算几乎没有损失；每次尝试后都记时间（成功或被限流都一样），
    # 避免限流后每 15 分钟又去撞一次。
    gdelt_cfg = sources.get("gdelt", {})
    if gdelt_cfg.get("enabled", True):
        gdelt_interval_minutes = int(gdelt_cfg.get("interval_minutes", 60))
        gdelt_state_path = Path(db_path).parent / ".gdelt_last_run"
        if _youtube_due(gdelt_state_path, gdelt_interval_minutes):
            run("gdelt", lambda: gdelt.fetch(gdelt_cfg.get("query", "")))
            _mark_youtube_run(gdelt_state_path)
        else:
            counts["gdelt"] = 0
            logger.info("GDELT 本轮跳过：未满 %d 分钟间隔", gdelt_interval_minutes)
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
                "SELECT id, growth FROM items WHERE url = ?", (item.get("url", ""),)
            ).fetchone()
            previous = None
            previous_growth = 0.0
            prev_collected_at = None
            if row:
                # 有上一轮快照：按真实增量算；同时读出上轮增速，供算加速度。
                snap = dbmod.previous_snapshot(conn, int(row["id"]))
                if snap is not None:
                    previous, prev_collected_at = snap
                previous_growth = float(row["growth"] or 0)
            if previous is None:
                # 首次见到（没有任何快照）：本轮只作基线——增速记 0，
                # 不把该内容此前的全部历史互动量当成一轮的增量。
                # 快照照常存，下一轮开始算真实增量。否则一个有 12 万
                # 播放的老视频第一次被采到时，会被算成 30,800/小时
                # 的增速，霸占“增长最快”榜并污染事件 max_growth。
                previous = float(item.get("engagement") or 0)
                previous_growth = 0.0
            cross = len(title_platforms.get(item.get("title", "").strip().lower(), set()))
            # 名义间隔：YouTube 快照是每 30 分钟一次，用 0.5 小时；
            # 其他平台沿用全局扫描间隔（默认 0.25 小时）。
            nominal_hours = (
                youtube_interval_hours if item.get("platform") == "youtube" else interval_hours
            )
            # 增速除以两次快照之间的真实间隔，而不是名义间隔：
            # 定时器每 15 分钟一跳，YouTube 实际间隔会被量化成 30–45 分钟，
            # 电脑休眠/重启后间隔更长；一直除以 0.5 小时会把增速高估
            # （实测：40 分钟间隔、增量 469.2 被算成 938.4/小时，
            # 真实约 704/小时）。间隔短于名义值时（手动连跑两轮）
            # 仍按名义值算，避免除以几秒钟把增速吹上天。
            item_hours = nominal_hours
            if prev_collected_at:
                prev_dt = parse_dt(prev_collected_at)
                if prev_dt is not None:
                    elapsed = (datetime.now(timezone.utc) - prev_dt).total_seconds() / 3600
                    item_hours = max(nominal_hours, elapsed)
            result = score_item(
                item,
                previous_engagement=previous,
                previous_growth=previous_growth,
                interval_hours=item_hours,
                cross_platform_count=max(1, cross),
            )
            item_id = dbmod.upsert_item(conn, item, result)
            dbmod.record_snapshot(conn, item_id, float(item.get("engagement") or 0), result["score"])
            saved += 1

        # 标题翻译：本轮新入库、且没有中文标题的内容。评分/入库已经完成，
        # 翻译绝不拖慢或打断它们；失败只记 warning（见 _translate_pending_items）。
        try:
            counts["translated"] = _translate_pending_items(conn, _translation_config(cfg))
        except Exception as exc:
            logger.warning("标题翻译失败（不影响采集）: %s", exc)
            counts["translated"] = 0

        # P2 准备：每轮采集后聚类并持久化事件快照（失败不影响本轮采集）。
        # 只有接在这里，events / event_snapshots 才会随扫描自动积累，
        # 事件才有跨轮次的稳定 ID 与可比的历史，爆发检测才有上一轮可比。
        try:
            from trend_radar.burst import detect_bursts
            from trend_radar.clustering import cluster_from_db, persist_events

            events = cluster_from_db(db_path)
            persist_events(conn, events)
            # 事件名翻译：必须在 detect_bursts 之前，burst 里才能带上 name_zh；
            # 失败不影响爆发检测与提醒。
            try:
                _translate_pending_events(conn, events, _translation_config(cfg))
            except Exception as exc:
                logger.warning("事件名翻译失败（不影响采集）: %s", exc)
            bursts = detect_bursts(conn, thresholds=cfg.get("burst"))
            counts["events"] = len(events)
            counts["bursts"] = len(bursts)
            if bursts:
                logger.info(
                    "爆发事件 %d 个: %s",
                    len(bursts),
                    "; ".join(f"{b['name']}({','.join(b['reasons'])})" for b in bursts),
                )
                # P3 Telegram 提醒：未配 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
                # 时 send_burst_alerts 直接跳过、不发任何请求；发送失败只记
                # warning，不影响本轮采集结果。见 docs/P3_ALERTS.md。
                try:
                    from trend_radar.alerts import send_burst_alerts

                    if cfg.get("telegram", {}).get("enabled", True):
                        alert_result = send_burst_alerts(bursts)
                        counts["alerts_sent"] = alert_result["sent"]
                        counts["alerts_failed"] = len(alert_result["errors"])
                        if alert_result["errors"]:
                            logger.warning(
                                "Telegram 提醒失败 %d 条: %s",
                                len(alert_result["errors"]),
                                alert_result["errors"],
                            )
                    else:
                        counts["alerts_sent"] = 0
                        counts["alerts_failed"] = 0
                except Exception as exc:  # 提醒失败不影响采集结果
                    logger.warning("Telegram 提醒异常（不影响采集）: %s", exc)
                    counts["alerts_sent"] = 0
                    counts["alerts_failed"] = len(bursts)
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
