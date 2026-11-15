"""真实间隔测试：增速除以两次快照之间的真实间隔，而不是名义间隔。

背景（2026-11-15 实测）：Kanak 视频两次 YouTube 快照之间实际隔了
40 分钟（10:48 → 11:28），但增速一直除以名义上的 0.5 小时，
469.2 的增量被算成 938.4/小时，真实约 704/小时，虚高约 1.33 倍。
原因是定时器每 15 分钟一跳，YouTube 实际间隔会被量化成 30–45 分钟，
电脑休眠/重启后间隔更长。
规则：hours = max(名义间隔, 两次快照之间的真实间隔)。
间隔短于名义值时（手动连跑两轮）仍按名义值算，避免除以几秒钟
把增速吹上天。
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod
from trend_radar.collector import collect_once


def _item(platform, engagement):
    return {
        "platform": platform,
        "title": "Test Item",
        "url": f"https://example.com/{platform}-test",
        "published_at": "2026-11-15T06:00:00+00:00",
        "first_seen": "2026-11-15T07:00:00+00:00",
        "engagement": engagement,
        "views": 0,
        "likes": 0,
        "shares": 0,
        "comments": 0,
        "source": "test",
        "language": "",
        "summary_zh": "",
    }


def _config(db_path: Path, platform: str) -> dict:
    sources = {
        "rss": {"enabled": False},
        "gdelt": {"enabled": False},
        "reddit": {"enabled": False},
        "youtube": {"enabled": False},
        "x": {"enabled": False},
        "tiktok": {"enabled": False},
    }
    if platform == "youtube":
        sources["youtube"] = {"enabled": True, "queries": ["test"], "interval_minutes": 30}
    else:
        sources["rss"] = {"enabled": True, "feeds": ["https://example.com/feed"]}
    return {
        "scan_interval_minutes": 15,
        "db_path": str(db_path),
        "sources": sources,
        "burst": {},
    }


def _backdate_first_snapshot(db_path: Path, minutes: float) -> None:
    """把第一行快照的采集时间拨回 minutes 分钟前，模拟真实间隔。"""
    conn = dbmod.connect(db_path)
    item_id = conn.execute("SELECT id FROM items").fetchone()["id"]
    past = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    conn.execute(
        "UPDATE snapshots SET collected_at = ? WHERE item_id = ?",
        (past, int(item_id)),
    )
    conn.commit()
    conn.close()


def _growth_and_snap_times(db_path: Path):
    conn = dbmod.connect(db_path)
    growth = conn.execute("SELECT growth FROM items").fetchone()["growth"]
    times = [
        r["collected_at"]
        for r in conn.execute("SELECT collected_at FROM snapshots ORDER BY collected_at")
    ]
    conn.close()
    return growth, times


def _elapsed_hours(times) -> float:
    t0 = datetime.fromisoformat(times[0].replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(times[1].replace("Z", "+00:00"))
    return (t1 - t0).total_seconds() / 3600


class TestActualInterval(unittest.TestCase):
    def test_youtube_40_min_gap(self):
        """YouTube：名义 0.5 小时，真实间隔 40 分钟，增速按 40 分钟算。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            rounds = [[_item("youtube", 1100.0)], [_item("youtube", 1650.0)]]
            with mock.patch("trend_radar.collector.youtube.fetch", side_effect=rounds):
                collect_once(config=_config(db_path, "youtube"))
                _backdate_first_snapshot(db_path, 40)
                collect_once(config=_config(db_path, "youtube"))
            growth, times = _growth_and_snap_times(db_path)
            # 增量 550 / 真实间隔（40 分钟 ≈ 0.667 小时）≈ 825，不是 1100。
            expected = 550.0 / _elapsed_hours(times)
            self.assertAlmostEqual(growth, expected, places=1)
            self.assertLess(growth, 900.0)

    def test_short_gap_floored_at_nominal(self):
        """间隔短于名义值时仍按名义值算：5 分钟间隔也按 0.5 小时算。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            rounds = [[_item("youtube", 1100.0)], [_item("youtube", 1650.0)]]
            with mock.patch("trend_radar.collector.youtube.fetch", side_effect=rounds):
                collect_once(config=_config(db_path, "youtube"))
                _backdate_first_snapshot(db_path, 5)
                collect_once(config=_config(db_path, "youtube"))
            growth, _ = _growth_and_snap_times(db_path)
            self.assertEqual(growth, 1100.0)

    def test_rss_30_min_gap(self):
        """RSS：名义 0.25 小时，真实间隔 30 分钟，增速按 30 分钟算。"""
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "trends.sqlite3"
            rounds = [[_item("rss", 100.0)], [_item("rss", 200.0)]]
            with mock.patch("trend_radar.collector.rss.fetch", side_effect=rounds):
                collect_once(config=_config(db_path, "rss"))
                _backdate_first_snapshot(db_path, 30)
                collect_once(config=_config(db_path, "rss"))
            growth, times = _growth_and_snap_times(db_path)
            # 增量 100 / 真实间隔（30 分钟 = 0.5 小时）= 200，不是 400。
            expected = 100.0 / _elapsed_hours(times)
            self.assertAlmostEqual(growth, expected, places=1)


if __name__ == "__main__":
    unittest.main()
