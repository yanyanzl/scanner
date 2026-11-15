"""P2 准备测试：事件持久 ID + persist_events。

只用临时数据库，不碰真实 data/trends.sqlite3。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod
from trend_radar.clustering import canonical_event_id, cluster_from_db, cluster_items, persist_events


def _item(title, platform, url, score=0.5, growth=0.0):
    return {
        "title": title,
        "platform": platform,
        "url": url,
        "score": score,
        "growth": growth,
        "first_seen": "2026-11-14T12:00:00+00:00",
        "published_at": "2026-11-14T12:00:00+00:00",
    }


def _apple_items():
    return [
        _item("Apple unveils new iPhone with AI chip", "rss", "https://a.example/1", 0.8, 10.0),
        _item("Apple launches new iPhone featuring AI chip", "gdelt", "https://b.example/2", 0.7, 5.0),
        _item("New iPhone with AI chip unveiled by Apple", "reddit", "https://c.example/3", 0.6, 2.0),
    ]


class TestEventId(unittest.TestCase):
    def test_same_event_keeps_id_across_runs(self):
        """同一事件两轮聚类（第二轮分数略变）-> 同一个 event_id。"""
        first = cluster_items(_apple_items())
        second_items = _apple_items()
        second_items[0]["score"] = 0.9  # 分数变化：代表标题不变，核心词不变
        second = cluster_items(second_items)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0]["event_id"], second[0]["event_id"])
        self.assertTrue(first[0]["event_id"].startswith("evt_"))

    def test_event_id_deterministic(self):
        self.assertEqual(
            canonical_event_id(_apple_items()), canonical_event_id(_apple_items())
        )

    def test_short_identical_titles_get_different_ids(self):
        """同名不同期的短标题（Tech Now 两期）-> 两个事件、两个 id。"""
        items = [
            _item("Tech Now", "rss", "https://bbc.example/tech-now-ep1", 0.5),
            _item("Tech Now", "rss", "https://bbc.example/tech-now-ep2", 0.5),
        ]
        events = cluster_items(items)
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0]["event_id"], events[1]["event_id"])

    def test_unrelated_events_get_different_ids(self):
        items = [
            _item("Local football team wins championship final", "rss", "https://a.example/1"),
            _item("Scientists discover new deep sea species", "gdelt", "https://b.example/2"),
        ]
        events = cluster_items(items)
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0]["event_id"], events[1]["event_id"])


class TestPersistEvents(unittest.TestCase):
    def test_first_seen_never_moves_backwards_and_snapshots_accumulate(self):
        """两轮 persist：first_seen 保持最早值，last_seen 更新，快照各追加一行。"""
        with tempfile.TemporaryDirectory() as d:
            conn = dbmod.connect(str(Path(d) / "test.sqlite3"))
            try:
                events = cluster_items(_apple_items())
                self.assertEqual(len(events), 1)
                t1 = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
                t2 = datetime(2026, 11, 15, 10, 30, tzinfo=timezone.utc)
                persist_events(conn, events, now=t1)
                # 第二轮：代表标题的 first_seen 被人为写晚，first_seen 不能跟着变晚
                later = [dict(events[0], first_seen="2026-11-15T10:30:00+00:00")]
                persist_events(conn, later, now=t2)
                row = conn.execute(
                    "SELECT * FROM events WHERE event_id = ?", (events[0]["event_id"],)
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["first_seen"], "2026-11-14T12:00:00+00:00")
                self.assertEqual(row["last_seen"], t2.isoformat())
                count = conn.execute(
                    "SELECT COUNT(*) FROM event_snapshots WHERE event_id = ?",
                    (events[0]["event_id"],),
                ).fetchone()[0]
                self.assertEqual(count, 2)
            finally:
                conn.close()

    def test_persist_empty_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            conn = dbmod.connect(str(Path(d) / "test.sqlite3"))
            try:
                persist_events(conn, [])
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0
                )
            finally:
                conn.close()

    def test_cluster_from_db_includes_stable_event_id(self):
        """真实 db 路径：cluster_from_db 的结果带 event_id，两次调用一致。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "test.sqlite3")
            conn = dbmod.connect(db_path)
            try:
                for it in _apple_items():
                    item_id = dbmod.upsert_item(
                        conn, it, {"score": it["score"], "growth": it["growth"], "acceleration": 0, "age_hours": 1}
                    )
                    dbmod.record_snapshot(conn, item_id, it["score"] * 100, it["score"])
            finally:
                conn.close()
            first = cluster_from_db(db_path, hours=24 * 365)
            second = cluster_from_db(db_path, hours=24 * 365)
            self.assertEqual(len(first), 1)
            self.assertIn("event_id", first[0])
            self.assertEqual(first[0]["event_id"], second[0]["event_id"])


if __name__ == "__main__":
    unittest.main()
