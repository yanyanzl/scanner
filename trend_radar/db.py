"""SQLite 存储：内容 + 每次采集的快照（用于算增长速度和加速度）。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    published_at TEXT,
    first_seen TEXT,
    source TEXT,
    language TEXT,
    summary_zh TEXT,
    score REAL DEFAULT 0,
    growth REAL DEFAULT 0,
    acceleration REAL DEFAULT 0,
    age_hours REAL DEFAULT 48,
    collected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    engagement REAL NOT NULL,
    score REAL NOT NULL,
    collected_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES items (id)
);
CREATE INDEX IF NOT EXISTS idx_items_score ON items (score DESC);
CREATE INDEX IF NOT EXISTS idx_items_platform ON items (platform);
CREATE INDEX IF NOT EXISTS idx_snapshots_item ON snapshots (item_id, collected_at);
-- P2 准备：跨轮次事件身份 + 事件快照（2026-11-15）
-- events 存每个事件的最新状态；event_snapshots 每轮为每个事件追加一行，
-- 用于算事件级增长/加速度和爆发检测。CREATE TABLE IF NOT EXISTS，
-- 老数据库第一次连接时自动建表，不用手动迁移。
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    first_seen TEXT,
    last_seen TEXT,
    platform_count INTEGER DEFAULT 1,
    item_count INTEGER DEFAULT 1,
    total_score REAL DEFAULT 0,
    max_score REAL DEFAULT 0,
    max_growth REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS event_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    item_count INTEGER DEFAULT 1,
    platform_count INTEGER DEFAULT 1,
    total_score REAL DEFAULT 0,
    max_score REAL DEFAULT 0,
    max_growth REAL DEFAULT 0,
    platforms TEXT DEFAULT '',
    FOREIGN KEY (event_id) REFERENCES events (event_id)
);
CREATE INDEX IF NOT EXISTS idx_event_snapshots_event ON event_snapshots (event_id, collected_at);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_item(conn: sqlite3.Connection, item: dict, score_result: dict) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO items
            (platform, title, url, published_at, first_seen, source, language,
             summary_zh, score, growth, acceleration, age_hours, collected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title=excluded.title,
            score=excluded.score,
            growth=excluded.growth,
            acceleration=excluded.acceleration,
            age_hours=excluded.age_hours,
            collected_at=excluded.collected_at
        """,
        (
            item.get("platform", ""),
            item.get("title", ""),
            item.get("url", ""),
            item.get("published_at"),
            item.get("first_seen") or now,
            item.get("source", ""),
            item.get("language", ""),
            item.get("summary_zh", ""),
            score_result.get("score", 0),
            score_result.get("growth", 0),
            score_result.get("acceleration", 0),
            score_result.get("age_hours", 48),
            now,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM items WHERE url = ?", (item.get("url", ""),)).fetchone()
    return int(row["id"]) if row else int(cur.lastrowid)


def record_snapshot(conn: sqlite3.Connection, item_id: int, engagement: float, score: float) -> None:
    conn.execute(
        "INSERT INTO snapshots (item_id, engagement, score, collected_at) VALUES (?, ?, ?, ?)",
        (item_id, float(engagement or 0), float(score or 0), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def previous_engagement(conn: sqlite3.Connection, item_id: int) -> float:
    row = conn.execute(
        "SELECT engagement FROM snapshots WHERE item_id = ? ORDER BY collected_at DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    return float(row["engagement"]) if row else 0.0


def previous_snapshot(conn: sqlite3.Connection, item_id: int) -> tuple[float, str] | None:
    """上一轮快照的 (互动量, 采集时间)；从未有过快照时返回 None（不是 0）。

    区分“没有快照”和“快照互动量为 0”很重要：首次见到的内容
    必须按基线处理（见 collector.py），而不能把历史总量当成
    一轮的增量。previous_engagement() 保留作兼容，它无法区分这两种情况。
    返回采集时间是为了让 collector 按两次快照之间的真实间隔算增速，
    而不是一直除以名义间隔。
    """
    row = conn.execute(
        "SELECT engagement, collected_at FROM snapshots "
        "WHERE item_id = ? ORDER BY collected_at DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    return float(row["engagement"]), str(row["collected_at"])


def top_items(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM items ORDER BY score DESC, growth DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
