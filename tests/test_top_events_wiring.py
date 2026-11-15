"""load_top_events 接线测试：用真实 db.py 建表、写行，再跑聚类 + 排名。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod
from trend_radar.dashboard_data import load_top_events


def _item(platform, title, url):
    return {
        "platform": platform,
        "title": title,
        "url": url,
        "published_at": "2026-11-15T11:00:00+00:00",
        "first_seen": "2026-11-15T11:00:00+00:00",
        "engagement": 0,
        "source": "test",
        "language": "en",
        "summary_zh": "",
    }


def _seed(conn):
    """两个事件：一个跨 rss + youtube（标题重合，会聚成一个事件），
    一个只有 rss。跨平台事件增长更高、分数更高，应排第一。"""
    dbmod.upsert_item(
        conn,
        _item("rss", "Apple unveils new iPhone chip", "https://example.com/a1"),
        {"score": 0.6, "growth": 0, "acceleration": 0, "age_hours": 1},
    )
    dbmod.upsert_item(
        conn,
        _item("youtube", "Apple unveils new iPhone chip Pro", "https://example.com/a2"),
        {"score": 0.8, "growth": 500, "acceleration": 0, "age_hours": 1},
    )
    dbmod.upsert_item(
        conn,
        _item("rss", "Heat pumps how much heat does your home need", "https://example.com/b1"),
        {"score": 0.4, "growth": 0, "acceleration": 0, "age_hours": 1},
    )
    conn.commit()


class TestLoadTopEvents(unittest.TestCase):
    def test_ranked_list(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            conn = dbmod.connect(db_path)
            _seed(conn)
            conn.close()
            top = load_top_events(db_path)
            self.assertEqual(len(top), 2)
            first, second = top
            # 跨平台 + 增长最高的事件排第一。
            self.assertEqual(first["platform_count"], 2)
            self.assertIn("Apple", first["name"])
            self.assertTrue(0.0 <= first["rank_score"] <= 100.0)
            self.assertGreater(first["rank_score"], second["rank_score"])
            self.assertTrue(first["rank_reasons"])
            self.assertIn("2 个平台", first["rank_reasons"])
            self.assertIn("增长最快", first["rank_reasons"])
            for ev in top:
                self.assertIn("rank_score", ev)
                self.assertIn("rank_reasons", ev)

    def test_limit(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            conn = dbmod.connect(db_path)
            _seed(conn)
            conn.close()
            self.assertEqual(len(load_top_events(db_path, limit=1)), 1)

    def test_empty_db_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            conn = dbmod.connect(db_path)
            conn.close()
            self.assertEqual(load_top_events(db_path), [])

    def test_missing_db_returns_empty(self):
        missing = Path(tempfile.gettempdir()) / "does-not-exist-top-events.sqlite3"
        if missing.exists():
            missing.unlink()
        self.assertEqual(load_top_events(missing), [])


if __name__ == "__main__":
    unittest.main()
