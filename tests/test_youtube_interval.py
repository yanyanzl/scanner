"""YouTube 单独间隔测试：每 30 分钟采集一次，且增长按 0.5 小时算。

背景：2026-11-14 发现 YouTube Data API v3 搜索在 2026 年 6 月后有约
100 次/天的单独配额，而原配置每 15 分钟搜 2 个词 = 192 次/天，
接通当天即超限。2026-11-15 经 Google 官方文档核实：自 2026-06-01 起
search.list 单独成桶，上限 100 次/天、每次 1 单位；其他接口共用
10,000 units/天。修复：config.yaml 里 youtube.interval_minutes=30，
每天 96 次搜索（留 4 次缓冲），collector 按该间隔跳过未到时间的轮次；
YouTube 条目打分时也用 0.5 小时作间隔，否则增长会被高估 2 倍。
"""
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import collector


def _youtube_item(engagement: float = 1100.0):
    return {
        "platform": "youtube",
        "title": "Breaking news test video",
        "url": "https://www.youtube.com/watch?v=test123",
        "published_at": "2026-11-15T06:00:00+00:00",
        "first_seen": "2026-11-15T07:00:00+00:00",
        "engagement": engagement,
        "views": 1000.0,
        "likes": 80.0,
        "comments": 20.0,
        "source": "Test Channel",
    }


def _config():
    return {
        "scan_interval_minutes": 15,
        "db_path": "data/trends.sqlite3",
        "sources": {
            "rss": {"enabled": False},
            "gdelt": {"enabled": False},
            "reddit": {"enabled": False},
            "youtube": {"enabled": True, "queries": ["technology"], "interval_minutes": 30},
            "x": {"enabled": False},
            "tiktok": {"enabled": False},
        },
    }


class TestYoutubeDue(unittest.TestCase):
    def test_missing_state_file_is_due(self):
        self.assertTrue(collector._youtube_due(Path("/tmp/does-not-exist-youtube-state"), 60))

    def test_recent_run_is_not_due(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / ".youtube_last_run"
            now = datetime.now(timezone.utc)
            state.write_text((now - timedelta(minutes=10)).isoformat(), encoding="utf-8")
            self.assertFalse(collector._youtube_due(state, 60, now=now))

    def test_old_run_is_due(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / ".youtube_last_run"
            now = datetime.now(timezone.utc)
            state.write_text((now - timedelta(minutes=61)).isoformat(), encoding="utf-8")
            self.assertTrue(collector._youtube_due(state, 60, now=now))


class TestYoutubeIntervalEndToEnd(unittest.TestCase):
    def test_half_hour_gate_and_baseline_then_real_growth(self):
        """第一轮采 YouTube：首次见到作基线，growth 0；紧接着第二轮跳过；
        30 分钟后再采，growth 只算真实增量（按 0.5 小时）。

        注意：2026-11-15 之前这里断言第一轮 growth 为 2200（1100 / 0.5），
        那是首次见到虚高 bug 的表现——历史总量被当成一轮增量。
        修正后第一轮是基线（growth 0），第二轮真实增量 550 才按
        550 / 0.5 = 1100 算；若错用 0.25 小时则会是 2200。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            fetch = mock.Mock(
                side_effect=[[_youtube_item()], [_youtube_item(1650.0)]]
            )
            with (
                mock.patch.object(collector, "BASE_DIR", tmp),
                mock.patch.object(collector.youtube, "fetch", fetch),
                mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": "test-key"}),
            ):
                first = collector.collect_once(_config())
                second = collector.collect_once(_config())
                # 把上次采集时间拨回 31 分钟前，模拟 30 分钟间隔已满。
                state = tmp / "data" / ".youtube_last_run"
                state.write_text(
                    (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(),
                    encoding="utf-8",
                )
                third = collector.collect_once(_config())
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(first["youtube"], 1)
            self.assertEqual(second["youtube"], 0)
            self.assertEqual(third["youtube"], 1)
            self.assertTrue((tmp / "data" / ".youtube_last_run").exists())
            conn = sqlite3.connect(str(tmp / "data" / "trends.sqlite3"))
            try:
                row = conn.execute(
                    "SELECT growth FROM items WHERE platform = 'youtube'"
                ).fetchone()
                snaps = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            finally:
                conn.close()
            self.assertIsNotNone(row)
            # 两轮都采到了，快照 2 行；第二轮真实增量 550 / 0.5 小时 = 1100。
            self.assertEqual(snaps, 2)
            self.assertEqual(row[0], 1100.0)


if __name__ == "__main__":
    unittest.main()
