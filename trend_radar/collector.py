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
from trend_radar import downloads as downloads_mod
from trend_radar import topics as topics_mod
from trend_radar import translate as translate_mod
from trend_radar.adapters import (
    gdelt,
    guardian,
    newsapi,
    newsdata,
    nyt,
    reddit,
    rss,
    tiktok,
    x_api,
    youtube,
)
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


# 免费新闻 API 的默认采集间隔（分钟）：按各自免费额度算出，
# 每个来源每轮调 topics 里的查询词（默认 5 个），见 docs/NEWS_APIS.md：
# - newsapi：100 次/天，90 分钟 → 16 轮/天 × 5 = 80 次/天（余 20）。
# - guardian：500 次/天，15 分钟 → 96 轮/天 × 5 = 480 次/天（余 20）。
# - nyt：500 次/天、5 次/分钟，15 分钟 → 480 次/天（余 20）；
#   另外每轮内两次调用之间隔 12 秒（见 adapters/nyt.py）。
# - newsdata：200 credits/天，45 分钟 → 32 轮/天 × 5 = 160 credits/天（余 40）。
_NEWS_API_DEFAULT_INTERVALS = {
    "newsapi": 90,
    "guardian": 15,
    "nyt": 15,
    "newsdata": 45,
}

# 免费新闻 API：模块、.env key 名。查询词全部来自 topics。
_NEWS_APIS = (
    ("newsapi", newsapi, "NEWSAPI_KEY"),
    ("guardian", guardian, "GUARDIAN_API_KEY"),
    ("nyt", nyt, "NYT_API_KEY"),
    ("newsdata", newsdata, "NEWSDATA_API_KEY"),
)


# DeepL 轮次间隔（分钟）：DeepL 免费额度是一次性 100 万字符（不是
# 按月重置），所以 DeepL 每小时最多 1 次轮次（state 文件
# data/.deepl_last_run，和 YouTube/GDELT 同一套间隔门控），见
# docs/TRANSLATION.md。Azure 不再用间隔门控——每轮都跑，靠每轮
# 600 字符上限 + 每月 180 万字符上限控用量（state 文件
# data/.azure_usage.json）；旧的 data/.azure_last_run 不再使用。
DEEPL_TRANSLATION_INTERVAL_MINUTES = 60

# 翻译范围：只翻排名前 N 的事件（见 docs/TRANSLATION.md）。
TOP_TRANSLATE_EVENTS = 5


def _translation_config(cfg: dict) -> dict:
    """读 config.yaml 的 translation 一节。

    注意：配置里完全没有 translation 一节时默认关闭——这样旧的
    内联配置（测试里大量使用）不会突然开始调翻译接口；正式的
    config.yaml 里有该节，enabled 默认为 true。
    azure_max_per_round / deepl_max_per_round 是每轮各提供方最多翻
    的文本条数（Top 5 事件名 + 爆发事件名 + 各事件分数最高的成员标题，
    见 _collect_priority_texts）；azure_max_chars_per_round 是 Azure
    每轮字符硬上限（默认 600：600 × 96 轮/天 × 31 天 = 1,785,600，
    低于月度上限 1,800,000，见 docs/TRANSLATION.md）；
    azure_monthly_cap_chars 是 Azure 月度字符上限（默认 1,800,000，
    用 data/.azure_usage.json 计数兜底）；DeepL 每轮字符硬上限
    1200 在 translate.py 里（DeepL 每小时最多 1 次，不跟 Azure
    每轮跑——它的一次性总额经不起每轮跑）。
    """
    section = cfg.get("translation")
    if not isinstance(section, dict):
        return {
            "enabled": False,
            "target_lang": "zh-CN",
            "azure_max_per_round": 15,
            "deepl_max_per_round": 15,
            "azure_max_chars_per_round": translate_mod.AZURE_MAX_CHARS_PER_ROUND,
            "azure_monthly_cap_chars": translate_mod.AZURE_MONTHLY_CAP_CHARS,
        }
    return {
        "enabled": bool(section.get("enabled", True)),
        "target_lang": str(section.get("target_lang", "zh-CN")),
        "azure_max_per_round": int(section.get("azure_max_per_round", 15)),
        "deepl_max_per_round": int(section.get("deepl_max_per_round", 15)),
        "azure_max_chars_per_round": max(
            0,
            int(
                section.get(
                    "azure_max_chars_per_round",
                    translate_mod.AZURE_MAX_CHARS_PER_ROUND,
                )
            ),
        ),
        "azure_monthly_cap_chars": max(
            0,
            int(
                section.get(
                    "azure_monthly_cap_chars",
                    translate_mod.AZURE_MONTHLY_CAP_CHARS,
                )
            ),
        ),
    }


