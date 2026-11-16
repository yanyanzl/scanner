"""P2 准备：事件爆发检测。

比较 events 表里每个事件的最新快照与上一轮快照，规则简单、可解释，
阈值是初始假设，需用真实快照数据校准（和评分权重一样，见 docs/P2_PREP.md）。

规则（默认阈值见 DEFAULTS / config.yaml 的 burst 一节）：
  (a) growth_spike：最新 max_growth 相对上一轮翻了 growth_multiplier 倍以上，
      且绝对增量不小于 growth_min_delta（防小基数抖动）。
  (b) score_surge：最新 total_score 相对上一轮翻了 score_multiplier 倍以上，
      且绝对增量不小于 score_min_delta。
  (c) new_multi_platform：事件只有一条快照（首次出现），且已出现在
      new_event_min_platforms 个或更多平台上——刚出现就跨平台，本身就是信号。
      但还要过最低质量门槛（2026-11-15，2026-11-16 收紧）：条数
      >= alert_min_items 或总分 >= alert_min_total_score，否则不算
      爆发。2026-11-16 生产实测：11 条/2.94 分的小丑短视频靠条数
      一条腿漏过去了（旧门槛 10 条），所以条数腿抬到 20 条。
      规则 (a)(b) 自带绝对增量门槛，不受这条影响。

重复出现（2026-11-15，2026-11-16 加固）：new_multi_platform 爆发里，
同名事件在 recurrence_window_hours 小时内已跨平台出现过的，记为
重复出现（burst 里的 is_recurrence=True），不是新故事——调用方
（collector 发提醒、event_ranking 排名）据此不发提醒、不给爆发加分
和新鲜度分。判断分两步：(1) 名字比较先剥掉日期片段再归一化
（DD.MM.YYYY、YYYY-MM-DD、DD/MM/YYYY、M月D日——每天换日期的直播
标题才能认出来；非日期的数字保留，iPhone 17 与 iPhone 16 仍是两个
故事）；(2) “出现过”按老事件最近一次快照算，不按 first_seen——老
事件首次出现即使超出窗口，只要昨晚还有快照，同名新事件仍是重复出现。
不做标题黑名单。代价：同一标题的真新事件在窗口期内也会被压住；
窗口就是调这个的旋钮。

跨轮次重复触发（2026-11-16 生产实测，09:15 与 09:31 两轮逐字节
重复）：detect_bursts 读库里所有事件，一条快照的事件只要过门槛就
触发 new_multi_platform——不管那条快照是哪一轮的。聚类不稳定时，
事件某轮不再出现在聚类输出里就永远只有一条快照，于是每轮都重新
触发；is_recurrence 要求一个不同 event_id 的老事件，永远拦不住
原事件本身。修法：only_fresh=True 时，只有最新快照是本轮新产生
的事件才参与检测（快照时间与本轮 now 的差在容忍内，见
FRESH_SNAPSHOT_TOLERANCE）；采集器（collector）传这个参数，
面板/独立调用不传、保持原行为。

注意：只有一条快照的事件，规则 (a)(b) 无从比较，不触发；只有规则 (c)
会触发。没有快照的事件不参与检测。
纯标准库，不联网。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trend_radar.scoring import parse_dt

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "growth_multiplier": 2.0,
    "growth_min_delta": 100.0,
    "score_multiplier": 2.0,
    "score_min_delta": 5.0,
    "new_event_min_platforms": 2,
    # 最低质量门槛（只用于 new_multi_platform，见模块 docstring）：
    # 条数和总分满足其一即可。2026-11-16：条数腿 10 -> 20（11 条/
    # 2.94 分的小丑短视频靠旧门槛漏过去了）。初始假设，未校准。
    "alert_min_items": 20,
    "alert_min_total_score": 5.0,
    # 重复出现窗口（小时）：同名多平台事件在窗口内出现过，再出现
    # 就算重复出现。初始假设，未校准。
    "recurrence_window_hours": 24.0,
}


# 最新快照“是本轮新产生的”容忍窗口（only_fresh=True 时用）：
# 采集器把同一 round_now 传给 persist_events 和 detect_bursts，
# 理论上快照时间 == round_now；留 5 分钟是防两者用的 now 有几秒
# 偏差。轮次间隔 15 分钟，上一轮的快照（15 分钟前）一定落在窗口外，
# 不会误判为新鲜。
FRESH_SNAPSHOT_TOLERANCE = timedelta(minutes=5)


def load_burst_config() -> dict:
    """读 config.yaml 的 burst 一节；读不到就用 DEFAULTS。"""
    thresholds = dict(DEFAULTS)
    try:
        import yaml  # type: ignore

        cfg = yaml.safe_load((BASE_DIR / "config.yaml").read_text(encoding="utf-8")) or {}
        for key in thresholds:
            if key in (cfg.get("burst") or {}):
                thresholds[key] = cfg["burst"][key]
    except Exception:
        pass
    return thresholds


def _normalize_event_name(name: str | None) -> str:
    """事件名归一化：小写、去首尾空白、空白折叠。

    和 trend_radar/alerts.py::_normalize_raw_name 同一个归一化
    （去重和重复出现判断都要“原名相同”，不能让翻译状态影响结果）。
    """
    return " ".join(str(name or "").lower().split())


# 日期片段（只剥 4 位年份的完整日期，避免把版本号 1.2.3 这类
# 当成日期剥掉；裸数字如 iPhone 17 里的 17 从来不碰）。
_DATE_PATTERNS = (
    r"\d{4}年\d{1,2}月\d{1,2}日",
    r"\d{1,2}月\d{1,2}日",
    r"\d{4}[./-]\d{1,2}[./-]\d{1,2}",
    r"\d{1,2}[./-]\d{1,2}[./-]\d{4}",
)
_DATE_RE = re.compile("|".join(f"(?:{p})" for p in _DATE_PATTERNS))


def _strip_date_fragments(name: str) -> str:
    """去掉标题里的日期片段（仅 is_recurrence 用）。

    每天换日期的直播标题（15.11.2026 -> 16.11.2026）剥掉后
    才能认出是同一个标题。只认带分隔符的完整日期；版本号
    （1.2.3）和裸数字（iPhone 17）不受影响。
    """
    return _DATE_RE.sub("", name)


def _normalize_event_name_for_recurrence(name: str | None) -> str:
    """重复出现比较用的归一化：先剥日期，再小写、空白折叠。

    注意和 _normalize_event_name 的区别：去重（alerts.py）和
    event_id 生成都不剥日期，只有 is_recurrence 剥——标题里
    的日期本来就是每天变的，不剥就永远匹配不上。
    """
    return " ".join(_strip_date_fragments(str(name or "")).lower().split())


def _as_aware(dt: datetime | None) -> datetime | None:
    """解析出的时间统一成带时区的 UTC 时间；解析失败返回 None。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_recurrence(
    conn: sqlite3.Connection,
    event_id: str,
    name: str | None,
    first_seen: str | datetime | None,
    window_hours: float = 24.0,
    now: datetime | str | None = None,
) -> bool:
    """判断一个事件是不是“重复出现”。

    库里已有一个更早的同名多平台事件时返回 True：更早 =
    first_seen 严格早于本事件的 first_seen；同名 = 剥掉日期片段后
    归一化相同（2026-11-16：直播标题每天换日期，不剥就匹配不上）；
    多平台 = 那个事件最新快照的 platform_count >= 2；最近一次
    快照落在最近 window_hours 小时内（相对 now，2026-11-16 改：
    以前按老事件 first_seen 算，首次出现超过窗口的老事件即使昨晚
    还有快照也会漏过去）。
    任一条件不满足、名字为空、window_hours <= 0 时返回 False。
    first_seen 解析失败时以 now 为准（本事件视为刚出现）。

    这是 collector（发提醒）和 Dashboard Top 10（排名）共用的
    唯一判断入口：detect_bursts 调它给 burst 打 is_recurrence
    标记，两边都读这个标记，不各自再查一次库。
    """
    if window_hours is None or float(window_hours) <= 0:
        return False
    normalized = _normalize_event_name_for_recurrence(name)
    if not normalized:
        return False
    if now is None:
        now_dt = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        now_dt = _as_aware(now) or datetime.now(timezone.utc)
    else:
        now_dt = _as_aware(parse_dt(now)) or datetime.now(timezone.utc)
    ref_dt = _as_aware(parse_dt(first_seen)) or now_dt
    window_start = now_dt - timedelta(hours=float(window_hours))
    rows = conn.execute(
        """
        SELECT e.event_id, e.name, e.first_seen,
               (SELECT s.platform_count FROM event_snapshots s
                WHERE s.event_id = e.event_id
                ORDER BY s.collected_at DESC, s.id DESC LIMIT 1) AS pc,
               (SELECT s.collected_at FROM event_snapshots s
                WHERE s.event_id = e.event_id
                ORDER BY s.collected_at DESC, s.id DESC LIMIT 1) AS last_seen
        FROM events e
        WHERE e.event_id != ?
        """,
        (event_id,),
    ).fetchall()
    for row in rows:
        other_first = _as_aware(parse_dt(row["first_seen"]))
        if other_first is None:
            continue
        # 老事件必须比本事件早出现（同轮拆出的孪生事件不算重复出现，
        # 那种情况由 alerts.py 的同名去重处理）。
        if not (other_first < ref_dt):
            continue
        # “出现过”按老事件最近一次快照算：昨晚还有快照的同名事件，
        # 即使首次出现已超出窗口，也算重复出现。
        last_seen = _as_aware(parse_dt(row["last_seen"]))
        if last_seen is None:
            continue
        if not (window_start <= last_seen <= now_dt):
            continue
        pc = row["pc"]
        if pc is None or int(pc) < 2:
            continue
        if _normalize_event_name_for_recurrence(row["name"]) == normalized:
            return True
    return False


