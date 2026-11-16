"""dashboard_data 加载器测试：用真实 db.py 建表、写行，不 import app.py。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod
from trend_radar.dashboard_data import load_bursts, load_fastest, load_history, load_items


def _item(platform, title, url, engagement=0.0):
    return {
        "platform": platform,
        "title": title,
        "url": url,
        "published_at": "2026-11-15T06:00:00+00:00",
        "first_seen": "2026-11-15T07:00:00+00:00",
        "engagement": engagement,
        "source": "test",
        "language": "en",
        "summary_zh": "",
    }


def _seed(conn, items):
    ids = {}
    for item, score in items:
        item_id = dbmod.upsert_item(
            conn,
            item,
            {"score": score, "growth": item.get("engagement", 0), "acceleration": 0, "age_hours": 1},
        )
        ids[item["url"]] = item_id
    return ids


def _snapshot(conn, item_id, engagement, collected_at):
    conn.execute(
        "INSERT INTO snapshots (item_id, engagement, score, collected_at) VALUES (?, ?, ?, ?)",
        (item_id, engagement, 0, collected_at),
    )
    conn.commit()


class TestLoadersMissingAndEmpty(unittest.TestCase):
    def test_missing_db_returns_empty(self):
        missing = Path(tempfile.gettempdir()) / "does-not-exist-dashboard.sqlite3"
        self.assertEqual(load_items(missing), [])
        self.assertEqual(load_fastest(missing), [])
        self.assertEqual(load_history(missing), ([], {}))

    def test_empty_db_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            dbmod.connect(db_path).close()
            self.assertEqual(load_items(db_path), [])
            self.assertEqual(load_fastest(db_path), [])
            self.assertEqual(load_history(db_path), ([], {}))

    def test_db_without_tables_returns_empty(self):
        # 空文件：sqlite 能打开但没有 items 表，不应抛异常。
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            db_path.touch()
            self.assertEqual(load_items(db_path), [])
            self.assertEqual(load_history(db_path), ([], {}))


class TestLoadItems(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "trends.sqlite3"
        self.conn = dbmod.connect(self.db_path)
        _seed(
            self.conn,
            [
                (_item("rss", "Low score", "https://example.com/low"), 1.0),
                (_item("youtube", "High score", "https://example.com/high", 500), 9.0),
                (_item("rss", "Mid score", "https://example.com/mid"), 5.0),
            ],
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_all_sorted_by_score_desc(self):
        titles = [it["title"] for it in load_items(self.db_path)]
        self.assertEqual(titles, ["High score", "Mid score", "Low score"])

    def test_platform_filter(self):
        titles = [it["title"] for it in load_items(self.db_path, platform="rss")]
        self.assertEqual(titles, ["Mid score", "Low score"])

    def test_limit(self):
        self.assertEqual(len(load_items(self.db_path, limit=2)), 2)

    def test_fastest_sorted_by_growth(self):
        fastest = load_fastest(self.db_path)
        self.assertEqual(fastest[0]["title"], "High score")


class TestLoadItemsRecurrence(unittest.TestCase):
    """实时热点榜去掉定时重复播报（任务 3）。

    场景复现 2026-11-16 20:28 截图：热点榜前 3 里有 2 个是定时重复
    播报（每小时直播、2PM 头条）。两条同标题直播视频（新 URL、新
    视频）与库里已有的老事件同名（去掉日期后相同），且老事件最近
    一次快照是跨平台的 → 当前事件是 recurrence，其成员不进榜单；
    真新闻（克什米尔地震）不受影响。
    """

    LIVE_TITLE = "今日突发新闻直播 | 本时头条新闻 | 本小时头条 | 15.11.2026"

    def setUp(self):
        from datetime import datetime, timedelta, timezone

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "trends.sqlite3"
        self.conn = dbmod.connect(self.db_path)
        _seed(
            self.conn,
            [
                (_item("youtube", self.LIVE_TITLE, "https://youtube.com/watch?v=AAA"), 9.0),
                (_item("youtube", self.LIVE_TITLE, "https://youtube.com/watch?v=BBB"), 8.0),
                (_item("rss", "克什米尔地震", "https://example.com/quake"), 5.0),
            ],
        )
        # 老事件（双胞胎）：同名、首次出现比当前事件早、最近快照跨平台。
        now = datetime.now(timezone.utc)
        last_seen = (now - timedelta(hours=1)).isoformat()
        self.conn.execute(
            "INSERT INTO events (event_id, name, first_seen, last_seen,"
            " platform_count, item_count, total_score, max_score, max_growth)"
            " VALUES ('evt_old', ?, '2026-11-14T07:00:00+00:00', ?, 2, 10, 5.0, 0.5, 100.0)",
            (self.LIVE_TITLE, last_seen),
        )
        self.conn.execute(
            "INSERT INTO event_snapshots (event_id, collected_at, item_count,"
            " platform_count, total_score, max_score, max_growth, platforms)"
            " VALUES ('evt_old', ?, 10, 2, 5.0, 0.5, 100.0, 'rss,youtube')",
            (last_seen,),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_recurrence_items_excluded(self):
        titles = [
            it["title"]
            for it in load_items(self.db_path, exclude_recurrence=True)
        ]
        self.assertEqual(titles, ["克什米尔地震"])

    def test_default_keeps_everything(self):
        titles = [it["title"] for it in load_items(self.db_path)]
        self.assertEqual(
            titles, [self.LIVE_TITLE, self.LIVE_TITLE, "克什米尔地震"]
        )

    def test_limit_still_respected(self):
        self.assertEqual(
            len(load_items(self.db_path, limit=1, exclude_recurrence=True)), 1
        )


class TestLoadHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "trends.sqlite3"
        self.conn = dbmod.connect(self.db_path)
        ids = _seed(
            self.conn,
            [
                (_item("youtube", "Video A", "https://example.com/a", 100), 5.0),
                (_item("youtube", "Video B", "https://example.com/b", 50), 4.0),
            ],
        )
        self.a, self.b = ids["https://example.com/a"], ids["https://example.com/b"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_history_ordering_and_alignment(self):
        t1, t2, t3 = (
            "2026-11-15T07:00:00+00:00",
            "2026-11-15T07:30:00+00:00",
            "2026-11-15T08:00:00+00:00",
        )
        _snapshot(self.conn, self.a, 100, t1)
        _snapshot(self.conn, self.a, 300, t2)
        _snapshot(self.conn, self.a, 900, t3)
        _snapshot(self.conn, self.b, 50, t1)
        _snapshot(self.conn, self.b, 60, t3)  # t2 缺失，应为 None
        times, series = load_history(self.db_path)
        self.assertEqual(times, [t1, t2, t3])
        self.assertEqual(series["Video A"], [100.0, 300.0, 900.0])
        self.assertEqual(series["Video B"], [50.0, None, 60.0])

    def test_all_zero_engagement_returns_empty(self):
        _snapshot(self.conn, self.a, 0, "2026-11-15T07:00:00+00:00")
        _snapshot(self.conn, self.b, 0, "2026-11-15T07:00:00+00:00")
        self.assertEqual(load_history(self.db_path), ([], {}))

    def test_limit_caps_series(self):
        for i, item_id in enumerate((self.a, self.b)):
            _snapshot(self.conn, item_id, 10 + i, "2026-11-15T07:00:00+00:00")
        _, series = load_history(self.db_path, limit=1)
        self.assertEqual(len(series), 1)


class TestLoadBursts(unittest.TestCase):
    def test_missing_and_empty_db_returns_empty(self):
        missing = Path(tempfile.gettempdir()) / "does-not-exist-bursts.sqlite3"
        self.assertEqual(load_bursts(missing), [])
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            dbmod.connect(db_path).close()
            self.assertEqual(load_bursts(db_path), [])

    def test_growth_spike_event_returned(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            conn = dbmod.connect(db_path)
            conn.execute(
                "INSERT INTO events (event_id, name, first_seen, last_seen,"
                " platform_count, item_count, total_score, max_score, max_growth)"
                " VALUES ('evt_test', 'Test Event', '2026-11-15T07:00:00+00:00',"
                " '2026-11-15T08:00:00+00:00', 1, 1, 10.0, 10.0, 500.0)"
            )
            for t, total, growth in (
                ("2026-11-15T07:00:00+00:00", 2.0, 100.0),
                ("2026-11-15T08:00:00+00:00", 10.0, 500.0),
            ):
                conn.execute(
                    "INSERT INTO event_snapshots (event_id, collected_at, item_count,"
                    " platform_count, total_score, max_score, max_growth, platforms)"
                    " VALUES ('evt_test', ?, 1, 1, ?, ?, ?, 'rss')",
                    (t, total, total, growth),
                )
            conn.commit()
            conn.close()
            bursts = load_bursts(db_path)
            self.assertEqual(len(bursts), 1)
            self.assertEqual(bursts[0]["event_id"], "evt_test")
            self.assertIn("growth_spike", bursts[0]["reasons"])
            self.assertIn("score_surge", bursts[0]["reasons"])


if __name__ == "__main__":
    unittest.main()