def _ranking_top_events(events: list[dict], bursts: list[dict], cfg: dict) -> list[dict]:
    """取综合排名前 TOP_TRANSLATE_EVENTS 的事件（见 trend_radar/event_ranking.py）。

    排名权重/半衰期读传入的 config（ranking 一节），缺失时用默认值。
    失败时返回 []（不翻译、不中断采集）。
    """
    try:
        from trend_radar.event_ranking import rank_events

        ranking = cfg.get("ranking") or {}
        weights = ranking.get("weights") if isinstance(ranking.get("weights"), dict) else None
        half_life = ranking.get("freshness_half_life_hours")
        penalty_max = ranking.get("hashtag_penalty_max")
        ranked = rank_events(
            events,
            bursts=bursts,
            weights=weights,
            freshness_half_life_hours=(float(half_life) if half_life is not None else None),
            hashtag_penalty_max=(float(penalty_max) if penalty_max is not None else None),
        )
        return ranked[:TOP_TRANSLATE_EVENTS]
    except Exception as exc:
        logger.warning("事件排名失败（本轮不翻译事件名）: %s", exc)
        return []


def _collect_priority_texts(
    events_by_id: dict, top_events: list[dict], bursts: list[dict]
) -> list[dict]:
    """收集本轮要翻译的文本（按优先级排序，每条带上它的用途）。

    顺序：Top 5 事件名 → 爆发事件名 → 各目标事件分数最高的成员标题。
    事件名在前、成员标题在后——每轮 600 字符预算不够时先砍成员标题，
    事件名优先保住（提醒和榜单上先看到的都是事件名）。同一文本只
    出现一次（事件名经常就是它分数最高的成员标题），用途合并记录。
    没有可翻译内容的文本（纯 hashtag 串、纯 URL、纯 @提及）直接跳过，
    不进目标集（见 add 里的 needs_translation 判定）。
    每条形如 {"text": ..., "events": {event_id, ...}, "items": {title, ...}}：
    events 是以该文本为事件名的事件，items 是以该文本为成员标题的条目。
    """
    ordered: list[dict] = []
    index: dict[str, dict] = {}

    def add(text: str, event_id: str | None = None, item_title: str | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        # 没有可翻译内容的文本不进翻译目标集：纯 hashtag 串、纯 URL、
        # 纯 @提及等——Azure 对这类文本原样返回，翻了等于没翻，还占
        # 预算、误报“没有可用译文”（2026-11-16 10:03 轮实测）。用
        # needs_translation 判定（去掉 URL/@提及/hashtag 后没有字母
        # 的跳过）；带正文的标题哪怕带 hashtag 也照常进集。
        if not translate_mod.needs_translation(text):
            return
        entry = index.get(text)
        if entry is None:
            entry = {"text": text, "events": set(), "items": set()}
            index[text] = entry
            ordered.append(entry)
        if event_id:
            entry["events"].add(event_id)
        if item_title:
            entry["items"].add(item_title)

    for ev in top_events or []:
        add(ev.get("name", ""), event_id=ev.get("event_id", ""))
    for burst in bursts or []:
        # 爆发事件可能不在本轮聚类输出里：detect_bursts 从库里读所有事件，
        # 有快照的事件都会被判爆发（比如只有 1 条快照的老事件），而
        # cluster_from_db 只聚最近窗口内的内容——窗口外的事件不在 events
        # 里。它的事件名一样要翻：用 burst 自带的名字（来自库里 events.name），
        # 不能因为 events_by_id 里没有它就跳过。跳过的话，它的提醒永远显示
        # 原标题，还会和已翻成中文的同名事件拆成两条提醒（2026-11-15 实测：
        # 6 个同名直播事件拆成中文/原文两条）。
        event_id = burst.get("event_id", "")
        ev = events_by_id.get(event_id)
        name = (ev.get("name", "") if ev else "") or burst.get("name", "")
        if name:
            add(name, event_id=event_id)
    target_ids: list[str] = []
    for ev in top_events or []:
        target_ids.append(ev.get("event_id", ""))
    for burst in bursts or []:
        target_ids.append(burst.get("event_id", ""))
    seen_events: set[str] = set()
    for event_id in target_ids:
        if not event_id or event_id in seen_events or event_id not in events_by_id:
            continue
        seen_events.add(event_id)
        members = events_by_id[event_id].get("items") or []
        if not members:
            continue
        top_member = max(members, key=lambda it: float(it.get("score") or 0))
        title = (top_member.get("title") or "").strip()
        if title:
            add(title, item_title=title)
    return ordered


def _translate_priority_texts(
    conn,
    events: list[dict],
    top_events: list[dict],
    bursts: list[dict],
    tr_cfg: dict,
    azure_key: str,
    azure_region: str,
    deepl_key: str,
    db_path: Path,
) -> tuple[int, int]:
    """翻本轮优先文本（Top 5 事件名 + 爆发事件名 + 各事件最高分成员标题）。

    提供方顺序：Azure 主用——**每轮都跑**（配了 key 就跑，没有每小时
    门控）；月度用量用 data/.azure_usage.json 计数，剩余额度不够时
    本轮只翻剩余额度内的文本、用完时整轮跳过 Azure。Azure 不可用
    （没配 key、429/403、请求失败、月度用量用完）时改走 DeepL 备用
    （每小时最多 1 次，state 文件 data/.deepl_last_run）；两个都没配
    或 DeepL 也没到时间时本轮不翻译。每轮只调一次提供方（Azure 一次
    请求打包全部文本；DeepL 逐条调）。已有中文名的事件/标题不再调接口；
    中文文本直接记原文为译文，不占预算。失败的翻译不写库，下一轮再试。
    翻出的事件名同时写回 bursts 列表里的 name_zh（提醒当轮就能用中文名）
    与 events.name_zh；翻出的成员标题写 items.title_zh。返回
    (azure 翻出的条数, deepl 翻出的条数)。本函数本身不抛异常。
    """
    if not tr_cfg.get("enabled", False):
        return 0, 0
    events_by_id = {ev.get("event_id", ""): ev for ev in events if ev.get("event_id")}
    entries = _collect_priority_texts(events_by_id, top_events, bursts)
    if not entries:
        return 0, 0

    # 目标解析：去掉已有中文名的，中文文本直接记原文为译文（不调接口）。
    pending_texts: list[str] = []
    entry_by_text: dict[str, dict] = {}
    for entry in entries:
        text = entry["text"]
        live_events = set()
        for event_id in entry["events"]:
            row = conn.execute(
                "SELECT name_zh FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if not row or not (row["name_zh"] or "").strip():
                live_events.add(event_id)
        live_items = set()
        for title in entry["items"]:
            row = conn.execute(
                "SELECT 1 FROM items WHERE title = ? "
                "AND title_zh IS NOT NULL AND title_zh != '' LIMIT 1",
                (title,),
            ).fetchone()
            if not row:
                live_items.add(title)
        if not live_events and not live_items:
            continue
        if not translate_mod.needs_translation(text):
            for event_id in live_events:
                dbmod.set_event_name_zh(conn, event_id, text)
            for title in live_items:
                conn.execute(
                    "UPDATE items SET title_zh = ? "
                    "WHERE title = ? AND (title_zh IS NULL OR title_zh = '')",
                    (text, title),
                )
            conn.commit()
            continue
        entry["events"] = live_events
        entry["items"] = live_items
        entry_by_text[text] = entry
        pending_texts.append(text)
    if not pending_texts:
        return 0, 0

    def apply_results(results: dict[str, str]) -> int:
        """把一批译文写回库 + 内存里的 bursts；返回实际写进的条数。"""
        applied = 0
        translated_names: dict[str, str] = {}
        # 归一化原名 -> 译文：同名事件的译文按文本归属——只要 burst 的
        # 原名就是某条已翻文本（归一化后相同），不管它的 event_id 有没有
        # 进本轮的翻译目标集，都用同一条中文名（见 _collect_priority_texts
        # 里 burst 循环的注释；2026-11-15 实测：6 个同名直播事件里有几个
        # 不在本轮聚类输出里，翻译只写回了在集里的，提醒按显示名去重时
        # 拆成了中文/原文两条）。
        translated_by_name: dict[str, str] = {}
        for text, translated in results.items():
            entry = entry_by_text.get(text)
            if entry is None or not translated or translated == text:
                continue
            applied += 1
            translated_by_name[" ".join(text.lower().split())] = translated
            for event_id in entry["events"]:
                dbmod.set_event_name_zh(conn, event_id, translated)
                translated_names[event_id] = translated
            for title in entry["items"]:
                conn.execute(
                    "UPDATE items SET title_zh = ? "
                    "WHERE title = ? AND (title_zh IS NULL OR title_zh = '')",
                    (translated, title),
                )
        conn.commit()
        for burst in bursts or []:
            name_zh = translated_names.get(burst.get("event_id", ""))
            if not name_zh:
                raw = " ".join(str(burst.get("name") or "").lower().split())
                if raw:
                    name_zh = translated_by_name.get(raw)
            if name_zh:
                burst["name_zh"] = name_zh
                # 同名事件没进翻译目标集时库里的 name_zh 还是空的：顺手
                # 补写，下轮 detect_bursts 直接读到中文名；已有中文名的不动。
                event_id = burst.get("event_id", "")
                if event_id and event_id not in translated_names:
                    row = conn.execute(
                        "SELECT name_zh FROM events WHERE event_id = ?", (event_id,)
                    ).fetchone()
                    if row is not None and not (row["name_zh"] or "").strip():
                        dbmod.set_event_name_zh(conn, event_id, name_zh)
        return applied

    azure_usage_path = Path(db_path).parent / translate_mod.AZURE_USAGE_FILENAME
    azure_monthly_cap = max(
        0, int(tr_cfg.get("azure_monthly_cap_chars", translate_mod.AZURE_MONTHLY_CAP_CHARS))
    )
    azure_per_round_cap = max(
        0, int(tr_cfg.get("azure_max_chars_per_round", translate_mod.AZURE_MAX_CHARS_PER_ROUND))
    )
    deepl_state_path = Path(db_path).parent / ".deepl_last_run"
    deepl_due = bool(deepl_key) and _youtube_due(
        deepl_state_path, DEEPL_TRANSLATION_INTERVAL_MINUTES
    )
    azure_count = 0
    deepl_count = 0
    azure_attempted = False
    azure_ok = False
    if azure_key:
        # Azure 每轮都跑（2026-11-15 用户决定：每 15 分钟一轮就翻一次，
        # 不再每小时最多 1 次）。用量保障靠两层：每轮 600 字符硬上限
        # （600 × 96 轮/天 × 31 天 = 1,785,600 < 1,800,000）+ 月度
        # 上限（data/.azure_usage.json 计数，用完本轮跳过）。
        azure_attempted = True
        _month, used = translate_mod.load_azure_usage(azure_usage_path)
        remaining = azure_monthly_cap - used
        if remaining <= 0:
            logger.info(
                "Azure 本月用量已达上限（%d/%d 字符），本轮跳过 Azure 翻译，改走 DeepL",
                used,
                azure_monthly_cap,
            )
        else:
            try:
                results, chars_used, _exhausted = translate_mod.azure_translate_many(
                    pending_texts,
                    azure_key,
                    region=azure_region,
                    max_texts=max(0, int(tr_cfg.get("azure_max_per_round", 15))),
                    max_chars=min(azure_per_round_cap, remaining),
                )
                azure_count = apply_results(results)
                azure_ok = bool(results)
                if results:
                    # 只记成功请求的字符：失败的请求不计（azure_translate_many
                    # 失败时抛异常、走不到这里）。
                    monthly_total = translate_mod.record_azure_usage(
                        azure_usage_path, chars_used
                    )
                    if monthly_total is None:
                        logger.info(
                            "Azure 本轮翻译优先文本 %d 条（%d 字符）",
                            len(results),
                            chars_used,
                        )
                    else:
                        logger.info(
                            "Azure 本轮翻译优先文本 %d 条（%d 字符），"
                            "本月累计 %d/%d 字符",
                            len(results),
                            chars_used,
                            monthly_total,
                            azure_monthly_cap,
                        )
            except translate_mod.AzureUnavailable:
                pass  # 下面改走 DeepL；warning 已在 azure_translate_many 里记过
            except Exception as exc:
                logger.warning("Azure 翻译异常（本轮改走 DeepL）: %s", exc)
    if not azure_ok:
        # DeepL 备用：只有 Azure 没配 key 或本轮 Azure 没翻出东西时才跑
        # （Azure 成功翻出时不跑——不翻倍；Azure 月度用量用完时也到这里，
        # 同样按 DeepL 自己的每小时门控走）。
        deepl_may_run = (not azure_key) or azure_attempted
        if deepl_may_run and deepl_due:
            try:
                results, chars_used, _exhausted = translate_mod.deepl_translate_many(
                    pending_texts,
                    deepl_key,
                    max_texts=max(0, int(tr_cfg.get("deepl_max_per_round", 15))),
                    max_chars=translate_mod.DEEPL_MAX_CHARS_PER_ROUND,
                    target_lang=translate_mod.deepl_target_lang(
                        str(tr_cfg.get("target_lang", "zh-CN"))
                    ),
                )
                deepl_count = apply_results(results)
                if results:
                    logger.info("DeepL 本轮翻译优先文本 %d 条（%d 字符）", len(results), chars_used)
            except Exception as exc:
                logger.warning("DeepL 翻译异常（不影响采集）: %s", exc)
            _mark_youtube_run(deepl_state_path)
        elif deepl_may_run and not deepl_key:
            # Azure 失败且 DeepL 没配 key：本轮注定不翻译，记一行——
            # 否则 azure/deepl 双 0 且没有任何说明（2026-11-16 连续
            # 两轮就是这种情况：deepl_due 里 bool(deepl_key) 为假，
            # 整个 DeepL 分支被静默跳过）。
            logger.info("Azure 本轮没翻出东西，DeepL 也没配 key（DEEPL_API_KEY），本轮不翻译")
        elif deepl_may_run and not deepl_due:
            logger.info("Azure 本轮没翻出东西，DeepL 本轮还没到时间（每小时最多 1 次），本轮不翻译")
    return azure_count, deepl_count


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
    # 搜索主题：所有需要关键词的来源共用（见 trend_radar/topics.py）。
    topics = topics_mod.load_topics(cfg)

    # 下载文件自动清理：data/downloads/ 里超过 1 小时的文件删掉
    # （见 trend_radar/downloads.py）。失败不影响采集。
    try:
        cleaned = downloads_mod.cleanup_downloads(Path(db_path).parent / "downloads")
        if cleaned:
            logger.info("下载目录清理：删除 %d 个过期文件", cleaned)
    except Exception as exc:
        logger.warning("下载目录清理失败（不影响采集）: %s", exc)

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
        # RSS 出版商 feed（BBC 等）是配置里写死的；Google News / Bing
        # 的搜索 feed 是从 topics 现场生成的（见 trend_radar/topics.py），
        # 改主题不用改代码。aggregators: false 可以只保留出版商 feed。
        rss_cfg = sources["rss"]
        feeds = list(rss_cfg.get("feeds", []))
        if rss_cfg.get("aggregators", True):
            feeds += topics_mod.google_news_feed_urls(topics)
            feeds += topics_mod.bing_news_feed_urls(topics)
        run("rss", lambda: rss.fetch(feeds))
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
            # GDELT 查询词默认由 topics 拼成；配置里写了 query 时以配置为准。
            gdelt_query = gdelt_cfg.get("query") or topics_mod.gdelt_query(topics)
            run("gdelt", lambda: gdelt.fetch(gdelt_query))
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

    # 免费新闻 API（NewsAPI / Guardian / NYT / NewsData）：查询词全部来自
    # topics；每个来源单独限频（state 文件），间隔按免费额度算出，
    # 见 docs/NEWS_APIS.md。无 key 时 adapter 直接返回 []、不发请求；
    # 只有配了 key 才记时间，这样新填 key 后下一轮立即就能跑，不用等满间隔。
    for api_name, api_module, env_key in _NEWS_APIS:
        api_cfg = sources.get(api_name, {})
        if not api_cfg.get("enabled", False):
            continue
        api_interval = int(
            api_cfg.get("interval_minutes", _NEWS_API_DEFAULT_INTERVALS[api_name])
        )
        api_state_path = Path(db_path).parent / f".{api_name}_last_run"
        if _youtube_due(api_state_path, api_interval):
            run(api_name, lambda m=api_module: m.fetch(topics))
            if os.environ.get(env_key):
                _mark_youtube_run(api_state_path)
        else:
            counts[api_name] = 0
            logger.info("%s 本轮跳过：未满 %d 分钟间隔", api_name, api_interval)

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

        # 翻译配置（本轮后面才翻：聚类、排名、爆发检测之后，发提醒之前，
        # 见下面的事件处理；只翻 Top 5 事件名 + 爆发事件名 + 各事件最高分
        # 成员标题，见 docs/TRANSLATION.md）。
        tr_cfg = _translation_config(cfg)
        counts["azure_translated"] = 0
        counts["deepl_translated"] = 0

        # P2 准备：每轮采集后聚类并持久化事件快照（失败不影响本轮采集）。
        # 只有接在这里，events / event_snapshots 才会随扫描自动积累，
        # 事件才有跨轮次的稳定 ID 与可比的历史，爆发检测才有上一轮可比。
        # round_now 只算一次，同时传给 persist_events 和 detect_bursts：
        # 快照的 collected_at 就是本轮的 now，detect_bursts 的
        # only_fresh 才能认出“本轮新产生的快照”。不传同一个 now，
        # 两边各自取当前时间、差几秒，容忍窗口（5 分钟）也能兜住，
        # 但同一个 now 更干净。only_fresh=True 是跨轮次重复触发的
        # 根治（2026-11-16：09:15 与 09:31 两轮逐字节重复，同一批
        # event_id 反复报警——陈旧快照的事件不再触发，见 burst.py）。
        try:
            from trend_radar.burst import detect_bursts
            from trend_radar.clustering import cluster_from_db, persist_events

            round_now = datetime.now(timezone.utc)
            events = cluster_from_db(db_path)
            persist_events(conn, events, now=round_now)
            bursts = detect_bursts(
                conn, thresholds=cfg.get("burst"), now=round_now, only_fresh=True
            )
            # 重复出现：同名事件在窗口内已跨平台出现过（burst 的
            # is_recurrence，见 trend_radar/burst.py）——不发提醒。
            # 排名和翻译仍用完整 bursts 列表：rank_events 会按
            # is_recurrence 去掉爆发加分和新鲜度分；翻译按文本去重，
            # 已翻过的名字不再调接口。
            recurrence_bursts = [b for b in bursts if b.get("is_recurrence")]
            alert_bursts = [b for b in bursts if not b.get("is_recurrence")]
            counts["bursts_recurrence"] = len(recurrence_bursts)
            if recurrence_bursts:
                window_h = (cfg.get("burst") or {}).get("recurrence_window_hours", 24)
                logger.info(
                    "重复出现 %d 个（同名事件 %s 小时内已跨平台），不发提醒: %s",
                    len(recurrence_bursts),
                    window_h,
                    "; ".join(b["name"] for b in recurrence_bursts),
                )
            # 优先翻译：必须在排名与爆发检测之后、发提醒之前——
            # 只翻 Top 5 事件名 + 爆发事件名 + 各事件最高分成员标题
            # （见 docs/TRANSLATION.md）。Azure 每轮都跑（月度用量
            # 封顶）、DeepL 每小时最多 1 次（备用）；翻出的事件名当轮
            # 就写进 bursts 的 name_zh，提醒里直接用中文名。失败不影响
            # 爆发检测与提醒。
            try:
                top_events = _ranking_top_events(events, bursts, cfg)
                azure_key = (
                    os.environ.get("AZURE_TRANSLATOR_KEY", "").strip()
                    if tr_cfg.get("enabled")
                    else ""
                )
                azure_region = (
                    os.environ.get("AZURE_TRANSLATOR_REGION", "").strip()
                    if tr_cfg.get("enabled")
                    else ""
                )
                deepl_key = (
                    os.environ.get("DEEPL_API_KEY", "").strip()
                    if tr_cfg.get("enabled")
                    else ""
                )
                azure_count, deepl_count = _translate_priority_texts(
                    conn,
                    events,
                    top_events,
                    bursts,
                    tr_cfg,
                    azure_key,
                    azure_region,
                    deepl_key,
                    db_path,
                )
                counts["azure_translated"] = azure_count
                counts["deepl_translated"] = deepl_count
            except Exception as exc:
                logger.warning("优先翻译失败（不影响采集）: %s", exc)
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

                    if cfg.get("telegram", {}).get("enabled", True) and alert_bursts:
                        alert_result = send_burst_alerts(alert_bursts)
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
                    counts["alerts_failed"] = len(alert_bursts)
                # Muse 对话提醒：与 Telegram 发同一批 alert_bursts
                # （已过质量门槛、去重、重复出现过滤），每条消息首行是
                # 结构化标记（见 trend_radar/muse_chat.py），由浏览器
                # 程序打进用户自己的 Chrome Muse 对话框。只受
                # muse_chat.enabled 控制，与 telegram.enabled 无关。
                # 失败只记 warning，不影响本轮采集结果。见 docs/MUSE_CHAT.md。
                try:
                    from trend_radar.muse_chat import send_alerts_to_muse_chat

                    if cfg.get("muse_chat", {}).get("enabled", True) and alert_bursts:
                        muse_result = send_alerts_to_muse_chat(
                            alert_bursts, cfg.get("muse_chat") or {}
                        )
                        counts["muse_chat_sent"] = muse_result["sent"]
                        counts["muse_chat_failed"] = len(muse_result["failed"])
                        if muse_result["failed"]:
                            logger.warning(
                                "Muse 对话提醒失败 %d 条: %s",
                                len(muse_result["failed"]),
                                muse_result["failed"],
                            )
                    else:
                        counts["muse_chat_sent"] = 0
                        counts["muse_chat_failed"] = 0
                except Exception as exc:  # 提醒失败不影响采集结果
                    logger.warning("Muse 对话提醒异常（不影响采集）: %s", exc)
                    counts["muse_chat_sent"] = 0
                    counts["muse_chat_failed"] = len(alert_bursts)
        except Exception as exc:  # 聚类/持久化失败不影响采集结果
            logger.warning("事件聚类/持久化失败（不影响采集）: %s", exc)
            counts["events"] = 0
            counts["bursts"] = 0
            counts["bursts_recurrence"] = 0
    finally:
        conn.close()

    # 每日质量简报：记下本轮的计数（按本地日期分桶，供次日 8 点的
    # 质量简报汇总；失败不影响本轮，见 trend_radar/quality_digest.py）。
    try:
        from trend_radar.quality_digest import record_round_counts

        record_round_counts(db_path, counts)
    except Exception as exc:
        logger.warning("质量简报计数记录失败（不影响采集）: %s", exc)

    counts["saved"] = saved
    logger.info("本轮采集完成: %s", counts)
    return counts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(collect_once())