def _snapshot_values(row: sqlite3.Row) -> dict:
    return {
        "collected_at": row["collected_at"],
        "item_count": int(row["item_count"]),
        "platform_count": int(row["platform_count"]),
        "total_score": float(row["total_score"]),
        "max_score": float(row["max_score"]),
        "max_growth": float(row["max_growth"]),
        "platforms": row["platforms"] or "",
    }


def detect_bursts(
    conn: sqlite3.Connection,
    thresholds: dict | None = None,
    now: datetime | str | None = None,
    only_fresh: bool = False,
) -> list[dict]:
    """检测所有事件的爆发，返回 burst 列表（按最新 total_score 降序）。

    每个 burst：event_id、name、name_zh（有中文名时才有，没有为 None，
    显示时回退到 name）、reasons（命中的规则名）、is_recurrence
    （new_multi_platform 里同名事件在窗口内已跨平台出现过，见
    is_recurrence；其他规则恒为 False）、current（最新快照
    值）、previous（上一轮快照值，规则 (c) 时为 None）、detected_at。
    没有任何事件爆发时返回 []。

    规则 (c) 有最低质量门槛：只有一条快照的新事件，条数
    >= alert_min_items 或总分 >= alert_min_total_score 才算爆发。

    only_fresh=True 时，只检测最新快照是本轮新产生的事件（最新快照
    的 collected_at 与 now 的差在 FRESH_SNAPSHOT_TOLERANCE 内）；
    快照陈旧的事件（上一轮及更早，没再进聚类输出）直接跳过——
    否则它每轮都会重新触发（2026-11-16 生产实测：09:15 与 09:31
    两轮逐字节重复，同一批 event_id 反复报警）。采集器传
    only_fresh=True（并把同一 round_now 传给 persist_events 和这里）；
    面板/独立调用不传，默认 False、保持原行为（面板打开时看的是
    最近一轮的爆发，不按轮次过滤）。
    """
    cfg = dict(DEFAULTS)
    if thresholds:
        cfg.update(thresholds)
    if now is None:
        now_dt = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        now_dt = _as_aware(now) or datetime.now(timezone.utc)
    else:
        now_dt = _as_aware(parse_dt(now)) or datetime.now(timezone.utc)
    detected_at = now_dt.isoformat()

    bursts: list[dict] = []
    # name_zh 列是 2026-11-15 翻译功能加的；db.connect() 会给老库补列，
    # 但 detect_bursts 也可能被直接连在老库上的调用方使用，先查有没有该列，
    # 没有就只读 name（burst 里 name_zh 为 None，显示回退到原标题）。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "name_zh" in cols:
        event_rows = conn.execute(
            "SELECT event_id, name, name_zh, first_seen FROM events"
        ).fetchall()
    else:
        event_rows = conn.execute(
            "SELECT event_id, name, first_seen FROM events"
        ).fetchall()
    for event_row in event_rows:
        event_id = event_row["event_id"]
        name_zh = None
        if "name_zh" in cols:
            name_zh = (event_row["name_zh"] or "").strip() or None
        snaps = conn.execute(
            """
            SELECT * FROM event_snapshots
            WHERE event_id = ? ORDER BY collected_at DESC, id DESC LIMIT 2
            """,
            (event_id,),
        ).fetchall()
        if not snaps:
            continue
        current = _snapshot_values(snaps[0])
        if only_fresh:
            # 跨轮次重复触发的根治：最新快照不是本轮新产生的，说明
            # 本轮没有关于这个事件的新证据——不管它有几条快照、之前
            # 有没有触发过规则，本轮都不算爆发。只影响 only_fresh=True
            # 的调用方（采集器）；默认 False 时原行为不变。
            snap_dt = _as_aware(parse_dt(current["collected_at"]))
            if snap_dt is None or snap_dt < now_dt - FRESH_SNAPSHOT_TOLERANCE:
                continue
        reasons: list[str] = []
        previous = None
        if len(snaps) == 1:
            if current["platform_count"] >= int(cfg["new_event_min_platforms"]):
                # 最低质量门槛（2026-11-15，2026-11-16 条数腿抬到 20）：
                # 条数和总分满足其一才算爆发——11 条/2.94 分的小丑
                # 短视频这类“跨平台但没分量”的事件不再触发。
                # growth_spike / score_surge 自带绝对增量门槛，不走这条。
                if (
                    current["item_count"] >= int(cfg["alert_min_items"])
                    or current["total_score"] >= float(cfg["alert_min_total_score"])
                ):
                    reasons.append("new_multi_platform")
        else:
            previous = _snapshot_values(snaps[1])
            growth_prev = previous["max_growth"]
            growth_now = current["max_growth"]
            if growth_now >= growth_prev * float(cfg["growth_multiplier"]) and (
                growth_now - growth_prev
            ) >= float(cfg["growth_min_delta"]):
                reasons.append("growth_spike")
            score_prev = previous["total_score"]
            score_now = current["total_score"]
            if score_now >= score_prev * float(cfg["score_multiplier"]) and (
                score_now - score_prev
            ) >= float(cfg["score_min_delta"]):
                reasons.append("score_surge")
        if reasons:
            # 重复出现只判 new_multi_platform：同名事件在窗口内已
            # 跨平台出现过，再出现就不是新故事（调用方不发提醒、排名
            # 里不给爆发加分和新鲜度分）。growth_spike / score_surge
            # 是老事件的变化信号，不按重复出现处理。
            is_rec = False
            if "new_multi_platform" in reasons:
                is_rec = is_recurrence(
                    conn,
                    event_id,
                    event_row["name"],
                    event_row["first_seen"],
                    window_hours=float(cfg["recurrence_window_hours"]),
                    now=now_dt,
                )
            bursts.append(
                {
                    "event_id": event_id,
                    "name": event_row["name"],
                    "name_zh": name_zh,
                    "reasons": reasons,
                    "is_recurrence": is_rec,
                    "current": current,
                    "previous": previous,
                    "detected_at": detected_at,
                }
            )
    bursts.sort(key=lambda b: b["current"]["total_score"], reverse=True)
    return bursts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P2 准备：检测事件爆发。")
    parser.add_argument("--db", default=str(BASE_DIR / "data" / "trends.sqlite3"))
    parser.add_argument("--json", action="store_true", help="print bursts as JSON")
    args = parser.parse_args(argv)
    path = Path(args.db)
    if not path.exists():
        print("数据库不存在：没有可检测的事件快照。")
        return 0
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        bursts = detect_bursts(conn, thresholds=load_burst_config())
    finally:
        conn.close()
    if args.json:
        print(json.dumps(bursts, ensure_ascii=False, indent=2))
    elif not bursts:
        print("暂无爆发事件。")
    else:
        print(f"爆发事件 {len(bursts)} 个：")
        for b in bursts:
            print(
                f"- [{', '.join(b['reasons'])}] {b['name']} "
                f"(event_id={b['event_id']}, "
                f"增长 {b['previous']['max_growth'] if b['previous'] else '-'} -> "
                f"{b['current']['max_growth']}/小时, "
                f"总分 {b['previous']['total_score'] if b['previous'] else '-'} -> "
                f"{b['current']['total_score']})"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
