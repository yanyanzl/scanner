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
        """首次出现就跨 2 个平台 -> new_multi_platform（总分 6.0，过最低质量门槛）。"""
        persist_events(
            self.conn,
            [_event("evt_e", "Breaking tech news", 50.0, 6.0, platforms=("rss", "youtube"))],
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


def _event_with(event_id, name, first_seen, item_count, total_score, max_growth=0.0,
                platforms=("rss", "youtube")):
    """_event 的变体：条数/总分/首次出现时间可单独指定（门槛与重复出现测试用）。"""
    ev = _event(event_id, name, max_growth, total_score, platforms=platforms)
    ev["first_seen"] = first_seen
    ev["item_count"] = item_count
    return ev


LIVE_TITLE = "Today Breaking News LIVE | Top Headlines Of The Hour | 15.11.2026"
CLOWN_TITLE = "Clown's Artificial Intelligence#cosplay #shorts #SpiderMan#kpop"


class TestNewMultiPlatformQualityGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbmod.connect(str(Path(self.tmp.name) / "test.sqlite3"))
        self.t1 = datetime(2026, 11, 15, 20, 36, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_clown_below_both_thresholds_no_burst(self):
        """小丑短视频实测：7 条、总分 1.88、跨 2 平台——两个门槛都不过，不算爆发。"""
        ev = _event_with("evt_clown", CLOWN_TITLE, self.t1.isoformat(), 7, 1.88)
        persist_events(self.conn, [ev], now=self.t1)
        self.assertEqual(detect_bursts(self.conn, now=self.t1), [])

    def test_livestream_above_thresholds_bursts(self):
        """小时直播：103 条、总分 32——门槛过了，照常爆发，且不是重复出现（库里没有更早同名事件）。"""
        ev = _event_with("evt_live", LIVE_TITLE, self.t1.isoformat(), 103, 32.0)
        persist_events(self.conn, [ev], now=self.t1)
        bursts = detect_bursts(self.conn, now=self.t1)
        self.assertEqual(len(bursts), 1)
        self.assertIn("new_multi_platform", bursts[0]["reasons"])
        self.assertFalse(bursts[0]["is_recurrence"])

    def test_item_count_boundary_passes(self):
        """条数恰好 10、总分 0——条数门槛过了，算爆发（OR 关系）。"""
        ev = _event_with("evt_ten", "Ten items, no score", self.t1.isoformat(), 10, 0.0)
        persist_events(self.conn, [ev], now=self.t1)
        self.assertEqual(len(detect_bursts(self.conn, now=self.t1)), 1)

    def test_total_score_boundary_passes(self):
        """条数 1、总分恰好 5.0——总分门槛过了，算爆发（OR 关系）。"""
        ev = _event_with("evt_five", "One item, score five", self.t1.isoformat(), 1, 5.0)
        persist_events(self.conn, [ev], now=self.t1)
        self.assertEqual(len(detect_bursts(self.conn, now=self.t1)), 1)

    def test_below_both_boundaries_no_burst(self):
        """9 条、4.99 分——两个门槛都差一点，不算爆发。"""
        ev = _event_with("evt_nine", "Nine items", self.t1.isoformat(), 9, 4.99)
        persist_events(self.conn, [ev], now=self.t1)
        self.assertEqual(detect_bursts(self.conn, now=self.t1), [])

    def test_custom_thresholds_override(self):
        """阈值可配：15 条/8.0 分在默认阈值下爆发，在收紧的阈值下不爆发。"""
        ev = _event_with("evt_mid", "Fifteen items", self.t1.isoformat(), 15, 8.0)
        persist_events(self.conn, [ev], now=self.t1)
        self.assertEqual(len(detect_bursts(self.conn, now=self.t1)), 1)
        self.assertEqual(
            detect_bursts(
                self.conn,
                thresholds={"alert_min_items": 20, "alert_min_total_score": 10.0},
                now=self.t1,
            ),
            [],
        )

    def test_growth_spike_unaffected_by_gate(self):
        """growth_spike 自带增量门槛：2 条/1.0 分的小事件，增长翻倍且增量够，照常触发。"""
        _persist_two_rounds(
            self.conn,
            _event_with("evt_g", "Small but spiking", self.t1.isoformat(), 2, 1.0,
                        max_growth=100.0, platforms=("rss",)),
            _event_with("evt_g", "Small but spiking", self.t1.isoformat(), 2, 1.0,
                        max_growth=500.0, platforms=("rss",)),
            self.t1,
            self.t1 + timedelta(minutes=30),
        )
        bursts = detect_bursts(self.conn, now=self.t1 + timedelta(minutes=30))
        self.assertEqual(len(bursts), 1)
        self.assertIn("growth_spike", bursts[0]["reasons"])

    def test_score_surge_unaffected_by_gate(self):
        """score_surge 同理：2 条的小事件，总分 1 -> 10，照常触发。"""
        _persist_two_rounds(
            self.conn,
            _event_with("evt_s", "Small but surging", self.t1.isoformat(), 2, 1.0,
                        max_growth=10.0, platforms=("rss",)),
            _event_with("evt_s", "Small but surging", self.t1.isoformat(), 2, 10.0,
                        max_growth=10.0, platforms=("rss",)),
            self.t1,
            self.t1 + timedelta(minutes=30),
        )
        bursts = detect_bursts(self.conn, now=self.t1 + timedelta(minutes=30))
        self.assertEqual(len(bursts), 1)
        self.assertIn("score_surge", bursts[0]["reasons"])


class TestRecurrence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbmod.connect(str(Path(self.tmp.name) / "test.sqlite3"))
        self.t0 = datetime(2026, 11, 15, 19, 36, tzinfo=timezone.utc)
        self.t1 = datetime(2026, 11, 15, 20, 36, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _persist_pair(self, first, second):
        persist_events(self.conn, [first], now=self.t0)
        persist_events(self.conn, [second], now=self.t1)

    def test_second_hour_same_name_is_recurrence(self):
        """实测场景：同名直播 1 小时后又出现——第二条是重复出现，第一条不是。"""
        first = _event_with("evt_live1", LIVE_TITLE, self.t0.isoformat(), 103, 32.0)
        second = _event_with("evt_live2", LIVE_TITLE, self.t1.isoformat(), 110, 34.0)
        self._persist_pair(first, second)
        bursts = detect_bursts(self.conn, now=self.t1)
        by_id = {b["event_id"]: b for b in bursts}
        self.assertEqual(len(bursts), 2)
        self.assertFalse(by_id["evt_live1"]["is_recurrence"])
        self.assertTrue(by_id["evt_live2"]["is_recurrence"])
        self.assertIn("new_multi_platform", by_id["evt_live2"]["reasons"])

    def test_outside_window_not_recurrence(self):
        """同名事件 25 小时后才出现——超出默认 24 小时窗口，不算重复出现。"""
        t25 = self.t0 + timedelta(hours=25)
        first = _event_with("evt_live1", LIVE_TITLE, self.t0.isoformat(), 103, 32.0)
        second = _event_with("evt_live2", LIVE_TITLE, t25.isoformat(), 110, 34.0)
        persist_events(self.conn, [first], now=self.t0)
        persist_events(self.conn, [second], now=t25)
        bursts = detect_bursts(self.conn, now=t25)
        by_id = {b["event_id"]: b for b in bursts}
        self.assertFalse(by_id["evt_live2"]["is_recurrence"])

    def test_different_name_not_recurrence(self):
        """名字不同——不是重复出现。"""
        first = _event_with("evt_live1", LIVE_TITLE, self.t0.isoformat(), 103, 32.0)
        second = _event_with("evt_other", "Something else entirely", self.t1.isoformat(),
                             110, 34.0)
        self._persist_pair(first, second)
        bursts = detect_bursts(self.conn, now=self.t1)
        by_id = {b["event_id"]: b for b in bursts}
        self.assertFalse(by_id["evt_other"]["is_recurrence"])

    def test_earlier_single_platform_not_recurrence(self):
        """早先同名事件只有 1 个平台——不算“多平台事件”，后出现的不是重复出现。"""
        first = _event_with("evt_live1", LIVE_TITLE, self.t0.isoformat(), 103, 32.0,
                            platforms=("rss",))
        second = _event_with("evt_live2", LIVE_TITLE, self.t1.isoformat(), 110, 34.0)
        self._persist_pair(first, second)
        bursts = detect_bursts(self.conn, now=self.t1)
        by_id = {b["event_id"]: b for b in bursts}
        self.assertEqual(len(bursts), 1)  # 早先单平台那条本身不是爆发
        self.assertFalse(by_id["evt_live2"]["is_recurrence"])

    def test_name_normalization_case_and_spaces(self):
        """大小写/多余空白不同——归一化后同名，照样判重复出现。"""
        first = _event_with("evt_live1", LIVE_TITLE, self.t0.isoformat(), 103, 32.0)
        odd = "  today breaking news LIVE | Top Headlines Of The Hour | 15.11.2026  "
        second = _event_with("evt_live2", odd, self.t1.isoformat(), 110, 34.0)
        self._persist_pair(first, second)
        bursts = detect_bursts(self.conn, now=self.t1)
        by_id = {b["event_id"]: b for b in bursts}
        self.assertTrue(by_id["evt_live2"]["is_recurrence"])

    def test_window_zero_disables_recurrence(self):
        """窗口设为 0——重复出现判断关闭，第二条不算重复出现。"""
        first = _event_with("evt_live1", LIVE_TITLE, self.t0.isoformat(), 103, 32.0)
        second = _event_with("evt_live2", LIVE_TITLE, self.t1.isoformat(), 110, 34.0)
        self._persist_pair(first, second)
        bursts = detect_bursts(
            self.conn, thresholds={"recurrence_window_hours": 0}, now=self.t1
        )
        by_id = {b["event_id"]: b for b in bursts}
        self.assertFalse(by_id["evt_live2"]["is_recurrence"])

    def test_is_recurrence_helper_edges(self):
        """is_recurrence 直测：空名字、未知事件、窗口关闭都返回 False。"""
        from trend_radar.burst import is_recurrence

        self.assertFalse(is_recurrence(self.conn, "evt_x", "", self.t1.isoformat()))
        self.assertFalse(
            is_recurrence(self.conn, "evt_x", LIVE_TITLE, self.t1.isoformat())
        )
        first = _event_with("evt_live1", LIVE_TITLE, self.t0.isoformat(), 103, 32.0)
        persist_events(self.conn, [first], now=self.t0)
        self.assertFalse(
            is_recurrence(
                self.conn, "evt_live2", LIVE_TITLE, self.t1.isoformat(),
                window_hours=0, now=self.t1,
            )
        )
        self.assertTrue(
            is_recurrence(
                self.conn, "evt_live2", LIVE_TITLE, self.t1.isoformat(),
                window_hours=24.0, now=self.t1,
            )
        )


if __name__ == "__main__":
    unittest.main()
