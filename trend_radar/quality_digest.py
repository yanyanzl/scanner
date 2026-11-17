"""每日质量简报：扫描器每天一次把质量摘要推送到 Muse 对话。

“结果→分析→优化”的循环机制（2026-11-16 用户要求）：扫描器每天
早上 8 点（调度器本地时间）把昨日的质量摘要打进 Muse 对话，
Cheese 收到后分析并优化。简报只做摘要，不做决策——优化动作仍由
Cheese 在对话里做（或经用户确认后做）。

内容（紧凑，约 1000 字符以内）：日期；生成时间（发送当时的本机
当地时间，与报告期日期不同）；Top 5 事件（当前综合排名）；
昨日计数（事件数、爆发数、被抑制的重复出现数、Telegram 已发、
Muse 已发/失败）；质量信号（Top 10 里单平台事件数、hashtag 占比
高的事件数、recurrence 事件数、通用标题数）。

标记：第一行是【质量简报｜quality-digest v1｜date=YYYY-MM-DD】，
与爆发提醒的 scanner-alert 标记区分开——parse_scanner_marker
不会把它认成 burst 提醒；识别用这里的 is_quality_digest。

数据来源：轮次计数由 collect_once 每轮结束后写进
data/.quality_daily.json（按本地日期分桶，只保留昨天和今天）；
事件数与 Top 10 现场从数据库算（load_top_events + 最近 48 小时
聚类里 first_seen 落在昨天的事件数）。发送走 muse_chat 已有的
通用发送路径（send_to_muse_chat），不另写 AppleScript/CDP。
发送失败只记 warning，不影响扫描轮次；muse_chat 没启用时跳过。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from trend_radar.timefmt import format_local_time

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

DIGEST_MARKER_VERSION = "v1"
_DIGEST_MARKER_RE = re.compile(
    r"^【质量简报｜quality-digest v(\d+)｜date=(\d{4}-\d{2}-\d{2})】$"
)

DIGEST_STATE_FILENAME = ".quality_daily.json"
DEFAULT_DIGEST_HOUR = 8
DIGEST_MAX_CHARS = 1000
HASHTAG_HIGH_RATIO = 0.5
_TOP_EVENTS_FOR_DIGEST = 10
_TOP_EVENTS_SHOWN = 5
_NAME_MAX_CHARS = 20

# 轮次计数里累加的键（collect_once 每轮的 counts 字典里同名键）。
_COUNT_KEYS = (
    ("bursts", "bursts"),
    ("recurrence", "bursts_recurrence"),
    ("telegram_sent", "alerts_sent"),
    ("telegram_failed", "alerts_failed"),
    ("muse_sent", "muse_chat_sent"),
    ("muse_failed", "muse_chat_failed"),
)


def parse_quality_digest(text: str | None) -> dict | None:
    """识别质量简报标记：第一行是【质量简报｜quality-digest vN｜date=YYYY-MM-DD】。

    返回 {"version": int, "date": "YYYY-MM-DD"}；不是简报返回 None。
    与 muse_chat.parse_scanner_marker 互不干扰：scanner-alert 的正则
    只认【扫描器提醒｜scanner-alert ...】，简报的正则只认这一行。
    """
    if not text:
        return None
    first = str(text).splitlines()[0].strip() if str(text).splitlines() else ""
    match = _DIGEST_MARKER_RE.match(first)
    if not match:
        return None
    return {"version": int(match.group(1)), "date": match.group(2)}


def is_quality_digest(text: str | None) -> bool:
    """一段文本是不是质量简报（只看第一行标记）。"""
    return parse_quality_digest(text) is not None


def load_digest_config(cfg: dict | None = None) -> dict:
    """读 quality_digest 一节；缺失时默认 enabled=True、hour=8。

    cfg 是已加载的 config 字典（调度器传）；不传时读 config.yaml。
    hour 只取 0-23（取模），enabled 缺失时默认 True。
    """
    out = {"enabled": True, "hour": DEFAULT_DIGEST_HOUR}
    section = None
    if isinstance(cfg, dict):
        section = cfg.get("quality_digest")
    if section is None:
        try:
            import yaml  # type: ignore

            raw = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
            section = raw.get("quality_digest")
        except Exception:
            section = None
    if isinstance(section, dict):
        if section.get("enabled") is not None:
            out["enabled"] = bool(section["enabled"])
        if section.get("hour") is not None:
            try:
                out["hour"] = int(section["hour"]) % 24
            except (TypeError, ValueError):
                pass
    return out


def _state_path(db_path: str | Path) -> Path:
    return Path(db_path).parent / DIGEST_STATE_FILENAME


def _load_daily_state(db_path: str | Path) -> dict:
    """读轮次计数 state 文件；缺失/损坏返回 {}（不抛异常）。"""
    try:
        data = json.loads(_state_path(db_path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_daily_state(db_path: str | Path, state: dict) -> bool:
    """原子写轮次计数 state 文件（临时文件 + os.replace）；失败返回 False。"""
    try:
        path = _state_path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:
        return False


def record_round_counts(
    db_path: str | Path, counts: dict | None, today: str | None = None
) -> bool:
    """把一轮的计数累加进当日桶（按本地日期）。

    由 collect_once 每轮结束后调用；失败（state 文件写不出去等）
    返回 False，调用方只记 warning、不影响采集。桶里只保留今天和
    昨天，更早的日期每次写入时清掉，文件不会无限长大。
    """
    try:
        day = today or datetime.now().date().isoformat()
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        state = _load_daily_state(db_path)
        bucket = state.get(day)
        if not isinstance(bucket, dict):
            bucket = {}
            state[day] = bucket
        bucket["rounds"] = int(bucket.get("rounds", 0)) + 1
        for bucket_key, count_key in _COUNT_KEYS:
            bucket[bucket_key] = int(bucket.get(bucket_key, 0)) + int(
                (counts or {}).get(count_key, 0) or 0
            )
        for old in [k for k in state if k not in (day, yesterday)]:
            del state[old]
        return _save_daily_state(db_path, state)
    except Exception:
        return False


def _yesterday() -> str:
    return (datetime.now().date() - timedelta(days=1)).isoformat()


def gather_digest_data(db_path: str | Path, date: str | None = None) -> dict:
    """为指定日期（默认昨天，本地日期）汇总简报数据。

    返回 {"date", "top_events", "counts", "events_count"}：
    top_events 是当前 Top 10（最近 24 小时综合排名，见
    dashboard_data.load_top_events）；counts 是 state 文件里该日的
    轮次计数桶（没有时为 None）；events_count 是 first_seen 落在
    该日的去重事件数（最近 48 小时聚类里按本地日期数）。DB 不存在
    时 top_events 为 []、events_count 为 0，不抛异常。
    """
    day = date or _yesterday()
    top_events: list[dict] = []
    events_count = 0
    try:
        from trend_radar.dashboard_data import load_top_events
        from trend_radar.clustering import cluster_from_db
        from trend_radar.scoring import parse_dt

        top_events = load_top_events(db_path, limit=_TOP_EVENTS_FOR_DIGEST) or []
        events = cluster_from_db(db_path, hours=48) or []
        for ev in events:
            first = parse_dt(ev.get("first_seen"))
            if first is None:
                continue
            if first.astimezone().date().isoformat() == day:
                events_count += 1
    except Exception as exc:
        logger.warning("质量简报汇总数据失败：%s", exc)
    state = _load_daily_state(db_path)
    counts = state.get(day) if isinstance(state.get(day), dict) else None
    return {
        "date": day,
        "top_events": top_events,
        "counts": counts,
        "events_count": events_count,
        # 生成时间：汇总当时的本机当地时间（ISO，带本地时区偏移）。
        # 与报告期日期（date，昨天）是两回事：date 是“报哪天”，
        # generated_at 是“这份简报什么时候生成的”。
        "generated_at": datetime.now().astimezone().isoformat(),
    }


def _short_name(name: str) -> str:
    """简报里的事件名：去换行、截到 20 字（超了加 …）。"""
    text = " ".join(str(name or "").split())
    if len(text) <= _NAME_MAX_CHARS:
        return text
    return text[:_NAME_MAX_CHARS] + "…"


def format_quality_digest(data: dict) -> str:
    """把汇总数据格式化成简报文本（纯函数，可单测）。

    data 是 gather_digest_data 的返回值。Top 5 取 top_events 的前 5
    条（当前综合排名）；质量信号对整个 Top 10 列表算（单平台数、
    hashtag 占比 >= 0.5 的事件数、recurrence 事件数、通用标题数）。
    计数缺失时写“昨日无轮次记录”，Top 10 为空时质量信号写“暂无数据”。
    data 里有 generated_at 时在标题行后加一行生成时间（本机当地
    时间，见 gather_digest_data）；没有时不写这一行。
    """
    date = data.get("date") or ""
    top_events = list(data.get("top_events") or [])
    shown = top_events[:_TOP_EVENTS_SHOWN]
    counts = data.get("counts")
    lines = [
        f"【质量简报｜quality-digest {DIGEST_MARKER_VERSION}｜date={date}】",
        f"📋 每日质量简报（{date}）",
    ]
    # 生成时间行（2026-11-16 第二批改进）：发送当时的本机当地时间，
    # 与报告期日期（date）不同，两行都保留。data 里没有 generated_at
    # 时不写这一行（不造假时间）。
    generated_local = format_local_time(data.get("generated_at"))
    if generated_local:
        lines.append(f"生成时间：{generated_local}（伦敦时间）")
    lines.append("Top 5（当前综合排名）：")
    if not shown:
        lines.append("暂无事件")
    for i, ev in enumerate(shown, 1):
        name = _short_name(ev.get("name") or "")
        score = ev.get("rank_score")
        score_s = f"{float(score):.1f}分" if score is not None else "—分"
        platforms = int(ev.get("platform_count") or 0)
        growth = ev.get("max_growth")
        growth_s = f"，增长{float(growth):.1f}/小时" if growth is not None else ""
        lines.append(f"{i}. {name} {score_s}，{platforms}平台{growth_s}")
    if counts is None:
        lines.append("当日计数：昨日无轮次记录（扫描器可能未运行）")
    else:
        parts = [f"事件{int(data.get('events_count') or 0)}个"]
        parts.append(f"爆发{int(counts.get('bursts', 0))}个")
        parts.append(f"重复出现被抑制{int(counts.get('recurrence', 0))}个")
        parts.append(f"Telegram已发{int(counts.get('telegram_sent', 0))}条")
        parts.append(
            f"Muse已发{int(counts.get('muse_sent', 0))}条"
            f"、失败{int(counts.get('muse_failed', 0))}条"
        )
        lines.append("当日计数：" + "，".join(parts))
    if top_events:
        single = sum(1 for e in top_events if int(e.get("platform_count") or 0) == 1)
        tag_high = sum(
            1
            for e in top_events
            if float(e.get("hashtag_ratio") or 0) >= HASHTAG_HIGH_RATIO
        )
        rec = sum(1 for e in top_events if e.get("is_recurrence"))
        generic = sum(1 for e in top_events if e.get("is_generic_title"))
        lines.append(
            f"质量信号（Top {len(top_events)}）：单平台{single}个"
            f"，hashtag占比高{tag_high}个，recurrence {rec}个"
            f"，通用标题{generic}个"
        )
    else:
        lines.append("质量信号：暂无 Top 10 数据")
    return "\n".join(lines)


def send_quality_digest(config: dict | None = None) -> dict:
    """构建昨日质量简报并打进 Muse 对话。返回 {"ok": bool, "reason": ...}。

    quality_digest.enabled 为 false、或 muse_chat 未启用、或昨日
    完全无数据时跳过（不发、不报错）。发送失败只记 warning，绝不
    抛异常、不影响扫描轮次。
    """
    cfg = config or {}
    try:
        if not load_digest_config(cfg).get("enabled", True):
            return {"ok": False, "reason": "digest_disabled"}
        muse_cfg = cfg.get("muse_chat") or {}
        if not muse_cfg.get("enabled", True):
            logger.info("每日质量简报跳过：muse_chat 未启用")
            return {"ok": False, "reason": "muse_chat_disabled"}
        db_path = Path(cfg.get("db_path", "data/trends.sqlite3"))
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        data = gather_digest_data(db_path)
        if not data["top_events"] and data["counts"] is None:
            logger.info("每日质量简报跳过：昨日无数据（扫描器可能未运行）")
            return {"ok": False, "reason": "no_data"}
        from trend_radar.muse_chat import send_to_muse_chat

        result = send_to_muse_chat(format_quality_digest(data), muse_cfg)
        if result.get("ok"):
            logger.info("每日质量简报已发送（%s）", data["date"])
            return {"ok": True, "date": data["date"]}
        logger.warning("每日质量简报发送失败：%s", result.get("reason"))
        return {"ok": False, "reason": result.get("reason", "unknown")}
    except Exception as exc:  # 简报失败不影响扫描轮次
        logger.warning("每日质量简报异常（不影响扫描）: %s", exc)
        return {"ok": False, "reason": str(exc)}
