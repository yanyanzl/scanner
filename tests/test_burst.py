"""P2 准备测试：爆发检测 detect_bursts。

只用临时数据库，不碰真实 data/trends.sqlite3。
快照通过 clustering.persist_events 的真实路径写入。
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trend_radar import db as dbmod
from trend_radar.burst import detect_bursts
from trend_radar.clustering import persist_events


def _event(event_id, name, max_growth, total_score, platforms=("rss",)):
    return {
        "event_id": event_id,
        "name": name,
        "first_seen": "2026-11-14T12:00:00+00:00",
        "platforms": list(platforms),
        "platform_count": len(platforms),
        "item_count": len(platforms),
        "total_score": total_score,
        "max_score": total_score,
        "max_growth": max_growth,
        "trend": "rising" if max_growth > 0 else "flat",
        "items": [],
    }


def _persist_two_rounds(conn, first, second, t1, t2):
    persist_events(conn, [first], now=t1)
    persist_events(conn, [second], now=t2)


class TestBurstDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbmod.connect(str(Path(self.tmp.name) / "test.sqlite3"))
        self.t1 = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
        self.t2 = datetime(2026, 11, 15, 10, 30, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_growth_spike_flagged(self):
        """max_growth 100 -> 500（2 倍且增量 400）-> growth_spike。"""
        _persist_two_rounds(
            self.conn,
            _event("evt_a", "Apple iPhone AI chip", 100.0, 2.0),
            _event("evt_a", "Apple iPhone AI chip", 500.0, 2.5),
            self.t1,
            self.t2,
        )
        bursts = detect_bursts(self.conn)
        self.assertEqual(len(bursts), 1)
        self.assertEqual(bursts[0]["event_id"], "evt_a")
        self.assertIn("growth_spike", bursts[0]["reasons"])
        self.assertEqual(bursts[0]["previous"]["max_growth"], 100.0)
        self.assertEqual(bursts[0]["current"]["max_growth"], 500.0)

    def test_score_surge_flagged(self):
        """total_score 2 -> 10（2 倍且增量 8）-> score_surge，增长不动不触发 growth。"""
        _persist_two_rounds(
            self.conn,
            _event("evt_b", "Chip shortage", 10.0, 2.0),
            _event("evt_b", "Chip shortage", 10.0, 10.0),
            self.t1,
            self.t2,
        )
        bursts = detect_bursts(self.conn)
        self.assertEqual(len(bursts), 1)
        self.assertIn("score_surge", bursts[0]["reasons"])
        self.assertNotIn("growth_spike", bursts[0]["reasons"])

    def test_flat_history_no_burst(self):
        """增长 100 -> 110、总分微增 -> 不触发任何规则。"""
        _persist_two_rounds(
            self.conn,
            _event("evt_c", "Local football final", 100.0, 2.0),
            _event("evt_c", "Local football final", 110.0, 2.1),
            self.t1,
            self.t2,
        )
        self.assertEqual(detect_bursts(self.conn), [])

    def test_single_snapshot_single_platform_no_burst(self):
        """只有一条快照且单平台 -> 无可比较、无爆发。"""
        persist_events(
            self.conn, [_event("evt_d", "Quiet science news", 5.0, 1.0)], now=self.t1
        )
        self.assertEqual(detect_bursts(self.conn), [])

    def test_new_multi_platform_event_flagged(self):
        """首次出现就跨 2 个平台 -> new_multi_platform。"""
        persist_events(
            self.conn,
            [_event("evt_e", "Breaking tech news", 50.0, 3.0, platforms=("rss", "youtube"))],
            now=self.t1,
        )
        bursts = detect_bursts(self.conn)
        self.assertEqual(len(bursts), 1)
        self.assertIn("new_multi_platform", bursts[0]["reasons"])
        self.assertIsNone(bursts[0]["previous"])

    def test_small_base_growth_not_flagged(self):
        """小基数：0 -> 50 增长翻倍也不触发，因绝对增量 < 100。"""
        _persist_two_rounds(
            self.conn,
            _event("evt_f", "Tiny story", 0.0, 1.0),
            _event("evt_f", "Tiny story", 50.0, 1.0),
            self.t1,
            self.t2,
        )
        self.assertEqual(detect_bursts(self.conn), [])


if __name__ == "__main__":
    unittest.main()
