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
        """条数恰好 20、总分 0——条数门槛过了，算爆发（OR 关系，2026-11-16 条数腿抬到 20）。"""
        ev = _event_with("evt_twenty", "Twenty items, no score", self.t1.isoformat(), 20, 0.0)
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

LIVE_TITLE_OLD = "Today Breaking News LIVE | Top Headlines Of The Hour | 14.11.2026"


class TestRecurrenceDateAndGate20261116(unittest.TestCase):
    """2026-11-16 生产反馈：小丑 11 条/2.94 分又报了、带日期的直播
    每天换个新标题又报了。根因见 trend_radar/burst.py。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbmod.connect(str(Path(self.tmp.name) / "test.sqlite3"))
        self.t0 = datetime(2026, 11, 15, 19, 36, tzinfo=timezone.utc)
        self.t1 = datetime(2026, 11, 15, 20, 36, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_clown_11_items_low_score_no_burst(self):
        """生产实测（2026-11-16 09:10）：小丑 11 条、总分 2.94——条数腿
        已过旧门槛（10），靠条数一条腿漏过去了；收紧后应拦住。"""
        ev = _event_with("evt_clown2", CLOWN_TITLE, self.t1.isoformat(), 11, 2.94)
        persist_events(self.conn, [ev], now=self.t1)
        self.assertEqual(detect_bursts(self.conn, now=self.t1), [])

    def test_real_news_146_items_bursts(self):
        """生产实测（2026-11-16 09:15）：真新闻 146 条、总分 45.95——必须照常爆发。"""
        ev = _event_with(
            "evt_real", "突发新闻——国家警察局长在国家警察大军 81 周年庆典上的指挥",
            self.t1.isoformat(), 146, 45.95,
        )
        persist_events(self.conn, [ev], now=self.t1)
        bursts = detect_bursts(self.conn, now=self.t1)
        self.assertEqual(len(bursts), 1)
        self.assertIn("new_multi_platform", bursts[0]["reasons"])

    def test_small_but_real_5_items_high_score_bursts(self):
        """条数少但总分高的小而真事件：5 条、8.0 分——靠总分腿照常爆发。"""
        ev = _event_with("evt_small", "Small but real story", self.t1.isoformat(), 5, 8.0)
        persist_events(self.conn, [ev], now=self.t1)
        self.assertEqual(len(detect_bursts(self.conn, now=self.t1)), 1)

    def test_item_count_new_boundary_20(self):
        """条数腿抬到 20：20 条/0 分过，19 条/0 分不过。"""
        ev20 = _event_with("evt_twenty", "Twenty items", self.t1.isoformat(), 20, 0.0)
        persist_events(self.conn, [ev20], now=self.t1)
        self.assertEqual(len(detect_bursts(self.conn, now=self.t1)), 1)
        ev19 = _event_with("evt_nineteen", "Nineteen items", self.t1.isoformat(), 19, 0.0)
        persist_events(self.conn, [ev19], now=self.t1)
        by_id = {b["event_id"]: b for b in detect_bursts(self.conn, now=self.t1)}
        self.assertIn("evt_twenty", by_id)
        self.assertNotIn("evt_nineteen", by_id)

    def test_livestream_date_change_is_recurrence(self):
        """直播标题里的日期 14.11.2026 → 15.11.2026：剥掉日期后同名，
        第二条是重复出现。"""
        first = _event_with("evt_live_old", LIVE_TITLE_OLD, self.t0.isoformat(), 103, 32.0)
        second = _event_with("evt_live_new", LIVE_TITLE, self.t1.isoformat(), 32, 10.17)
        persist_events(self.conn, [first], now=self.t0)
        persist_events(self.conn, [second], now=self.t1)
        bursts = detect_bursts(self.conn, now=self.t1)
        by_id = {b["event_id"]: b for b in bursts}
        self.assertEqual(len(bursts), 2)
        self.assertFalse(by_id["evt_live_old"]["is_recurrence"])
        self.assertTrue(by_id["evt_live_new"]["is_recurrence"])

    def test_chinese_date_stripped_is_recurrence(self):
        """中文日期同理：11月14日 → 11月15日，剥掉后同名。"""
        first = _event_with("evt_cn_old", "今日头条 11月14日", self.t0.isoformat(), 103, 32.0)
        second = _event_with("evt_cn_new", "今日头条 11月15日", self.t1.isoformat(), 32, 10.17)
        persist_events(self.conn, [first], now=self.t0)
        persist_events(self.conn, [second], now=self.t1)
        bursts = detect_bursts(self.conn, now=self.t1)
        by_id = {b["event_id"]: b for b in bursts}
        self.assertTrue(by_id["evt_cn_new"]["is_recurrence"])

    def test_iphone_17_vs_16_not_recurrence(self):
        """非日期的数字要保留：iPhone 17 与 iPhone 16 是两个故事。"""
        first = _event_with("evt_ip17", "iPhone 17 发布", self.t0.isoformat(), 103, 32.0)
        second = _event_with("evt_ip16", "iPhone 16 发布", self.t1.isoformat(), 110, 34.0)
        persist_events(self.conn, [first], now=self.t0)
        persist_events(self.conn, [second], now=self.t1)
        bursts = detect_bursts(self.conn, now=self.t1)
        by_id = {b["event_id"]: b for b in bursts}
        self.assertFalse(by_id["evt_ip16"]["is_recurrence"])

    def test_old_first_seen_but_recent_snapshot_is_recurrence(self):
        """窗口按老事件最近一次快照算，不按 first_seen：老事件首次出现
        在 24 小时窗口之外，但昨晚 20:44 还有快照——同名新事件仍是
        重复出现。"""
        t_old = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)
        t_recent = datetime(2026, 11, 15, 20, 44, tzinfo=timezone.utc)
        t_new = datetime(2026, 11, 16, 9, 10, tzinfo=timezone.utc)
        old = _event_with("evt_old", "Daily Live News", t_old.isoformat(), 25, 8.0)
        persist_events(self.conn, [old], now=t_old)
        persist_events(self.conn, [old], now=t_recent)
        new = _event_with("evt_new", "Daily Live News", t_new.isoformat(), 25, 8.0)
        persist_events(self.conn, [new], now=t_new)
        bursts = detect_bursts(self.conn, now=t_new)
        by_id = {b["event_id"]: b for b in bursts}
        self.assertIn("evt_new", by_id)
        self.assertTrue(by_id["evt_new"]["is_recurrence"])

    def test_date_recurrence_loses_ranking_boost_end_to_end(self):
        """联动：日期剥离判出的重复出现，进排名照样丢爆发分 + 新鲜度分
        （分差正好 30.0），原因里不写爆发词和“首次出现”。"""
        from trend_radar.event_ranking import rank_events

        old = _event_with("evt_live_old", LIVE_TITLE_OLD, self.t0.isoformat(), 100, 30.0)
        new = _event_with("evt_live_new", LIVE_TITLE, self.t1.isoformat(), 100, 30.0)
        real = _event_with("evt_real", "Real News Story", self.t1.isoformat(), 100, 30.0)
        persist_events(self.conn, [old], now=self.t0)
        persist_events(self.conn, [new, real], now=self.t1)
        bursts = detect_bursts(self.conn, now=self.t1)
        by_id = {b["event_id"]: b for b in bursts}
        self.assertTrue(by_id["evt_live_new"]["is_recurrence"])
        self.assertFalse(by_id["evt_real"]["is_recurrence"])
        events = [
            {"event_id": "evt_real", "name": "Real News Story",
             "first_seen": self.t1.isoformat(), "platform_count": 2,
             "platforms": ["rss", "youtube"], "max_growth": 0.0,
             "total_score": 30.0, "max_score": 30.0, "item_count": 100},
            {"event_id": "evt_live_new", "name": LIVE_TITLE,
             "first_seen": self.t1.isoformat(), "platform_count": 2,
             "platforms": ["rss", "youtube"], "max_growth": 0.0,
             "total_score": 30.0, "max_score": 30.0, "item_count": 100},
        ]
        ranked = rank_events(events, bursts=bursts, now=self.t1)
        rby = {e["event_id"]: e for e in ranked}
        self.assertEqual(
            rby["evt_real"]["rank_score"] - rby["evt_live_new"]["rank_score"], 30.0
        )
        self.assertNotIn("新事件首次跨平台", rby["evt_live_new"]["rank_reasons"])
        self.assertTrue(
            all("首次出现" not in r for r in rby["evt_live_new"]["rank_reasons"])
        )


class TestCrossRoundRefire20261116(unittest.TestCase):
    """2026-11-16 生产实测：09:15 与 09:31 两轮逐字节重复——同一批
    event_id 每轮重新触发 new_multi_platform。根因：detect_bursts 读
    库里所有事件，一条快照的事件只要过门槛就触发，不管那条快照是哪
    一轮的；聚类不稳定、事件某轮不再进聚类输出，就永远只有一条快照，
    每轮都触发；is_recurrence 要求不同 event_id，拦不住原事件本身。
    修法：only_fresh=True 时只有最新快照是本轮新产生的才参与检测。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbmod.connect(str(Path(self.tmp.name) / "test.sqlite3"))
        self.t1 = datetime(2026, 11, 16, 9, 15, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_same_event_bursts_only_once_across_rounds(self):
        """同一事件连续两轮只提醒一次：09:15 创建并爆发，09:31（16 分钟
        后，无新快照）不再触发。"""
        live = _event_with("evt_live", LIVE_TITLE, self.t1.isoformat(), 103, 32.0)
        persist_events(self.conn, [live], now=self.t1)
        first = detect_bursts(self.conn, now=self.t1, only_fresh=True)
        self.assertEqual(len(first), 1)
        self.assertIn("new_multi_platform", first[0]["reasons"])
        second = detect_bursts(
            self.conn, now=self.t1 + timedelta(minutes=16), only_fresh=True
        )
        self.assertEqual(second, [])

    def test_fresh_event_still_bursts(self):
        """新事件第一次跨平台仍能正常提醒（only_fresh 不误伤新事件）。"""
        live = _event_with("evt_live", LIVE_TITLE, self.t1.isoformat(), 103, 32.0)
        persist_events(self.conn, [live], now=self.t1)
        bursts = detect_bursts(self.conn, now=self.t1, only_fresh=True)
        self.assertEqual(len(bursts), 1)

    def test_stale_growth_spike_does_not_refire(self):
        """两条快照的老事件：最新快照陈旧时 growth_spike 也不再触发；
        最新快照是本轮的才触发。"""
        _persist_two_rounds(
            self.conn,
            _event_with("evt_g", "Spiking", self.t1.isoformat(), 2, 1.0,
                        max_growth=100.0, platforms=("rss",)),
            _event_with("evt_g", "Spiking", self.t1.isoformat(), 2, 1.0,
                        max_growth=500.0, platforms=("rss",)),
            self.t1,
            self.t1 + timedelta(minutes=30),
        )
        fresh = detect_bursts(
            self.conn, now=self.t1 + timedelta(minutes=30), only_fresh=True
        )
        self.assertEqual(len(fresh), 1)
        self.assertIn("growth_spike", fresh[0]["reasons"])
        stale = detect_bursts(
            self.conn, now=self.t1 + timedelta(minutes=46), only_fresh=True
        )
        self.assertEqual(stale, [])

    def test_default_without_only_fresh_keeps_old_behavior(self):
        """默认不传 only_fresh 时原行为不变（面板/独立调用仍看到最近
        的爆发；只有采集器传 only_fresh=True 才按轮次过滤）。"""
        live = _event_with("evt_live", LIVE_TITLE, self.t1.isoformat(), 103, 32.0)
        persist_events(self.conn, [live], now=self.t1)
        bursts = detect_bursts(self.conn, now=self.t1 + timedelta(minutes=16))
        self.assertEqual(len(bursts), 1)


class TestRecurrenceSplit20261116(unittest.TestCase):
    """2026-11-16 09:15 实测：9 个同标题直播爆发，7 个是 recurrence、
    2 个不是（1 个还发出去了）。分裂机制：标题里的日期从 14.11.2026
    换成 15.11.2026，旧事件名字对不上；且窗口按老事件 first_seen 算，
    新日期系列最早的两条（其唯一前驱在窗口外）也逃掉。日期剥离 +
    最近快照锚点修好后，三条都应判为 recurrence。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbmod.connect(str(Path(self.tmp.name) / "test.sqlite3"))
        self.now = datetime(2026, 11, 16, 9, 15, tzinfo=timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_date_change_series_first_two_are_recurrence(self):
        old_title = "Today Breaking News LIVE | Top Headlines Of The Hour | 14.11.2026"
        new_title = "Today Breaking News LIVE | Top Headlines Of The Hour | 15.11.2026"
        # 旧日期事件：首次出现 11-14 20:00，但 11-15 20:00 还有快照
        # （最近快照在窗口内）。
        old = _event_with("evt_old", old_title, "2026-11-14T20:00:00+00:00", 103, 32.0)
        persist_events(
            self.conn, [old], now=datetime(2026, 11, 14, 20, 0, tzinfo=timezone.utc)
        )
        persist_events(
            self.conn, [old], now=datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)
        )
        # 新日期系列最早的三条：e1 首次出现 11-15 08:00（窗口外），
        # e2 11-15 10:00，e3 11-15 12:00。
        e1 = _event_with("evt_e1", new_title, "2026-11-15T08:00:00+00:00", 32, 10.17)
        e2 = _event_with("evt_e2", new_title, "2026-11-15T10:00:00+00:00", 32, 10.17)
        e3 = _event_with("evt_e3", new_title, "2026-11-15T12:00:00+00:00", 32, 10.17)
        persist_events(
            self.conn, [e1], now=datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
        )
        persist_events(
            self.conn, [e2], now=datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)
        )
        persist_events(
            self.conn, [e3], now=datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        )
        bursts = detect_bursts(self.conn, now=self.now)
        by_id = {b["event_id"]: b for b in bursts}
        self.assertEqual(len(bursts), 3)
        self.assertTrue(by_id["evt_e1"]["is_recurrence"])
        self.assertTrue(by_id["evt_e2"]["is_recurrence"])
        self.assertTrue(by_id["evt_e3"]["is_recurrence"])

if __name__ == "__main__":
    unittest.main()
