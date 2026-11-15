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

注意：只有一条快照的事件，规则 (a)(b) 无从比较，不触发；只有规则 (c)
会触发。没有快照的事件不参与检测。
纯标准库，不联网。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "growth_multiplier": 2.0,
    "growth_min_delta": 100.0,
    "score_multiplier": 2.0,
    "score_min_delta": 5.0,
    "new_event_min_platforms": 2,
}


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
) -> list[dict]:
    """检测所有事件的爆发，返回 burst 列表（按最新 total_score 降序）。

    每个 burst：event_id、name、name_zh（有中文名时才有，没有为 None，
    显示时回退到 name）、reasons（命中的规则名）、current（最新快照
    值）、previous（上一轮快照值，规则 (c) 时为 None）、detected_at。
    没有任何事件爆发时返回 []。
    """
    cfg = dict(DEFAULTS)
    if thresholds:
        cfg.update(thresholds)
    if now is None:
        detected_at = datetime.now(timezone.utc).isoformat()
    elif isinstance(now, datetime):
        detected_at = now.isoformat()
    else:
        detected_at = now

    bursts: list[dict] = []
    # name_zh 列是 2026-11-15 翻译功能加的；db.connect() 会给老库补列，
    # 但 detect_bursts 也可能被直接连在老库上的调用方使用，先查有没有该列，
    # 没有就只读 name（burst 里 name_zh 为 None，显示回退到原标题）。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "name_zh" in cols:
        event_rows = conn.execute("SELECT event_id, name, name_zh FROM events").fetchall()
    else:
        event_rows = conn.execute("SELECT event_id, name FROM events").fetchall()
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
        reasons: list[str] = []
        previous = None
        if len(snaps) == 1:
            if current["platform_count"] >= int(cfg["new_event_min_platforms"]):
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
            bursts.append(
                {
                    "event_id": event_id,
                    "name": event_row["name"],
                    "name_zh": name_zh,
                    "reasons": reasons,
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
